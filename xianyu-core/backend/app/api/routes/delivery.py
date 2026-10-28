"""发货接口（品类 A 卡密发货记录与配置 / 品类 B 人工履约订单留痕）。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import DbSession
from app.models import User
from app.users.deps import get_current_user
from app.users.scoping import ensure_account_scope, visible_account_ids

router = APIRouter(prefix="/delivery", tags=["delivery"])


# ---------- 读 ----------


@router.get("/records")
async def list_delivery_records(
    db: DbSession,
    user: User = Depends(get_current_user),
    account_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    from sqlalchemy import select

    from app.models import DeliveryRecord

    ids = await visible_account_ids(db, user)
    stmt = select(DeliveryRecord)
    if ids is not None:
        stmt = stmt.where(DeliveryRecord.account_id.in_(ids))
        if account_id is not None and account_id not in ids:
            return []
    if account_id is not None:
        stmt = stmt.where(DeliveryRecord.account_id == account_id)
    stmt = stmt.order_by(DeliveryRecord.id.desc()).limit(min(limit, 200))
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "account_id": r.account_id,
            "order_id": r.order_id,
            "delivery_mode": r.delivery_mode,
            "status": r.status,
            "card_item_id": r.card_item_id,
            "fail_reason": r.fail_reason,
            "receiver_info": r.receiver_info,
        }
        for r in rows
    ]


@router.get("/cards")
async def list_card_groups(
    db: DbSession,
    user: User = Depends(get_current_user),
    account_id: int | None = None,
) -> list[dict]:
    """卡密组库存（剩余量 = total - used，低于阈值告警）。

    卡密组只挂 goods 不挂账号，所以按账号过滤和收敛可见域都走 goods join；
    未绑定商品的组不属于任何账号，任何范围都可见（否则切账号时会"库存凭空消失"）。
    """
    from sqlalchemy import func, or_, select

    from app.models import CardGroup, CardItem, Goods

    stmt = select(CardGroup, Goods.account_id).outerjoin(Goods, Goods.id == CardGroup.goods_id)
    ids = await visible_account_ids(db, user)
    if ids is not None:
        stmt = stmt.where(or_(Goods.id.is_(None), Goods.account_id.in_(ids)))
        if account_id is not None and account_id not in ids:
            return []
    if account_id is not None:
        stmt = stmt.where(or_(Goods.id.is_(None), Goods.account_id == account_id))
    rows = (await db.execute(stmt)).all()

    result = []
    for group, group_account_id in rows:
        available = (
            await db.execute(
                select(func.count(CardItem.id)).where(
                    CardItem.group_id == group.id, CardItem.status == 0
                )
            )
        ).scalar_one()
        result.append(
            {
                "id": group.id,
                "account_id": group_account_id,
                "name": group.name,
                "goods_id": group.goods_id,
                "total_count": group.total_count,
                "used_count": group.used_count,
                "available": available,
                "low_stock": available <= group.alert_threshold,
            }
        )
    return result


# ---------- 写（实测配置用） ----------


class CardGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    goods_id: int | None = None
    alert_threshold: int = 5


class CardItemsIn(BaseModel):
    """批量导入卡密：每行 "卡号----卡值"（卡值可省略），空行/注释行忽略。"""

    lines: str = Field(min_length=1, max_length=200_000)


class DeliveryRuleIn(BaseModel):
    account_id: int
    goods_id: int | None = None  # 空 = 账号级默认规则
    delivery_mode: str = "kami"   # 当前仅 kami
    trigger_on_pay: int = 1
    max_delivery_per_day: int = 200


@router.post("/card-groups")
async def create_card_group(
    body: CardGroupIn, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    from sqlalchemy import select

    from app.models import CardGroup

    exists = (
        await db.execute(select(CardGroup.id).where(CardGroup.name == body.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(400, f"卡密组名已存在：{body.name}")
    group = CardGroup(
        name=body.name, goods_id=body.goods_id, alert_threshold=body.alert_threshold
    )
    db.add(group)
    await db.commit()
    return {"id": group.id, "name": group.name}


@router.post("/card-groups/{group_id}/items")
async def import_card_items(
    group_id: int, body: CardItemsIn, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """批量导入卡密（重复卡号跳过），并同步组 total_count。"""
    from sqlalchemy import func, select

    from app.models import CardGroup, CardItem

    group = await db.get(CardGroup, group_id)
    if group is None:
        raise HTTPException(404, f"卡密组不存在：{group_id}")

    existing_keys = set(
        (await db.execute(select(CardItem.card_key).where(CardItem.group_id == group_id)))
        .scalars()
        .all()
    )
    added = 0
    for raw in body.lines.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("----")
        key, value = key.strip(), value.strip()
        if not key or key in existing_keys:
            continue
        db.add(CardItem(group_id=group_id, card_key=key, card_value=value or None))
        existing_keys.add(key)
        added += 1
    await db.flush()

    total = (
        await db.execute(
            select(func.count(CardItem.id)).where(CardItem.group_id == group_id)
        )
    ).scalar_one()
    group.total_count = total
    await db.commit()
    return {"added": added, "total_count": total}


@router.post("/rules")
async def upsert_delivery_rule(
    body: DeliveryRuleIn, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """建/改发货规则（同账号+同商品只保留一条：先查后更，无则插入）。"""
    from sqlalchemy import select

    from app.models import DeliveryRule

    await ensure_account_scope(db, user, body.account_id)
    stmt = select(DeliveryRule).where(
        DeliveryRule.account_id == body.account_id,
        DeliveryRule.goods_id.is_(None) if body.goods_id is None else (DeliveryRule.goods_id == body.goods_id),
    )
    rule = (await db.execute(stmt)).scalar_one_or_none()
    if rule is None:
        rule = DeliveryRule(account_id=body.account_id, goods_id=body.goods_id)
        db.add(rule)
    rule.delivery_mode = body.delivery_mode
    rule.trigger_on_pay = body.trigger_on_pay
    rule.max_delivery_per_day = body.max_delivery_per_day
    await db.commit()
    return {"id": rule.id, "account_id": rule.account_id, "goods_id": rule.goods_id}
