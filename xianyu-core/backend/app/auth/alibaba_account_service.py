"""1688 货源账号入库与取用（与闲鱼 account 体系完全隔离，见 models/alibaba_account.py 的理由）。"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.account_service import decrypt_cookie, encrypt_cookie
from app.core.cookies import format_cookie_text
from app.models import AlibabaAccount

logger = logging.getLogger(__name__)


class AlibabaAccountError(RuntimeError):
    """1688 货源账号操作错误。"""


async def upsert_alibaba_account_from_cookies(
    db: AsyncSession,
    cookies: dict[str, str],
    *,
    owner_user_id: int | None = None,
    nickname: str | None = None,
) -> AlibabaAccount:
    """按 unb upsert 1688 货源账号；软删过的行复活（同一会员重新扫码应恢复而非撞唯一键）。"""
    unb = (cookies.get("unb") or "").strip()
    if not unb:
        raise AlibabaAccountError("Cookie 缺少 unb，无法归属 1688 货源账号")

    row = (
        await db.execute(select(AlibabaAccount).where(AlibabaAccount.external_uid == unb))
    ).scalar_one_or_none()
    if row is None:
        row = AlibabaAccount(external_uid=unb, status=1)
        db.add(row)
    else:
        row.deleted = 0
        row.status = 1

    if owner_user_id is not None:
        if row.user_id is not None and row.user_id != owner_user_id:
            raise AlibabaAccountError("该 1688 货源账号已绑定其他登录用户，无法重复绑定")
        row.user_id = owner_user_id

    m_h5_tk = cookies.get("_m_h5_tk", "")
    row.encrypted_cookie = encrypt_cookie(format_cookie_text(cookies))
    row.encrypted_token = encrypt_cookie(m_h5_tk) if m_h5_tk else None
    row.cookie_status = 1
    row.last_login_at = datetime.now()
    if nickname:
        row.nickname = nickname

    await db.commit()
    await db.refresh(row)
    logger.info("1688 货源账号入库 unb=%s id=%d keys=%d", unb, row.id, len(cookies))
    return row


async def alibaba_cookie_text(db: AsyncSession, alibaba_account_id: int) -> str:
    """取解密后的 1688 Cookie 明文（供采集器带 Cookie 请求）。无账号/已停用则抛错。"""
    row = await db.get(AlibabaAccount, alibaba_account_id)
    if row is None or row.deleted:
        raise AlibabaAccountError(f"1688 货源账号不存在：{alibaba_account_id}")
    if row.status != 1:
        raise AlibabaAccountError(f"1688 货源账号已停用：{alibaba_account_id}")
    return decrypt_cookie(row.encrypted_cookie)


async def pick_usable_alibaba_account(
    db: AsyncSession, *, user_id: int | None = None
) -> AlibabaAccount | None:
    """挑一个登录态可用的货源账号（最近扫码优先）。

    只认 `cookie_status==1`：匿名打搜索页和详情页一样吃 x5sec 壳，与其跑完再报 blocked，
    不如在启动浏览器之前就说"没有可用账号"。`user_id=None` 表示不限归属（admin）。
    """
    stmt = select(AlibabaAccount).where(
        AlibabaAccount.deleted == 0,
        AlibabaAccount.status == 1,
        AlibabaAccount.cookie_status == 1,
    )
    if user_id is not None:
        stmt = stmt.where(AlibabaAccount.user_id == user_id)
    stmt = stmt.order_by(AlibabaAccount.last_login_at.desc()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()
