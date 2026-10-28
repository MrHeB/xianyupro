"""auth：统一会话池（多账号登录、Cookie 存取、自动续期）——阶段 4-1。"""

from app.auth.account_service import (
    load_accounts_into_pool,
    upsert_account_from_cookies,
)
from app.auth.cookie_crypto import decrypt, encrypt
from app.auth.cookies import (
    format_cookie_text,
    merge_set_cookie_values,
    parse_cookie_text,
)
from app.auth.qr_login import QrLoginService, QrLoginError, qr_login_service
from app.auth.refresher import CookieTokenRefresher
from app.auth.session_pool import AccountSession, SessionPool, session_pool

__all__ = [
    "AccountSession",
    "SessionPool",
    "session_pool",
    "encrypt",
    "decrypt",
    "QrLoginService",
    "QrLoginError",
    "qr_login_service",
    "CookieTokenRefresher",
    "load_accounts_into_pool",
    "upsert_account_from_cookies",
    "parse_cookie_text",
    "format_cookie_text",
    "merge_set_cookie_values",
]
