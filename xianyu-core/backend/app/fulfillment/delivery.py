"""自动发货（品类 A 专用，阶段 4-4 移植；品类 B 只推订单通知不进发货流程）。

核心原则（xianyu-pilot 已验证，盘点 §4.4）：
- 触发不走轮询，全走 WS 付款消息（ORDER_PAID 事件）；
- 卡密原子认领见 card_stock（UPDATE 抢行，不依赖先查后改）；
- 两阶段：①WS 发消息成功=message_confirmed ②再调确认发货 mtop → success；
- **发送结果不确定（回包超时/处理异常）→ unknown 态，永不自动重发**：
  消息可能已到买家，重发=重复发货，宁可人工核实；
- 只有"明确未发出"（WS 无连接）才回滚卡密、允许失败重试。

状态机（delivery_record.status）：
    pending → message_sending → message_confirmed → platform_confirmed → success
    ├─ 明确失败（无规则/无卡/未登记商品/WS 无连接）→ failed（可人工补课后重试）
    ├─ 发送不确定 → unknown（卡密保持已认领，人工核实）
    ├─ 平台确认失败 → 停在 message_confirmed（卡密已送达；consign 幂等可重试）
    └─ 品类 B / 规则关闭 → skipped（仅留痕，不自动发货）

幂等三道防线：内存 in-flight 键 + delivery_record.order_id 唯一约束 +
既有记录状态非 failed 一律跳过（failed 才在同一行上重试）。

兜底：heal_stuck_deliveries 把中断在 pending/message_sending 的记录转 unknown
（调度接线在阶段 4-5 scheduler）。
"""

import logging
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select

from app.api_client.errors import XianyuApiError
from app.core.config import settings
from app.fulfillment.card_stock import check_low_stock, claim_card, mark_used, rollback
from app.messaging.events import Event, EventType, event_bus
from app.models import CardGroup, DeliveryRecord, DeliveryRule, Goods, TradeOrder

logger = logging.getLogger(__name__)

# 命中即不再自动处理（幂等跳过 = 除 failed 外全部状态），
# 这里显式列出"已发出/可能已发出"集合供查询复用
ALREADY_SENT_STATES = {"success", "unknown", "message_sent", "message_confirmed", "platform_confirmed"}

CONSIGN_API = "mtop.taobao.idle.logistic.consign.dummy"
CONSIGN_VERSION = "1.0"
# 已发货幂等特征（参考 pro confirm_shipment）
_ALREADY_DELIVERED_MARKS = ("ORDER_ALREADY_DELIVERY", "已发货")

# 记录卡在中间态超过该分钟数 → heal 转 unknown（人工核实）
STUCK_DELIVERY_MINUTES = 30


# ==================== 付款消息信息提取（纯函数） ====================

def _extract_param_from_url(url: str, keys: tuple[str, ...]) -> str:
    """从 reminderUrl 的 query 里按优先级取参数（orderId/tradeId/id 等）。"""
    if not url:
        return ""
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return ""
    for key in keys:
        values = query.get(key) or []
        if values and values[0]:
            return str(values[0])
    return ""


def extract_order_id(payload: dict) -> str:
    """真实订单号：reminderUrl 的 orderId/tradeId/id。取不到返回空串。"""
    return _extract_param_from_url(
        str(payload.get("reminderUrl") or ""), ("orderId", "tradeId", "id")
    )


def extract_goods_id(payload: dict) -> str:
    """闲鱼商品 ID：消息体 xyGoodsId 优先，其次 reminderUrl 的 itemId。"""
    xy_goods_id = str(payload.get("xyGoodsId") or "")
    if xy_goods_id:
        return xy_goods_id
    return _extract_param_from_url(str(payload.get("reminderUrl") or ""), ("itemId", "itemIdStr"))


