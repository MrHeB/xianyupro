"""Cookie 解析/格式化/合并工具（qr_login 与 refresher 共用）。

实现已下沉到 `app.core.cookies`（消除 api_client → auth 的循环依赖），
此处保留转发以兼容既有引用。
"""

from app.core.cookies import (
    CRITICAL_KEYS,
    format_cookie_text,
    merge_set_cookie_values,
    missing_critical_keys,
    parse_cookie_text,
)

__all__ = [
    "CRITICAL_KEYS",
    "format_cookie_text",
    "merge_set_cookie_values",
    "missing_critical_keys",
    "parse_cookie_text",
]
