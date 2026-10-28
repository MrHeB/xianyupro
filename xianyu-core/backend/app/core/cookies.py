"""Cookie 解析/格式化/合并工具（qr_login 与 refresher 共用）。

为什么需要手动合并 Set-Cookie：httpx 的 CookieJar 按 domain/path 存 Cookie，
闲鱼 CONFIRMED 时下发的 Set-Cookie domain 多为 .taobao.com/.alibaba.com，
与请求 host（passport.goofish.com）不匹配时会丢 havana_lgc2_77 等核心登录态字段，
导致 hasLogin 返回 SESSION_EXPIRED（参考 xianyu-pilot-pro 同款坑的修复）。
"""

CRITICAL_KEYS = ("havana_lgc2_77", "_hvn_lgc_", "havana_lgc_exp", "unb", "_m_h5_tk")


def parse_cookie_text(text: str) -> dict[str, str]:
    """Cookie 字符串 → dict（"k=v; k2=v2"）。"""
    result: dict[str, str] = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k:
                result[k] = v.strip()
    return result


def format_cookie_text(cookies: dict[str, str]) -> str:
    """dict → Cookie 字符串。"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def merge_set_cookie_values(base: dict[str, str], set_cookie_values: list[str]) -> list[str]:
    """把响应 Set-Cookie 头合并进 base（原地修改），返回新增/变化的 key 列表。

    每个 Set-Cookie 形如 "key=value; Path=/; Domain=.taobao.com; ..."，取第一段。
    """
    changed: list[str] = []
    for sc in set_cookie_values:
        first = sc.split(";")[0].strip()
        if "=" not in first:
            continue
        k, v = first.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        if base.get(k) != v:
            base[k] = v
            changed.append(k)
    return changed


def missing_critical_keys(cookies: dict[str, str]) -> list[str]:
    """诊断：缺少哪些关键登录态字段（缺失可能导致 hasLogin 假性 SESSION_EXPIRED）。"""
    return [k for k in CRITICAL_KEYS if k not in cookies]