def extract_buyer_id(payload: dict) -> str:
    """买家 ID：系统卡片消息的 senderUserId 即买家（pro 同款判定），
    兜底 sId、URL 的 userId。"""
    buyer = str(payload.get("senderUserId") or "")
    if buyer:
        return buyer
    buyer = str(payload.get("sId") or "")
    return buyer or _extract_param_from_url(
        str(payload.get("reminderUrl") or ""), ("userId", "peerUserId")
    )


# ==================== 发货处理器 ====================

class DeliveryHandler:
    """订阅 ORDER_PAID 事件，执行品类 A 自动发货（品类 B 仅通知）。"""

    def __init__(self, session_factory, *, sender=None, consign=None, bus=None) -> None:
        self._session_factory = session_factory
        self._sender = sender          # 可注入 (account_id, peer_id, text) -> bool
        self._consign = consign        # 可注入 (account_id, order_id) -> tuple[bool, str]
        self.bus = bus or event_bus
        self._in_flight: set[str] = set()
        self._subscribed = False

    def start(self) -> None:
        if self._subscribed:
            return
        self._subscribed = True
        self.bus.subscribe(EventType.ORDER_PAID, self.on_order_paid)
        logger.info("自动发货处理器已启动（auto_consign=%s）", settings.delivery_auto_consign)

    # ---------- 主流程 ----------

    async def on_order_paid(self, event: Event) -> None:
        payload = event.payload or {}
        account_id = event.account_id

        # 幂等键：真实 order_id 优先；无订单号但消息有 pnmId 时用 pnm 键
        # （同一付款推送去重足够；两单同 pnm 不存在）。两者皆无 → 无法安全发货。
        order_id = extract_order_id(payload)
        pnm_id = str(payload.get("pnmId") or "")
        order_key = order_id or (f"pnm-{pnm_id}" if pnm_id else "")
        if not order_key:
            logger.warning("付款消息无订单号且无 pnmId，无法幂等发货，转人工 accountId=%d", account_id)
            return

        if order_key in self._in_flight:
            logger.info("并发去重拦截（同单处理中）orderKey=%s", order_key)
            return
        self._in_flight.add(order_key)
        try:
            await self._deliver(account_id, payload, order_id, order_key)
        except Exception:
            # 中断位置未知：卡密可能已认领甚至已发出 → unknown，永不自动重发
            logger.exception("发货处理异常 orderKey=%s", order_key)
            await self._mark(
                order_key, status="unknown",
                fail_reason="处理异常中断，需人工核实", account_id=account_id,
            )
        finally:
            self._in_flight.discard(order_key)

    async def _deliver(self, account_id: int, payload: dict, order_id: str, order_key: str) -> None:
        xy_goods_id = extract_goods_id(payload)
        buyer_id = extract_buyer_id(payload)
        buyer_name = str(payload.get("senderUserName") or "")
        receiver_info = {
            "buyerUserId": buyer_id,
            "buyerName": buyer_name,
            "xyGoodsId": xy_goods_id,
            "sId": str(payload.get("sId") or ""),
            "pnmId": str(payload.get("pnmId") or ""),
        }

        if not xy_goods_id:
            await self._mark(
                order_key, "failed", "无法从付款消息提取商品ID，转人工",
                account_id=account_id, receiver_info=receiver_info,
            )
            return
        if not buyer_id:
            await self._mark(
                order_key, "failed", "无法识别买家，转人工",
                account_id=account_id, receiver_info=receiver_info,
            )
            return

        async with self._session_factory() as db:
            # ① 幂等：非 failed 既有记录一律跳过（WS 周期重推/买家再留言重推付款卡片都拦在这）
            existing = (
                await db.execute(
                    select(DeliveryRecord).where(DeliveryRecord.order_id == order_key)
                )
            ).scalar_one_or_none()
            if existing is not None and existing.status != "failed":
                logger.info("订单已有发货记录，跳过 orderKey=%s status=%s", order_key, existing.status)
                return

            # ② 品类分流：查不到商品不发货（防止把未登记/品类 B 商品发出去）
            goods = (
                await db.execute(
                    select(Goods).where(
                        Goods.account_id == account_id,
                        Goods.external_goods_id == xy_goods_id,
                    )
                )
            ).scalar_one_or_none()
            if goods is None:
                await self._mark(
                    order_key,
                    "failed",
                    f"商品 {xy_goods_id} 未在系统登记（品类未知），不自动发货",
                    account_id=account_id,
                    receiver_info=receiver_info,
                )
                return

            await self._upsert_order(db, account_id, order_id, goods, buyer_id, buyer_name, payload)

            if goods.category != "A":
                # 品类 B：只留痕 + 推 ORDER_CREATED 人工通知（流程方案 §3：不自动化）
                await self._mark(
                    order_key,
                    "skipped",
                    "品类B人工履约，系统仅通知",
                    account_id=account_id,
                    receiver_info=receiver_info,
                )
                await self.bus.publish(
                    Event(
                        type=EventType.ORDER_CREATED,
                        account_id=account_id,
                        payload={
                            "orderId": order_id,
                            "orderKey": order_key,
                            "xyGoodsId": xy_goods_id,
                            "buyerUserId": buyer_id,
                            "buyerName": buyer_name,
                            "goodsTitle": goods.title,
                        },
                    )
                )
                return

            # ③ 发货规则：商品级 → 账号级（goods_id 为空）
            rule = (
                await db.execute(
                    select(DeliveryRule)
                    .where(
                        DeliveryRule.account_id == account_id,
                        (DeliveryRule.goods_id == goods.id)
                        | (DeliveryRule.goods_id.is_(None)),
                    )
                    .order_by(DeliveryRule.goods_id.is_(None))  # 商品级优先
                    .limit(1)
                )
            ).scalar_one_or_none()
            if rule is None:
                await self._mark(
                    order_key, "failed", "未配置发货规则（delivery_rule），不自动发货",
                    account_id=account_id, receiver_info=receiver_info,
                )
                return
            if not rule.trigger_on_pay:
                await self._mark(
                    order_key, "skipped", "规则未开启付款即发（trigger_on_pay=0）",
                    account_id=account_id, receiver_info=receiver_info,
                )
                return
            if rule.delivery_mode != "kami":
                await self._mark(
                    order_key, "failed",
                    f"发货模式 {rule.delivery_mode} 未实现（当前仅 kami 卡密）",
                    account_id=account_id, receiver_info=receiver_info,
                )
                return

            # 每日上限（按账号当日 success 数）
            day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_count = (
                await db.execute(
                    select(func.count(DeliveryRecord.id)).where(
                        DeliveryRecord.account_id == account_id,
                        DeliveryRecord.status == "success",
                        DeliveryRecord.created_at >= day_start,
                    )
                )
            ).scalar_one()
            if today_count >= rule.max_delivery_per_day:
                await self._mark(
                    order_key, "failed",
                    f"已达每日发货上限 {rule.max_delivery_per_day}，剩余订单转人工",
                    account_id=account_id, receiver_info=receiver_info,
                )
                return

            # ④ 卡密组（绑定商品）
            group = (
                await db.execute(
                    select(CardGroup).where(CardGroup.goods_id == goods.id)
                )
            ).scalar_one_or_none()
            if group is None:
                await self._mark(
                    order_key, "failed",
                    f"商品《{goods.title}》未绑定卡密组（card_group.goods_id）",
                    account_id=account_id, receiver_info=receiver_info,
                )
                return

        # ⑤ 占位 + 原子认领（独立短事务，认领即提交，防进程崩溃丢卡）
        await self._mark(
            order_key, "pending",
            account_id=account_id,
            delivery_mode="kami",
            receiver_info=receiver_info,
        )
        async with self._session_factory() as db:
            card = await claim_card(db, group.id, order_key)
        if card is None:
            await self._mark(order_key, "failed", "卡密库存不足，请补充后重试")
            return
        await self._mark(order_key, "message_sending", card_item_id=card.card_item_id)
        async with self._session_factory() as db:
            await check_low_stock(db, card.group_id)  # 余量告警（仅日志，前端轮询 /delivery/cards）

        # ⑥ 发卡密给买家
        result = await self._ws_send(account_id, buyer_id, card.content)
        if result == "no_client":
            async with self._session_factory() as db:
                await rollback(db, card.card_item_id)
            await self._mark(order_key, "failed", "WS 未连接未发出，卡密已回滚可重试")
            return
        if result != "sent":
            # 回包超时/发送异常：消息可能已到买家 → unknown，卡密不回滚不重发
            await self._mark(
                order_key, "unknown",
                "卡密发送结果不确定（回包超时/异常），已停止自动重发，需人工核实",
                card_item_id=card.card_item_id,
            )
            return

        # 消息送达即核销卡密（内容已给买家，无论平台确认是否成功都算已消耗）
        async with self._session_factory() as db:
            await mark_used(db, card.card_item_id)
        await self._mark(order_key, "message_confirmed", card_item_id=card.card_item_id)

        # ⑦ 平台确认发货（虚拟无需物流）。合成幂等键（pnm-）没有真实订单号，跳过
        if not order_id:
            logger.info("无真实订单号，跳过平台确认发货 orderKey=%s", order_key)
            return
        if not settings.delivery_auto_consign:
            logger.info("auto_consign 关闭，停在 message_confirmed orderKey=%s", order_key)
            return
        ok, reason = await self._do_consign(account_id, order_id)
        if not ok:
            await self._mark(
                order_key, "message_confirmed",
                fail_reason=f"平台确认发货失败：{reason}（卡密已送达，确认发货可重试）",
            )
            return
        await self._mark(order_key, "platform_confirmed")
        await self._mark(order_key, "success")
        await self._mark_order_shipped(order_id)
        logger.info("自动发货完成 orderKey=%s cardItemId=%d", order_key, card.card_item_id)

    # ---------- 发送与平台确认（注入点） ----------

    async def _ws_send(self, account_id: int, peer_id: str, text: str) -> str:
        """returns 'sent' / 'no_client'（明确未发出）/ 其他（结果不确定）。"""
        if self._sender is not None:
            try:
                return "sent" if await self._sender(account_id, peer_id, text) else "unconfirmed"
            except Exception:
                logger.exception("发货消息发送异常 accountId=%d peer=%s", account_id, peer_id)
                return "unconfirmed"
        from app.messaging.manager import ws_manager

        client = ws_manager.get_client(account_id)
        if client is None:
            return "no_client"
        try:
            ok = await client.send_text(peer_id, text)
        except Exception:
            logger.exception("发货消息发送异常 accountId=%d peer=%s", account_id, peer_id)
            return "unconfirmed"
        return "sent" if ok else "unconfirmed"

    async def _do_consign(self, account_id: int, order_id: str) -> tuple[bool, str]:
        """调 mtop.taobao.idle.logistic.consign.dummy 确认发货（is_write 走限流）。"""
        if self._consign is not None:
            return await self._consign(account_id, order_id)
        from app.auth.session_pool import session_pool

        session = session_pool.get(account_id)
        if session is None or session.mtop is None:
            return False, "账号会话不可用"
        data = {"orderId": str(order_id), "tradeText": "", "picList": [], "newUnconsign": True}
        try:
            await session.mtop.call(CONSIGN_API, CONSIGN_VERSION, data, is_write=True)
            return True, ""
        except XianyuApiError as e:
            ret_text = " ".join(str(r) for r in (e.ret or []))
            if any(mark in ret_text for mark in _ALREADY_DELIVERED_MARKS):
                return True, "订单已发货（幂等成功）"
            return False, str(e)

    # ---------- 落库辅助 ----------

    async def _mark(
        self,
        order_key: str,
        status: str,
        fail_reason: str | None = None,
        *,
        account_id: int | None = None,
        delivery_mode: str | None = None,
        card_item_id: int | None = None,
        receiver_info: dict | None = None,
    ) -> None:
        """upsert 发货记录状态（重试在同一行上更新，order_id 唯一约束兜底并发）。"""
        async with self._session_factory() as db:
            rec = (
                await db.execute(
                    select(DeliveryRecord).where(DeliveryRecord.order_id == order_key)
                )
            ).scalar_one_or_none()
            if rec is None:
                if account_id is None:
                    logger.warning("发货记录不存在且缺 account_id，丢弃状态 orderKey=%s", order_key)
                    return
                rec = DeliveryRecord(
                    order_id=order_key,
                    account_id=account_id,
                    delivery_mode=delivery_mode or "kami",
                    status=status,
                    card_item_id=card_item_id,
                    receiver_info=receiver_info,
                )
                db.add(rec)
            else:
                if status is not None:
                    rec.status = status
                if account_id is not None:
                    rec.account_id = account_id
                if delivery_mode is not None:
                    rec.delivery_mode = delivery_mode
                if card_item_id is not None:
                    rec.card_item_id = card_item_id
                if receiver_info is not None:
                    rec.receiver_info = receiver_info
            if fail_reason is not None:
                rec.fail_reason = fail_reason
            await db.commit()

    async def _upsert_order(
        self, db, account_id: int, order_id: str, goods: Goods,
        buyer_id: str, buyer_name: str, payload: dict,
    ) -> None:
        """付款订单落库（无真实 order_id 时无法落，order_id 是唯一键）。"""
        if not order_id:
            return
        order = (
            await db.execute(select(TradeOrder).where(TradeOrder.order_id == order_id))
        ).scalar_one_or_none()
        now = datetime.now()
        if order is None:
            db.add(
                TradeOrder(
                    account_id=account_id,
                    order_id=order_id,
                    goods_id=goods.id,
                    category=goods.category,
                    buyer_id=buyer_id or None,
                    buyer_name=buyer_name or None,
                    total_amount_cent=0,  # 付款卡片无金额，订单同步任务（阶段4-5）补齐
                    order_status=1,  # 已付款
                    paid_at=now,
                )
            )
        else:
            order.order_status = max(order.order_status, 1)
            order.paid_at = order.paid_at or now
        await db.commit()

    async def _mark_order_shipped(self, order_id: str) -> None:
        async with self._session_factory() as db:
            order = (
                await db.execute(select(TradeOrder).where(TradeOrder.order_id == order_id))
            ).scalar_one_or_none()
            if order is not None:
                order.order_status = 3  # 已发货
                await db.commit()


# ==================== 兜底 heal（调度接线在阶段 4-5） ====================

async def heal_stuck_deliveries(session_factory, *, stuck_minutes: int = STUCK_DELIVERY_MINUTES) -> int:
    """把中断在 pending/message_sending 超时的记录转 unknown（人工核实）。

    不回滚卡密：中断点未知，消息可能已到买家（防重发哲学）。
    返回处理条数。
    """
    cutoff = datetime.now() - timedelta(minutes=stuck_minutes)
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(DeliveryRecord).where(
                    DeliveryRecord.status.in_(["pending", "message_sending"]),
                    DeliveryRecord.updated_at < cutoff,
                )
            )
        ).scalars().all()
        for rec in rows:
            rec.status = "unknown"
            rec.fail_reason = "处理中断（进程重启/事件丢失），需人工核实"
        await db.commit()
        if rows:
            logger.warning("发货兜底 heal：%d 条中间态记录转 unknown", len(rows))
        return len(rows)


def create_delivery_handler(session_factory, **kwargs) -> DeliveryHandler:
    """工厂（lifespan 中创建并 start；sender/consign 默认走 ws_manager/session_pool）。"""
    return DeliveryHandler(session_factory, **kwargs)
