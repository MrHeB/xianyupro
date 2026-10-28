"""卡密库存：原子认领 + 状态回滚（品类 A，阶段 4-4 移植）。

原子认领（xianyu-pilot 已验证，蓝本 automation_runtime._execute_kami_delivery）：
    UPDATE card_item SET status = 1, used_order_id = :order_id
    WHERE id = (SELECT id FROM (SELECT id FROM card_item
               WHERE group_id = :gid AND status = 0 ORDER BY id LIMIT 1) t)
子查询必须嵌套一层派生表——MySQL 禁止 UPDATE 目标表直接出现在 FROM 里，
派生表会被物化，从而绕开限制；SQLite 原生支持，两边兼容。

状态语义：0 未用 / 1 已认领 / 2 已用。
发货成功 → status=2；**明确**发送失败 → 回滚 status=0；
发送结果不确定（超时/异常）→ 保持 1，等人工核实（防重发哲学）。
"""

import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CardGroup, CardItem

logger = logging.getLogger(__name__)


@dataclass
class ClaimedCard:
    card_item_id: int
    group_id: int
    content: str  # "卡号----卡值"（卡值为空则只发卡号）


async def claim_card(db: AsyncSession, group_id: int, order_key: str) -> ClaimedCard | None:
    """原子认领一张未用卡密。组内无卡返回 None（调用方触发低库存告警）。

    order_key 是幂等键（真实 order_id 或 pnm- 合成键），只写进 used_order_id
    做追溯，认领的原子性完全靠 UPDATE 的行锁。
    """
    inner = (
        select(CardItem.id)
        .where(CardItem.group_id == group_id, CardItem.status == 0)
        .order_by(CardItem.id)
        .limit(1)
        .subquery()
    )
    stmt = (
        update(CardItem)
        .where(CardItem.id == select(inner.c.id).scalar_subquery())
        .values(status=1, used_order_id=order_key)
    )
    result = await db.execute(stmt)
    if (result.rowcount or 0) == 0:
        await db.commit()
        return None

    # 同 order_key 重试时可能残留旧的 status=1 卡（unknown 人工核实场景），取最新一张
    card = (
        (
            await db.execute(
                select(CardItem)
                .where(CardItem.used_order_id == order_key, CardItem.status == 1)
                .order_by(CardItem.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    await db.commit()
    return ClaimedCard(
        card_item_id=card.id,
        group_id=group_id,
        content=f"{card.card_key}----{card.card_value}" if card.card_value else card.card_key,
    )


async def mark_used(db: AsyncSession, card_item_id: int) -> None:
    """发货成功 → status=2，并同步 card_group.used_count 冗余计数。"""
    card = await db.get(CardItem, card_item_id)
    card.status = 2
    await db.execute(
        update(CardGroup)
        .where(CardGroup.id == card.group_id)
        .values(used_count=CardGroup.used_count + 1)
    )
    await db.commit()


async def rollback(db: AsyncSession, card_item_id: int) -> None:
    """**明确**发送失败 → 回滚 status=0（结果不确定时严禁调用，见模块 docstring）。"""
    card = await db.get(CardItem, card_item_id)
    card.status = 0
    card.used_order_id = None
    await db.commit()


async def remaining_count(db: AsyncSession, group_id: int) -> int:
    return (
        await db.execute(
            select(func.count(CardItem.id)).where(
                CardItem.group_id == group_id, CardItem.status == 0
            )
        )
    ).scalar_one()


async def check_low_stock(db: AsyncSession, group_id: int) -> bool:
    """认领后调用：剩余量 ≤ 告警阈值则记 warning（前端另可轮询 /delivery/cards）。"""
    group = await db.get(CardGroup, group_id)
    if group is None:
        return False
    remaining = await remaining_count(db, group_id)
    if remaining <= group.alert_threshold:
        logger.warning(
            "卡密低库存 groupId=%d name=%s remaining=%d threshold=%d",
            group_id, group.name, remaining, group.alert_threshold,
        )
        return True
    return False
