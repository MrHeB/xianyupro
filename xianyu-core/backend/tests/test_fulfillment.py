"""自动发货测试：内存 SQLite + 假发送器/假 consign，覆盖完整事件流。

覆盖：信息提取纯函数、卡密原子认领/核销/回滚/低库存、
全链路成功（含平台确认 + 订单状态流转）、幂等跳过、failed 重试、
unknown 永不重发、品类 B 分流通知、规则/库存各失败分支、每日上限、
pnm 幂等键、consign 失败停在 message_confirmed、孤儿 heal。
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.fulfillment.card_stock import (
    check_low_stock,
    claim_card,
    mark_used,
    remaining_count,
    rollback,
)
from app.fulfillment.delivery import (
    DeliveryHandler,
    extract_buyer_id,
    extract_goods_id,
    extract_order_id,
    heal_stuck_deliveries,
)
from app.messaging.events import Event, EventBus, EventType
from app.models import (
    Base,
    CardGroup,
    CardItem,
    DeliveryRecord,
    DeliveryRule,
    Goods,
    TradeOrder,
)


# ==================== 公共测试件 ====================


class LocalBus(EventBus):
    """同步等待 handler 完成的总线（测试用，避免 create_task 时序问题）。"""

    def __init__(self):
        super().__init__()
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        for handler in self._handlers.get(event.type, []):
            await self._safe_run(handler, event)
        self.published.append(event)  # 记录所有发布过的事件（验证品类 B 通知用）


class FakeSender:
    def __init__(self, result: bool = True, raise_exc: Exception | None = None):
        self.result = result
        self.raise_exc = raise_exc
        self.sent: list[tuple[int, str, str]] = []

    async def __call__(self, account_id: int, peer_id: str, text: str) -> bool:
        self.sent.append((account_id, peer_id, text))
        if self.raise_exc:
            raise self.raise_exc
        return self.result


class FakeConsign:
    def __init__(self, ok: bool = True, reason: str = ""):
        self.ok = ok
        self.reason = reason
        self.calls: list[str] = []

    async def __call__(self, account_id: int, order_id: str):
        self.calls.append(order_id)
        return self.ok, self.reason


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def bus():
    return LocalBus()


async def _seed_goods_a(session_factory, *, category="A", with_rule=True, with_group=True,
                        cards=(("K001", "V001"), ("K002", "V002")), limit=200,
                        trigger_on_pay=1, external_goods_id="xy-goods-1"):
    """标准品类 A/B 商品 + 卡密组 + 规则。返回 goods id。"""
    async with session_factory() as db:
        goods = Goods(
            account_id=1,
            external_goods_id=external_goods_id,
            category=category,
            title="测试卡密商品",
            price_cent=990,
        )
        db.add(goods)
        await db.flush()
        if with_group:
            group = CardGroup(name="组1", goods_id=goods.id, total_count=len(cards))
            db.add(group)
            await db.flush()
            for key, value in cards:
                db.add(CardItem(group_id=group.id, card_key=key, card_value=value))
        if with_rule:
            db.add(
                DeliveryRule(
                    account_id=1,
                    goods_id=goods.id,
                    delivery_mode="kami",
                    trigger_on_pay=trigger_on_pay,
                    max_delivery_per_day=limit,
                )
            )
        await db.commit()
        return goods.id


def _paid_event(
    *,
    order_id: str | None = "1000001",
    pnm_id: str = "pnm-1",
    sender: str = "buyer001",
    xy_goods_id: str = "xy-goods-1",
) -> Event:
    url = (
        f"https://m.goofish.com/item?trip=order&orderId={order_id}&itemId={xy_goods_id}"
        if order_id
        else f"https://m.goofish.com/item?trip=order&itemId={xy_goods_id}"
    )
    return Event(
        type=EventType.ORDER_PAID,
        account_id=1,
        payload={
            "sId": "47812870000",
            "pnmId": pnm_id,
            "senderUserId": sender,
            "senderUserName": "买家小明",
            "contentType": 26,
            "msgContent": "买家已付款，等待你发货",
            "reminderUrl": url,
            "xyGoodsId": xy_goods_id,
        },
    )


def make_handler(session_factory, bus, sender=None, consign=None) -> DeliveryHandler:
    handler = DeliveryHandler(
        session_factory, sender=sender, consign=consign, bus=bus
    )
    handler.start()
    return handler


async def _get_record(session_factory, order_key) -> DeliveryRecord | None:
    async with session_factory() as db:
        return (
            await db.execute(
                select(DeliveryRecord).where(DeliveryRecord.order_id == order_key)
            )
        ).scalar_one_or_none()


# ==================== 信息提取纯函数 ====================


def test_extract_order_id_priority():
    payload = {"reminderUrl": "https://m.goofish.com/x?tradeId=222&orderId=111&orderId=111"}
    assert extract_order_id(payload) == "111"
    assert extract_order_id({"reminderUrl": "https://m.goofish.com/x?tradeId=333"}) == "333"
    assert extract_order_id({"reminderUrl": "https://m.goofish.com/x?id=9"}) == "9"
    assert extract_order_id({"reminderUrl": ""}) == ""
    assert extract_order_id({}) == ""


def test_extract_goods_id_field_first():
    payload = {
        "xyGoodsId": "from-field",
        "reminderUrl": "https://m.goofish.com/x?itemId=from-url",
    }
    assert extract_goods_id(payload) == "from-field"
    assert extract_goods_id({"reminderUrl": "https://m.goofish.com/x?itemId=from-url"}) == "from-url"
    assert extract_goods_id({}) == ""


def test_extract_buyer_id_fallbacks():
    assert extract_buyer_id({"senderUserId": "b1", "sId": "s1"}) == "b1"
    assert extract_buyer_id({"sId": "s1"}) == "s1"
    assert (
        extract_buyer_id({"reminderUrl": "https://m.goofish.com/x?userId=u9&itemId=i"}) == "u9"
    )
    assert extract_buyer_id({}) == ""


# ==================== 卡密库存 ====================


async def test_claim_card_takes_oldest_and_formats(session_factory):
    await _seed_goods_a(session_factory)
    async with session_factory() as db:
        group_id = (await db.execute(select(CardGroup.id))).scalar_one()
        card = await claim_card(db, group_id, "1000001")
    assert card is not None
    assert card.content == "K001----V001"
    async with session_factory() as db:
        items = (await db.execute(select(CardItem).order_by(CardItem.id))).scalars().all()
    assert [i.status for i in items] == [1, 0]
    assert items[0].used_order_id == "1000001"


async def test_claim_card_valueless_key(session_factory):
    async with session_factory() as db:
        group = CardGroup(name="g", total_count=1)
        db.add(group)
        await db.flush()
        db.add(CardItem(group_id=group.id, card_key="ONLYKEY", card_value=None))
        await db.commit()
        group_id = group.id
    async with session_factory() as db:
        card = await claim_card(db, group_id, "o1")
    assert card.content == "ONLYKEY"


async def test_claim_card_empty_returns_none(session_factory):
    await _seed_goods_a(session_factory, cards=[])
    async with session_factory() as db:
        group_id = (await db.execute(select(CardGroup.id))).scalar_one()
        assert await claim_card(db, group_id, "o1") is None


async def test_mark_used_and_rollback(session_factory):
    await _seed_goods_a(session_factory)
    async with session_factory() as db:
        group_id = (await db.execute(select(CardGroup.id))).scalar_one()
        card = await claim_card(db, group_id, "o1")
        await mark_used(db, card.card_item_id)
    async with session_factory() as db:
        item = await db.get(CardItem, card.card_item_id)
        group = await db.get(CardGroup, group_id)
    assert item.status == 2
    assert group.used_count == 1

    async with session_factory() as db:
        card2 = await claim_card(db, group_id, "o2")
        await rollback(db, card2.card_item_id)
    async with session_factory() as db:
        item2 = await db.get(CardItem, card2.card_item_id)
    assert item2.status == 0
    assert item2.used_order_id is None


async def test_check_low_stock(session_factory):
    await _seed_goods_a(session_factory, cards=(("K1", "V1"),))
    async with session_factory() as db:
        group_id = (await db.execute(select(CardGroup.id))).scalar_one()
        card = await claim_card(db, group_id, "o1")
        await mark_used(db, card.card_item_id)
        assert await remaining_count(db, group_id) == 0
        assert await check_low_stock(db, group_id) is True  # 0 <= 阈值5


# ==================== 发货主流程 ====================


async def test_full_success_flow(session_factory, bus):
    goods_id = await _seed_goods_a(session_factory)
    sender, consign = FakeSender(), FakeConsign()
    handler = make_handler(session_factory, bus, sender=sender, consign=consign)

    await bus.publish(_paid_event())

    # ① 卡密发出：发给买家，内容是第一张卡
    assert sender.sent == [(1, "buyer001", "K001----V001")]
    # ② 平台确认发货被调（真实订单号）
    assert consign.calls == ["1000001"]
    # ③ 发货记录终态 success，绑定卡密
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "success"
    assert rec.card_item_id is not None
    assert rec.fail_reason is None
    # ④ 卡密核销 + 组计数
    async with session_factory() as db:
        item = await db.get(CardItem, rec.card_item_id)
        group = (
            (await db.execute(select(CardGroup).where(CardGroup.goods_id == goods_id)))
            .scalars()
            .one()
        )
    assert item.status == 2
    assert group.used_count == 1
    # ⑤ 订单落库且状态流转到已发货
    async with session_factory() as db:
        order = (
            await db.execute(select(TradeOrder).where(TradeOrder.order_id == "1000001"))
        ).scalar_one()
    assert order.order_status == 3
    assert order.category == "A"
    assert order.buyer_id == "buyer001"


async def test_duplicate_event_skipped(session_factory, bus):
    await _seed_goods_a(session_factory, cards=(("K1", "V1"), ("K2", "V2"), ("K3", "V3")))
    sender = FakeSender()
    # 断言终态 success，须注入 consign 桩（默认实现走 session_pool，测试环境无会话）
    handler = make_handler(session_factory, bus, sender=sender, consign=FakeConsign())

    await bus.publish(_paid_event())
    await bus.publish(_paid_event())  # WS 周期重推同一条付款消息

    assert len(sender.sent) == 1  # 只发一张卡
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "success"


async def test_failed_then_retry_after_fix(session_factory, bus):
    # 商品未登记 → failed；补登记后重推同单 → 成功（failed 可重试）
    sender = FakeSender()
    # 断言终态 success，须注入 consign 桩（默认实现走 session_pool，测试环境无会话）
    handler = make_handler(session_factory, bus, sender=sender, consign=FakeConsign())

    await bus.publish(_paid_event())
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "failed"
    assert "未在系统登记" in rec.fail_reason
    assert sender.sent == []

    await _seed_goods_a(session_factory)
    await bus.publish(_paid_event(pnm_id="pnm-1-dup"))
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "success"
    assert len(sender.sent) == 1


async def test_unconfirmed_send_becomes_unknown_no_resend(session_factory, bus):
    await _seed_goods_a(session_factory)
    sender = FakeSender(result=False)  # 回包超时/非200
    handler = make_handler(session_factory, bus, sender=sender)

    await bus.publish(_paid_event())

    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "unknown"
    assert "不确定" in rec.fail_reason
    # 卡密不回滚（已认领，防重复发货）
    async with session_factory() as db:
        item = await db.get(CardItem, rec.card_item_id)
    assert item.status == 1

    # 重推同单：unknown 命中幂等跳过，绝不二次发送
    await bus.publish(_paid_event(pnm_id="pnm-dup"))
    assert len(sender.sent) == 1
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "unknown"


async def test_sender_exception_becomes_unknown(session_factory, bus):
    await _seed_goods_a(session_factory)
    sender = FakeSender(raise_exc=RuntimeError("boom"))
    handler = make_handler(session_factory, bus, sender=sender)

    await bus.publish(_paid_event())
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "unknown"


async def test_no_ws_client_rolls_back_card(session_factory, bus, monkeypatch):
    await _seed_goods_a(session_factory)
    handler = make_handler(session_factory, bus)  # 不注入 sender，走默认 ws_manager

    from app.messaging import manager

    monkeypatch.setattr(manager.ws_manager, "get_client", lambda _aid: None)

    await bus.publish(_paid_event())
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "failed"
    assert "WS 未连接" in rec.fail_reason
    # 卡密回滚为可用
    async with session_factory() as db:
        items = (await db.execute(select(CardItem).order_by(CardItem.id))).scalars().all()
    assert [i.status for i in items] == [0, 0]


async def test_category_b_skipped_and_notified(session_factory, bus):
    await _seed_goods_a(session_factory, category="B")
    sender = FakeSender()
    handler = make_handler(session_factory, bus, sender=sender)

    await bus.publish(_paid_event())

    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "skipped"
    assert sender.sent == []  # 品类 B 绝不自动发货
    # 推了 ORDER_CREATED 人工通知
    created = [e for e in bus.published if e.type == EventType.ORDER_CREATED]
    assert len(created) == 1
    assert created[0].payload["goodsTitle"] == "测试卡密商品"
    # 订单落库（已付款，等人工履约）
    async with session_factory() as db:
        order = (
            await db.execute(select(TradeOrder).where(TradeOrder.order_id == "1000001"))
        ).scalar_one()
    assert order.order_status == 1


async def test_account_level_rule_fallback(session_factory, bus):
    """商品级规则缺失时回退账号级（goods_id 为空）。"""
    goods_id = await _seed_goods_a(session_factory, with_rule=False)
    async with session_factory() as db:
        db.add(
            DeliveryRule(
                account_id=1,
                goods_id=None,
                delivery_mode="kami",
                trigger_on_pay=1,
                max_delivery_per_day=200,
            )
        )
        await db.commit()

    sender = FakeSender()
    handler = make_handler(session_factory, bus, sender=sender, consign=FakeConsign())
    await bus.publish(_paid_event())

    assert len(sender.sent) == 1
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "success"


async def test_missing_rule_fails(session_factory, bus):
    await _seed_goods_a(session_factory, with_rule=False)
    sender = FakeSender()
    handler = make_handler(session_factory, bus, sender=sender)

    await bus.publish(_paid_event())
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "failed"
    assert "未配置发货规则" in rec.fail_reason
    assert sender.sent == []


async def test_missing_group_fails(session_factory, bus):
    await _seed_goods_a(session_factory, with_group=False)
    handler = make_handler(session_factory, bus, sender=FakeSender())

    await bus.publish(_paid_event())
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "failed"
    assert "未绑定卡密组" in rec.fail_reason


async def test_empty_stock_fails(session_factory, bus):
    await _seed_goods_a(session_factory, cards=())
    handler = make_handler(session_factory, bus, sender=FakeSender())

    await bus.publish(_paid_event())
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "failed"
    assert "库存不足" in rec.fail_reason


async def test_trigger_off_skipped(session_factory, bus):
    await _seed_goods_a(session_factory, trigger_on_pay=0)
    handler = make_handler(session_factory, bus, sender=FakeSender())

    await bus.publish(_paid_event())
    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "skipped"


async def test_daily_limit_blocks(session_factory, bus):
    # 3 张卡但每日上限 1：第一单成功，第二单 failed(达上限)——不会走到认领
    await _seed_goods_a(
        session_factory, cards=(("K1", "V1"), ("K2", "V2"), ("K3", "V3")), limit=1
    )
    sender = FakeSender()
    handler = make_handler(session_factory, bus, sender=sender, consign=FakeConsign())

    await bus.publish(_paid_event(order_id="2000001", pnm_id="p1"))
    await bus.publish(_paid_event(order_id="2000002", pnm_id="p2"))

    rec1 = await _get_record(session_factory, "2000001")
    rec2 = await _get_record(session_factory, "2000002")
    assert rec1.status == "success"
    assert rec2.status == "failed"
    assert "每日发货上限" in rec2.fail_reason
    assert len(sender.sent) == 1
    # 第二单没消耗卡密
    async with session_factory() as db:
        group_id = (await db.execute(select(CardGroup.id))).scalar_one()
        assert await remaining_count(db, group_id) == 2


async def test_pnm_key_when_no_order_id(session_factory, bus):
    """付款提醒只有 sid 没有真实订单号：用 pnm 键发货，跳过平台确认。"""
    await _seed_goods_a(session_factory)
    sender = FakeSender()
    consign = FakeConsign()
    handler = make_handler(session_factory, bus, sender=sender, consign=consign)

    await bus.publish(_paid_event(order_id=None, pnm_id="pnm-only"))

    assert len(sender.sent) == 1
    assert consign.calls == []  # 无真实订单号不调 consign
    rec = await _get_record(session_factory, "pnm-pnm-only")
    assert rec.status == "message_confirmed"
    # 卡密已核销（内容已发给买家）
    async with session_factory() as db:
        item = await db.get(CardItem, rec.card_item_id)
    assert item.status == 2


async def test_no_order_id_no_pnm_gives_up(session_factory, bus):
    await _seed_goods_a(session_factory)
    sender = FakeSender()
    handler = make_handler(session_factory, bus, sender=sender)

    event = _paid_event(order_id=None)
    event.payload["pnmId"] = ""
    await bus.publish(event)

    assert sender.sent == []
    assert await _get_record(session_factory, "") is None
    # 连记录都不该有（无幂等键无法安全发货）


async def test_consign_failure_stops_at_message_confirmed(session_factory, bus):
    await _seed_goods_a(session_factory)
    sender = FakeSender()
    consign = FakeConsign(ok=False, reason="mock 风控")
    handler = make_handler(session_factory, bus, sender=sender, consign=consign)

    await bus.publish(_paid_event())

    rec = await _get_record(session_factory, "1000001")
    assert rec.status == "message_confirmed"  # 卡密已送达，停在此态
    assert "平台确认发货失败" in rec.fail_reason
    # 卡密照常核销；订单停在已付款（待确认发货）
    async with session_factory() as db:
        item = await db.get(CardItem, rec.card_item_id)
        order = (
            await db.execute(select(TradeOrder).where(TradeOrder.order_id == "1000001"))
        ).scalar_one()
    assert item.status == 2
    assert order.order_status == 1


async def test_in_flight_guard(session_factory, bus):
    await _seed_goods_a(session_factory)
    sender = FakeSender()
    handler = make_handler(session_factory, bus, sender=sender)

    handler._in_flight.add("1000001")  # 模拟同单正在处理
    await bus.publish(_paid_event())
    assert sender.sent == []


# ==================== 兜底 heal ====================


async def test_heal_stuck_deliveries(session_factory):
    async with session_factory() as db:
        db.add(
            DeliveryRecord(
                order_id="9000001",
                account_id=1,
                delivery_mode="kami",
                status="message_sending",
                updated_at=datetime.now() - timedelta(minutes=60),
            )
        )
        db.add(
            DeliveryRecord(
                order_id="9000002",
                account_id=1,
                delivery_mode="kami",
                status="message_sending",  # 刚刚的，不处理
            )
        )
        await db.commit()

    healed = await heal_stuck_deliveries(session_factory, stuck_minutes=30)
    assert healed == 1
    rec = await _get_record(session_factory, "9000001")
    assert rec.status == "unknown"
    assert "人工核实" in rec.fail_reason
    rec2 = await _get_record(session_factory, "9000002")
    assert rec2.status == "message_sending"
