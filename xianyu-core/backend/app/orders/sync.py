"""订单同步（阶段 4-5）：mtop 已售订单分页拉取 → TradeOrder upsert。

数据来源二路互补：
- WS 付款事件实时落库（fulfillment/delivery._upsert_order，金额=0 待补）；
- 本模块由调度任务 order_sync 周期调用，补齐 total_amount_cent / order_status / paid_at
  并回填 goods 关联。

⚠️ sold.get 返回结构 [待验证]（protocol.md §5）：所有字段提取走多 key 防御性
（参考 listing/publisher._extract_item_id 模式），拿不到就跳过不炸，等实测后收紧。
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Goods, TradeOrder

logger = logging.getLogger(__name__)

SOLD_API = "mtop.taobao.idle.trade.merchant.sold.get"
SOLD_VERSION = "1.0"

PAGE_SIZE = 20
MAX_PAGES = 10  # 单次同步上限（600s 一轮，兜底足够）

# 平台文案 → 本地状态（protocol.md §5；文案一变即失效，做成表便于实测后调整）。
# 顺序即优先级：先认更具体的（"已付款，等待你发货"应落在 2 待发货 而非 1 已付款）。
_STATUS_TEXT_MAP = [
    ("待付款", 0),
    ("等待发货", 2),
    ("待发货", 2),
    ("已发货", 3),
    ("已完成", 4),
    ("交易成功", 4),
    ("已关闭", 5),
    ("退款", 5),
    ("已付款", 1),
]

_ORDER_ID_KEYS = ("orderId", "bizOrderId", "tradeOrderId", "id")
_ITEM_ID_KEYS = ("itemId", "xyItemId", "goodsId")
_BUYER_ID_KEYS = ("buyerUserId", "buyerId", "userId")
_BUYER_NAME_KEYS = ("buyerNick", "buyerName", "userName")
_AMOUNT_CENT_KEYS = ("totalAmountCent", "payAmountCent", "priceCent", "actualFeeCent")
_AMOUNT_YUAN_KEYS = ("totalAmount", "payAmount", "actualFee", "price")


def _first(item: dict, keys: tuple[str, ...]) -> Any:
    """按候选 key 顺序取第一个非空值。"""
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _extract_amount_cent(item: dict) -> int | None:
    """金额提取：优先"分"字段；退回"元"字段 ×100 四舍五入。"""
    cent = _first(item, _AMOUNT_CENT_KEYS)
    if isinstance(cent, (int, float)) and cent > 0:
        return int(cent)
    yuan = _first(item, _AMOUNT_YUAN_KEYS)
    if isinstance(yuan, (int, float)) and yuan > 0:
        return int(round(float(yuan) * 100))
    return None


def _extract_status(item: dict) -> int | None:
    """状态提取：多 key 拿文案，按关键词表映射本地 0-5；认不出返回 None（不动现值）。"""
    for key in ("orderStatusText", "statusText", "orderStatus", "status"):
        value = item.get(key)
        if not isinstance(value, str) or not value:
            continue
        for text, status in _STATUS_TEXT_MAP:
            if text in value:
                return status
    return None


def _find_order_list(data: dict) -> list[dict]:
    """从返回 data 里找订单列表（结构 [待验证]：先常见 key，再递归找含订单号的 dict 列表）。"""
    for key in ("orderList", "orders", "list", "rows", "cardList"):
        value = data.get(key)
        if isinstance(value, list) and value:
            return [v for v in value if isinstance(v, dict)]
    # 递归兜底：任一层级里出现"元素含订单号 key 的非空 dict 列表"即认为是订单列表
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(v for v in current.values() if isinstance(v, (dict, list)))
        elif isinstance(current, list) and current and all(isinstance(v, dict) for v in current):
            if any(_first(v, _ORDER_ID_KEYS) is not None for v in current):
                return current
    return []


def _find_last_end_row(data: dict, order_list: list[dict]) -> Any:
    """翻页游标 lastEndRow [待验证]：优先顶层字段，退回本页最后一条的订单号。"""
    for key in ("lastEndRow", "lastRow"):
        if data.get(key) not in (None, ""):
            return data[key]
    if order_list:
        return _first(order_list[-1], _ORDER_ID_KEYS)
    return None


async def sync_account_orders(
    db: AsyncSession, session, *, page_size: int = PAGE_SIZE
) -> dict:
    """拉取单账号已售订单并 upsert。session = auth 会话池 AccountSession。

    返回 {"synced": 新增数, "updated": 更新数}。失败抛 XianyuApiError（调度层记失败）。
    """
    synced = updated = 0
    last_end_row = None
    for page in range(1, MAX_PAGES + 1):
        payload: dict = {
            "needGroupInfo": True,
            "pageNumber": page,
            "pageSize": page_size,
            "userId": session.external_uid,
        }
        if last_end_row is not None:
            # 首页不传 null（服务端对 null 值的容忍度未知，能省则省）
            payload["lastEndRow"] = last_end_row
        data: dict = await session.mtop.call(SOLD_API, SOLD_VERSION, payload)
        order_list = _find_order_list(data)
        if not order_list:
            break  # 空页 = 拉完

        for item in order_list:
            order_id = _as_str(_first(item, _ORDER_ID_KEYS))
            if not order_id:
                continue
            created = await _upsert_order(db, session.account_id, order_id, item)
            synced += created
            updated += not created

        if len(order_list) < page_size:
            break
        last_end_row = _find_last_end_row(data, order_list)

    await db.commit()
    logger.info(
        "订单同步完成 account_id=%d synced=%d updated=%d",
        session.account_id, synced, updated,
    )
    return {"synced": synced, "updated": updated}


async def _upsert_order(
    db: AsyncSession, account_id: int, order_id: str, item: dict
) -> bool:
    """单条订单 upsert（order_id 唯一键兜底幂等）。返回是否新建。"""
    order = (
        await db.execute(select(TradeOrder).where(TradeOrder.order_id == order_id))
    ).scalar_one_or_none()

    amount_cent = _extract_amount_cent(item)
    status = _extract_status(item)
    item_id = _as_str(_first(item, _ITEM_ID_KEYS))

    # 商品关联：按闲鱼商品 ID 找本地 Goods，回填 goods_id / category
    goods = None
    if item_id:
        goods = (
            await db.execute(select(Goods).where(Goods.external_goods_id == item_id))
        ).scalar_one_or_none()

    if order is None:
        db.add(
            TradeOrder(
                account_id=account_id,
                order_id=order_id,
                external_order_id=_as_str(_first(item, ("externalOrderId",))),
                goods_id=goods.id if goods else None,
                category=goods.category if goods else "A",
                buyer_id=_as_str(_first(item, _BUYER_ID_KEYS)),
                buyer_name=_as_str(_first(item, _BUYER_NAME_KEYS)),
                total_amount_cent=amount_cent or 0,
                order_status=status if status is not None else 1,
                # sold.get 全是已售单，能出现即视为已付款
                paid_at=datetime.now(),
            )
        )
        return True

    # 已存在（通常来自 WS 付款事件）：只补空缺，不回退已有信息
    if amount_cent and not order.total_amount_cent:
        order.total_amount_cent = amount_cent
    if status is not None:
        order.order_status = status
    if goods and not order.goods_id:
        order.goods_id = goods.id
        order.category = goods.category
    buyer_id = _as_str(_first(item, _BUYER_ID_KEYS))
    if buyer_id and not order.buyer_id:
        order.buyer_id = buyer_id
    buyer_name = _as_str(_first(item, _BUYER_NAME_KEYS))
    if buyer_name and not order.buyer_name:
        order.buyer_name = buyer_name
    if not order.paid_at:
        order.paid_at = datetime.now()
    return False
