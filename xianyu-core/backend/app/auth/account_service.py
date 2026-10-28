"""账号持久化：扫码确认后的 Cookie → 加密入库 → 注册会话池（阶段 4-1）。"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import cookie_crypto
from app.auth.cookies import format_cookie_text, parse_cookie_text
from app.auth.session_pool import session_pool
from app.core.config import settings
from app.models import Account, AccountAuth, AccountRuntime

logger = logging.getLogger(__name__)


class AccountServiceError(RuntimeError):
    pass


def encrypt_cookie(cookie_text: str) -> str:
    return cookie_crypto.encrypt(cookie_text, settings.cookie_crypto_secret)


def decrypt_cookie(encrypted: str) -> str:
    return cookie_crypto.decrypt(encrypted, settings.cookie_crypto_secret)


async def upsert_account_from_cookies(
    db: AsyncSession, cookies: dict[str, str], *, owner_user_id: int | None = None
) -> Account:
    """按 unb upsert 账号 + 凭据 + 运行时，并注册进会话池。

    - account 不存在则创建（status=1 启用）；
    - account_auth 存 AES-GCM 密文（encrypted_cookie / encrypted_token=_m_h5_tk）；
    - owner_user_id：多人绑定归属（阶段 F1）。传值时把账号挂到该用户；
      已被其他用户绑定则抛 AccountServiceError 防越权；None 不修改归属。
    - 返回 Account。
    """
    unb = (cookies.get("unb") or "").strip()
    if not unb:
        raise AccountServiceError("Cookie 缺少 unb，无法归属账号")

    cookie_text = format_cookie_text(cookies)
    m_h5_tk = cookies.get("_m_h5_tk", "")

    account = (
        await db.execute(select(Account).where(Account.external_uid == unb))
    ).scalar_one_or_none()
    if account is None:
        account = Account(external_uid=unb, status=1)
        db.add(account)
        await db.flush()
        logger.info("新账号入库 unb=%s accountId=%d", unb, account.id)

    # 归属绑定 + 防越权（一人一闲鱼号 → 一个账号只能属于一个登录用户）
    if owner_user_id is not None:
        if account.user_id is not None and account.user_id != owner_user_id:
            raise AccountServiceError("该闲鱼号已绑定其他登录用户，无法重复绑定")
        account.user_id = owner_user_id

    auth = (
        await db.execute(select(AccountAuth).where(AccountAuth.account_id == account.id))
    ).scalar_one_or_none()
    if auth is None:
        auth = AccountAuth(account_id=account.id)
        db.add(auth)

    auth.encrypted_cookie = encrypt_cookie(cookie_text)
    auth.encrypted_token = encrypt_cookie(m_h5_tk) if m_h5_tk else None
    auth.cookie_status = 1  # 扫码成功即视为正常
    auth.last_login_at = datetime.now()

    runtime = (
        await db.execute(
            select(AccountRuntime).where(AccountRuntime.account_id == account.id)
        )
    ).scalar_one_or_none()
    if runtime is None:
        db.add(AccountRuntime(account_id=account.id))

    await db.commit()

    # 注册进内存会话池（持有明文 Cookie 供 api_client 使用）
    session_pool.register(account.id, unb, cookie_text)
    logger.info("账号凭据已加密入库并注册会话池 accountId=%d", account.id)
    return account


async def load_accounts_into_pool(db: AsyncSession) -> list[int]:
    """启动时从 DB 恢复所有 cookie_status=1 账号到会话池。

    返回恢复的 account_id 列表。解密失败（密钥换了）的账号跳过并置 cookie_status=2。
    """
    rows = (
        await db.execute(
            select(Account, AccountAuth)
            .join(AccountAuth, AccountAuth.account_id == Account.id)
            .where(Account.status == 1, AccountAuth.cookie_status == 1)
        )
    ).all()

    restored: list[int] = []
    for account, auth in rows:
        try:
            cookie_text = decrypt_cookie(auth.encrypted_cookie)
        except Exception:
            logger.warning("Cookie 解密失败（密钥变更？）accountId=%d，标记过期", account.id)
            auth.cookie_status = 2
            continue
        session_pool.register(account.id, account.external_uid, cookie_text)
        restored.append(account.id)
    await db.commit()
    logger.info("会话池恢复完成：%d 个账号", len(restored))
    return restored


def cookie_text_of(cookie_dict: dict[str, str]) -> str:
    return format_cookie_text(cookie_dict)


def cookie_dict_of(cookie_text: str) -> dict[str, str]:
    return parse_cookie_text(cookie_text)
