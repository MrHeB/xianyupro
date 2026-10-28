"""首次启动种子：sys_user 空表时创建默认管理员（config.admin_seed_*）。"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.users.security import hash_password

logger = logging.getLogger(__name__)


async def ensure_seed_admin(db: AsyncSession) -> None:
    """sys_user 表为空且配置了种子名时，创建 admin（登录后请立即改密）。"""
    from app.core.config import settings

    if not settings.admin_seed_username:
        return
    exists = await db.scalar(select(User.id).limit(1))
    if exists is not None:
        return
    admin = User(
        username=settings.admin_seed_username,
        password_hash=hash_password(settings.admin_seed_password),
        role="admin",
        remark="seed 默认管理员（请立即改密）",
    )
    db.add(admin)
    await db.commit()
    logger.warning("已创建默认管理员 %r（默认密码来自 config.admin_seed_password）", admin.username)