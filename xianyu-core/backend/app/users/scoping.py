"""数据可见范围：admin 全量 / user 仅自己归属的账号。

多租户语境（阶段 F1）：所有业务表都挂 account_id，账号归属由 account.user_id 决定。
- admin：全部账号可见（运营视角）；
- user（一个人多个小号）：仅自己绑定的账号。
"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account

NOT_OWNED = HTTPException(status_code=403, detail="该数据不属于当前登录用户")


async def visible_account_ids(db: AsyncSession, user) -> set[int] | None:
    """返回当前用户可见的账号 id 集合；admin 返回 None 表示全量。"""
    if user.role == "admin":
        return None
    rows = await db.execute(select(Account.id).where(Account.user_id == user.id))
    return {row[0] for row in rows.all()}


async def ensure_account_scope(db: AsyncSession, user, account_id: int) -> None:
    """校验 account_id 对当前用户可见（写操作 / 单对象访问用）。"""
    ids = await visible_account_ids(db, user)
    if ids is None:
        return
    if account_id not in ids:
        raise NOT_OWNED