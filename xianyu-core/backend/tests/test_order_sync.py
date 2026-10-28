"""订单同步测试：FakeMtop 返回 sold.get 订单页 → TradeOrder upsert。

覆盖：新增（金额/状态/买家/商品关联/paid_at）、WS 先落库后同步只补空缺、
重复同步幂等、翻页（lastEndRow 游标 + 满页续拉/不满页停）、
字段防御（缺金额/认不出的状态不炸）。
"""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Goods, TradeOrder
from app.orders.sync import (
    _extract_amount_cent,
    _extract_status,
    sync_account_orders,
)


class FakeMtop:
    """假 mtop 客户端：按 (api, pageNumber) 返回预置页面。"""

    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.calls: list[dict] = []

    async def call(self, api: str, version: str, data: dict, *, is_write: bool = False) -> dict:
        self.calls.append({"api": api, "version": version, "data": data})
        page_no = data["pageNumber"]
        if page_no <= len(self.pages):
            return self.pages[page_no - 1]
        return {}  # 空页


class FakeSession:
    """假 AccountSession（auth 会话池的最小面）。"""

    account_id = 1
    external_uid = "unb-123"

    def __init__(self, mtop):
        self.mtop = mtop


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


def _order_item(**overrides) -> dict:
    item = {
        "orderId": "9000001",
        "itemId": "xy-goods-1",
        "buyerUserId": "buyer001",
        "buyerNick": "买家小明",
        "totalAmount": 19.9,
        "orderStatusText": "已付款，等待发货",
    }
    item.update(overrides)
    return item


def _page(items: list[dict]) -> dict:
    return {"orderList": items}


async def _all_orders(session_factory) -> list[TradeOrder]:
    async with session_factory() as db:
        return list((await db.execute(select(TradeOrder))).scalars().all())


async def _get_order(session_factory, order_id: str) -> TradeOrder | None:
    async with session_factory() as db:
        return (
            await db.execute(select(TradeOrder).where(TradeOrder.order_id == order_id))
        ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_sync_creates_orders_with_amount_status_and_goods_link(session_factory):
    """新增：金额 元→分、状态文案映射、商品按 external_goods_id 关联。"""
    async with session_factory() as db:
        db.add(Goods(account_id=1, external_goods_id="xy-goods-1", category="A",
                     title="测试商品", price_cent=1990))
        await db.commit()

    mtop = FakeMtop([_page([_order_item()])])
    async with session_factory() as db:
        result = await sync_account_orders(db, FakeSession(mtop))

    assert result == {"synced": 1, "updated": 0}
    order = await _get_order(session_factory, "9000001")
    assert order is not None
    assert order.total_amount_cent == 1990
    assert order.order_status == 2  # "已付款，等待发货" 命中 待发货
    assert order.buyer_id == "buyer001"
    assert order.buyer_name == "买家小明"
    assert order.paid_at is not None
    # 商品关联回填
    async with session_factory() as db:
        goods_id = (
            await db.execute(select(Goods.id).where(Goods.external_goods_id == "xy-goods-1"))
        ).scalar_one()
    assert order.goods_id == goods_id
    assert order.category == "A"
    # 请求参数：userId=unb、翻页游标首页不传
    assert mtop.calls[0]["data"]["userId"] == "unb-123"
    assert "lastEndRow" not in mtop.calls[0]["data"]


@pytest.mark.asyncio
async def test_sync_fills_gaps_of_existing_order_without_rollback(session_factory):
    """WS 先落库（金额 0）→ 同步只补空缺，不回退已有信息。"""
    async with session_factory() as db:
        db.add(Goods(account_id=1, external_goods_id="xy-goods-1", category="A",
                     title="测试商品", price_cent=1990))
        db.add(TradeOrder(account_id=1, order_id="9000001", category="A",
                          buyer_name="付款事件里的名字", total_amount_cent=0,
                          order_status=1))
        await db.commit()

    mtop = FakeMtop([_page([_order_item()])])
    async with session_factory() as db:
        result = await sync_account_orders(db, FakeSession(mtop))

    assert result == {"synced": 0, "updated": 1}
    order = await _get_order(session_factory, "9000001")
    assert order.total_amount_cent == 1990  # 补齐
    assert order.order_status == 2  # 状态以同步为准
    assert order.buyer_name == "付款事件里的名字"  # 已有信息不覆盖
    assert order.buyer_id == "buyer001"  # 空缺补上


@pytest.mark.asyncio
async def test_sync_idempotent_on_rerun(session_factory):
    """重复同步幂等：不新增行、不重复计数。"""
    mtop = FakeMtop([_page([_order_item()])])
    async with session_factory() as db:
        await sync_account_orders(db, FakeSession(mtop))
    mtop2 = FakeMtop([_page([_order_item()])])
    async with session_factory() as db:
        result = await sync_account_orders(db, FakeSession(mtop2))

    assert result == {"synced": 0, "updated": 1}
    orders = await _all_orders(session_factory)
    assert len(orders) == 1


@pytest.mark.asyncio
async def test_sync_paginates_until_short_page(session_factory):
    """翻页：满页续拉并带 lastEndRow 游标；不满页停。"""
    full_page = _page([_order_item(orderId=f"9{i:07d}") for i in range(20)])
    short_page = _page([_order_item(orderId="9final0")])
    mtop = FakeMtop([full_page, short_page])

    async with session_factory() as db:
        result = await sync_account_orders(db, FakeSession(mtop))

    assert result["synced"] == 21
    assert len(mtop.calls) == 2
    # 第 2 页带游标（本页最后一条的订单号）
    assert mtop.calls[1]["data"]["lastEndRow"] == "90000019"
    orders = await _all_orders(session_factory)
    assert len(orders) == 21


@pytest.mark.asyncio
async def test_sync_skips_items_without_order_id(session_factory):
    """防御：无订单号的条目跳过不炸。"""
    mtop = FakeMtop([_page([{"itemId": "xy-goods-1"}, _order_item()])])
    async with session_factory() as db:
        result = await sync_account_orders(db, FakeSession(mtop))

    assert result == {"synced": 1, "updated": 0}
    orders = await _all_orders(session_factory)
    assert len(orders) == 1


def test_extract_amount_cent_prefers_cent_keys():
    assert _extract_amount_cent({"totalAmountCent": 12345}) == 12345
    assert _extract_amount_cent({"totalAmount": 19.9}) == 1990
    assert _extract_amount_cent({"totalAmount": "abc"}) is None
    assert _extract_amount_cent({}) is None


def test_extract_status_maps_known_text_only():
    assert _extract_status({"orderStatusText": "交易成功"}) == 4
    assert _extract_status({"orderStatusText": "退款成功"}) == 5
    assert _extract_status({"orderStatusText": "看不懂的文案"}) is None
    assert _extract_status({}) is None
