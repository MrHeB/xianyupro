"""工作台概览统计（阶段 F2）：一次请求出全部分组统计。
按当前用户数据范围：admin 全量 / user 仅自己的账号（users/scoping）。
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.api.deps import DbSession
from app.models import (
    Account,
    AccountAuth,
    ChatMessage,
    DeliveryRecord,
    Goods,
    TradeOrder,
    User,
)
from app.users.deps import get_current_user
from app.users.scoping import visible_account_ids

router = APIRouter(prefix="/stats", tags=["stats"])


def _today_start() -> datetime:
    """本地时区今日 0 点（与 base.py 约定一致：Python naive 本地时钟）。"""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _scope(ids: set[int] | None, column):
    """按可见账号集合生成过滤条件；None=admin 全量。"""
    return column.in_(ids) if ids is not None else None


@router.get("/overview")
async def stats_overview(
    db: DbSession,
    user: User = Depends(get_current_user),
    account_id: int | None = None,
) -> dict:
    # account_id 只收窄可见集合，下面所有统计都走 _scope，不必各自再判断一次
    ids = await visible_account_ids(db, user)
    if account_id is not None:
        ids = {account_id} if (ids is None or account_id in ids) else set()

    # 账号健康
    acct_cond = _scope(ids, Account.id)
    accounts_total = await db.scalar(
        select(func.count(Account.id)).where(*([acct_cond] if acct_cond is not None else []))
    )
    accounts_enabled = await db.scalar(
        select(func.count(Account.id)).where(Account.status == 1, *([acct_cond] if acct_cond is not None else []))
    )
    cookie_ok = await db.scalar(
        select(func.count(AccountAuth.account_id))
        .join(Account, Account.id == AccountAuth.account_id)
        .where(AccountAuth.cookie_status == 1, *([acct_cond] if acct_cond is not None else []))
    )

    # 商品：待上架 = 本地 status 0；在售 = status 1
    goods_cond = _scope(ids, Goods.account_id)
    goods_pending = await db.scalar(
        select(func.count(Goods.id)).where(Goods.status == 0, *([goods_cond] if goods_cond is not None else []))
    )
    goods_onsale = await db.scalar(
        select(func.count(Goods.id)).where(Goods.status == 1, *([goods_cond] if goods_cond is not None else []))
    )
    # 采集池（§8.1）：待确认 = 未到 MAPPED 的采集件；已放行待上架 = MAPPED
    goods_to_confirm = await db.scalar(
        select(func.count(Goods.id)).where(
            Goods.pool_status.in_(["SOURCED", "DECIDED"]),
            *([goods_cond] if goods_cond is not None else []),
        )
    )
    goods_approved = await db.scalar(
        select(func.count(Goods.id)).where(
            Goods.pool_status == "MAPPED",
            *([goods_cond] if goods_cond is not None else []),
        )
    )

    # 订单：待发货 = order_status 2；今日付款 = paid_at >= 今日 0 点
    order_cond = _scope(ids, TradeOrder.account_id)
    orders_pending_ship = await db.scalar(
        select(func.count(TradeOrder.id)).where(
            TradeOrder.order_status == 2,
            *([order_cond] if order_cond is not None else []),
        )
    )
    orders_today = await db.scalar(
        select(func.count(TradeOrder.id)).where(
            TradeOrder.paid_at >= _today_start(),
            *([order_cond] if order_cond is not None else []),
        )
    )

    # 消息：今日收到的 IN 消息（message_time 为毫秒时间戳）
    msg_cond = _scope(ids, ChatMessage.account_id)
    msgs_today_in = await db.scalar(
        select(func.count(ChatMessage.id)).where(
            ChatMessage.direction == "IN",
            ChatMessage.message_time >= int(_today_start().timestamp() * 1000),
            *([msg_cond] if msg_cond is not None else []),
        )
    )

    # 发货：待处理 / 今日
    dv_cond = _scope(ids, DeliveryRecord.account_id)
    dv_pending = await db.scalar(
        select(func.count(DeliveryRecord.id)).where(
            DeliveryRecord.status == "pending",
            *([dv_cond] if dv_cond is not None else []),
        )
    )
    dv_today = await db.scalar(
        select(func.count(DeliveryRecord.id)).where(
            DeliveryRecord.created_at >= _today_start(),
            *([dv_cond] if dv_cond is not None else []),
        )
    )

    return {
        "accounts": {
            "total": accounts_total or 0,
            "enabled": accounts_enabled or 0,
            "cookie_ok": cookie_ok or 0,
        },
        "goods": {
            "pending_publish": goods_pending or 0,
            "on_sale": goods_onsale or 0,
            "to_confirm": goods_to_confirm or 0,
            "approved": goods_approved or 0,
        },
        "orders": {"pending_delivery": orders_pending_ship or 0, "today": orders_today or 0},
        "messages": {"today_in": msgs_today_in or 0},
        "delivery": {"pending": dv_pending or 0, "today": dv_today or 0},
    }