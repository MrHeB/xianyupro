"""订单接口（阶段 4-5）：只读列表；数据来源=WS 付款事件落库 + order_sync 定时补齐。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.deps import DbSession
from app.models import TradeOrder, User
from app.users.deps import get_current_user
from app.users.scoping import visible_account_ids

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("")
async def list_orders(
    db: DbSession,
    user: User = Depends(get_current_user),
    account_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    """订单列表（最近优先），可按账号过滤。金额/状态由 order_sync 调度任务补齐。"""
    ids = await visible_account_ids(db, user)
    stmt = select(TradeOrder)
    if ids is not None:
        stmt = stmt.where(TradeOrder.account_id.in_(ids))
        if account_id is not None and account_id not in ids:
            return []
    if account_id is not None:
        stmt = stmt.where(TradeOrder.account_id == account_id)
    stmt = stmt.order_by(TradeOrder.id.desc()).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": o.id,
            "account_id": o.account_id,
            "order_id": o.order_id,
            "external_order_id": o.external_order_id,
            "goods_id": o.goods_id,
            "category": o.category,
            "buyer_id": o.buyer_id,
            "buyer_name": o.buyer_name,
            "total_amount_cent": o.total_amount_cent,
            "order_status": o.order_status,
            "paid_at": o.paid_at,
        }
        for o in rows
    ]
