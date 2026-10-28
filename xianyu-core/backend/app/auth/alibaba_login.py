"""1688 货源账号扫码登录（Playwright 真浏览器）。

为什么不手写 havana HTTP 协议（这里曾经就是手写的）：
- query.do 在 CONFIRMED 时**一个 Set-Cookie 都不下发**，整个会话只剩 XSRF-TOKEN；
- 换票动作在 jump.htm 的 JS 里 —— 未登录分支是
  `location.href='https://login.taobao.com/jump?target=...'`，纯跟 Location 头跟不动；
- 登录主机也不是 passport.1688.com，而是 login.taobao.com，回跳链里还有
  marketSigninJump.htm 这一跳（它才把 .taobao.com 会话换成 1688 会话）。
入口 URL 抄自 github.com/zhangjiancong/MarketSpider 的 GetCookie.py；真浏览器实测
（scripts/spike_1688_browser_qr.py，2026-09-19）：扫码确认后 14s 内 .taobao.com 一次性
下发 unb / sgcookie / tracknick 等 21 个字段。reference/goofish-cli 的扫码登录同样是
Playwright 轮询 context.cookies()，而不是手写协议。

会话存进程内存（与闲鱼 qr_login.py 同一口径，单副本部署假设）。
"""

import asyncio
import base64
import logging
import secrets
import time
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

_DONE_URL = "https://www.1688.com/"
_MARKET_SIGNIN_JUMP = f"https://login.1688.com/member/marketSigninJump.htm?Done={quote(_DONE_URL, safe='')}"
# 三级回跳：login.1688.com/member/jump.htm → marketSigninJump.htm → www.1688.com
_SEED_1688_URL = f"https://login.1688.com/member/jump.htm?target={quote(_MARKET_SIGNIN_JUMP, safe='')}"
LOGIN_URL = (
    f"https://login.taobao.com/?redirect_url={quote(_SEED_1688_URL, safe='')}"
    "&style=tao_custom&from=1688web"
)

# 1688 登录态关键字段：unb（会员 ID）+ cookie2（会话）。
# cookie2 在匿名阶段就会下发，所以它单独不构成"已登录"证据，unb 才是。
ALIBABA_CRITICAL_KEYS = ("unb", "cookie2")

_QR_SELECTORS = (
    ".qrcode-login",
    ".qrcode-img",
    ".login-qrcode",
    "#qrcode",
)
_REFRESH_SELECTOR = "text=刷新二维码"
_SCANNED_HINTS = ("扫描成功", "已扫描", "请在手机上确认")

SESSION_TIMEOUT = 300
_MAX_SESSIONS = 20  # 每会话一个独立 context（隔离 Cookie），比纯内存会话重，别按 100 开
_PAGE_TIMEOUT = 45000


class AlibabaLoginError(RuntimeError):
    """1688 扫码登录流程错误。"""


def missing_alibaba_keys(cookies: dict[str, str]) -> list[str]:
    return [k for k in ALIBABA_CRITICAL_KEYS if not cookies.get(k)]


def domain_counts(records: list[dict]) -> dict[str, int]:
    """按域统计 Cookie 条数，只碰域名不碰值。

    判断 _seed_1688_domain 那步有没有真的把会话换成 1688 域：只有 .taobao.com
    的话，拿去打 detail.1688.com 依旧是匿名。
    """
    out: dict[str, int] = {}
    for c in records:
        domain = str(c.get("domain", ""))
        out[domain] = out.get(domain, 0) + 1
    return out


def cookies_from_context(records: list[dict]) -> dict[str, str]:
    """Playwright 的 CookieRecord 列表 → 扁平 name/value dict。

    同名 Cookie 会跨域重复（cookie2 / _tb_token_ 在 .taobao.com 和 .1688.com 各一份），
    而入库的是一行 Cookie 文本、只能留一份：这份要拿去打 detail.1688.com，
    所以让 .1688.com 的值覆盖 .taobao.com 的。
    """
    out: dict[str, str] = {}
    # 排序键让 .1688.com 排最后 → 遍历时后写、覆盖掉 .taobao.com 的同名值
    for c in sorted(records, key=lambda r: 1 if str(r.get("domain", "")).endswith("1688.com") else 0):
        name, value = c.get("name"), c.get("value")
        if name and value:
            out[name] = value
    return out


class AlibabaQrLoginService:
    """1688 货源账号扫码登录会话管理（每进程一个实例）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._browser_lock = asyncio.Lock()
        self._playwright = None
        self._browser = None

    # ---------- 浏览器 ----------

    async def _get_browser(self):
        """懒启动 headless chromium；会话共用浏览器、各自一个 context。

        playwright 延迟 import：单测环境不装它也能 import 本模块（同 goofish-cli 的做法）。
        """
        async with self._browser_lock:
            if self._browser is not None:
                return self._browser
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            return self._browser

    async def _wait_qr(self, page):
        """等扫码容器出现并返回句柄；候选选择器全落空就判页面改版。"""
        for sel in _QR_SELECTORS:
            try:
                return await page.wait_for_selector(sel, timeout=6000, state="visible")
            except Exception:
                continue
        raise AlibabaLoginError("1688 登录页没有出现扫码区（页面改版或风控拦截）")

    async def _qr_image(self, handle) -> str:
        """截**整个扫码容器**而不是容器里的 img。

        实测容器里第一个 img 是 1688 logo（截出来就是一张橙色 logo）；而且真出风控
        校验/失效提示时那些也在容器里，截容器才能把它们带给用户看。
        """
        png = await handle.screenshot()
        return "data:image/png;base64," + base64.b64encode(png).decode()

    async def _seed_1688_domain(self, page) -> None:
        """登录后显式走一遍回跳链，把 .1688.com 域会话种上。

        实测扫码完浏览器停在 login.taobao.com，不主动走这一趟就没有 1688 域 Cookie，
        之后打 detail.1688.com 仍是匿名。这步失败不影响已拿到的登录态，只记日志。
        """
        try:
            await page.goto(_SEED_1688_URL, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT)
            await page.wait_for_timeout(1500)
        except Exception as exc:
            logger.warning(
                "1688 域回跳失败，只拿到 .taobao.com 会话 errorType=%s error=%s",
                type(exc).__name__,
                str(exc)[:200],
            )

    async def _refresh_if_expired(self, page) -> bool:
        """二维码 60~90s 失效，页面上会出现"刷新二维码"，点了才撑得到会话超时。"""
        try:
            btn = await page.query_selector(_REFRESH_SELECTOR)
            if not btn:
                return False
            await btn.click()
            return True
        except Exception:
            return False

    async def _is_scanned(self, page) -> bool:
        try:
            text = await page.inner_text("body")
        except Exception:
            return False
        return any(hint in text for hint in _SCANNED_HINTS)

    # ---------- 公开 API ----------

    async def generate(self) -> dict:
        """创建扫码会话，返回 {session_id, qr_image}。"""
        await self._cleanup_expired()

        async with self._lock:
            if len(self._sessions) >= _MAX_SESSIONS:
                raise AlibabaLoginError("扫码会话容量已满，请稍后重试")
            session_id = secrets.token_urlsafe(32)

        context = None
        try:
            browser = await self._get_browser()
            # 每会话一个干净 context：复用带登录态的 context 会直接跳过扫码页
            context = await browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 860})
            page = await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT)
            qr_image = await self._qr_image(await self._wait_qr(page))
        except AlibabaLoginError:
            await self._close_context(context)
            raise
        except Exception as exc:
            await self._close_context(context)
            raise AlibabaLoginError(f"生成 1688 登录二维码失败: {type(exc).__name__}: {exc}") from exc

        async with self._lock:
            self._sessions[session_id] = {
                "created_at": time.time(),
                "status": "new",
                "context": context,
                "page": page,
                "seeded": False,
            }
        logger.info("1688 扫码会话已创建 sessionId=%s...", session_id[:8])
        return {"session_id": session_id, "qr_image": qr_image}

    async def poll(self, session_id: str) -> dict:
        """轮询一次；登录态齐了把 Cookie 存进会话，只返回脱敏摘要（绝不带 Cookie 值）。"""
        sdata = await self._get_session(session_id)
        if sdata is None:
            return {"status": "expired", "message": "会话不存在或已过期"}
        if time.time() - sdata["created_at"] > SESSION_TIMEOUT:
            await self.cleanup(session_id)
            return {"status": "expired", "message": "登录超时"}

        page, context = sdata["page"], sdata["context"]
        cookies = cookies_from_context(await context.cookies())
        if not missing_alibaba_keys(cookies):
            records = []
            if not sdata["seeded"]:
                sdata["seeded"] = True
                before = dict(cookies)
                await self._seed_1688_domain(page)
                records = await context.cookies()
                seeded = cookies_from_context(records)
                # 种跳后反而缺关键字段（1688 域覆盖出空值之类）就退回种跳前的那份
                cookies = before if missing_alibaba_keys(seeded) else seeded
            missing = missing_alibaba_keys(cookies)
            sdata["saved_cookies"] = cookies
            sdata["status"] = "confirmed"
            logger.info(
                "1688 扫码登录成功 missing=%s cookieDomains=%s cookieNames=%s",
                missing,
                domain_counts(records) or "未种跳",
                sorted(cookies),
            )
            return {
                "status": "confirmed",
                "missing": missing,
                "unb": cookies.get("unb", ""),
                "nickname": unquote(cookies.get("tracknick") or ""),
            }

        if await self._refresh_if_expired(page):
            logger.info("1688 二维码已失效，已自动刷新 sessionId=%s...", session_id[:8])
            return {"status": "new"}
        sdata["status"] = "scaned" if await self._is_scanned(page) else "new"
        return {"status": sdata["status"]}

    async def take_cookies(self, session_id: str) -> dict | None:
        """取走 confirmed 会话的完整 Cookie（供入库）并清理会话。二维码只能确认一次。"""
        sdata = await self._get_session(session_id)
        if sdata is None:
            return None
        cookies = sdata.get("saved_cookies")
        await self.cleanup(session_id)
        return cookies

    async def cleanup(self, session_id: str) -> None:
        async with self._lock:
            sdata = self._sessions.pop(session_id, None)
        if sdata:
            await self._close_context(sdata.get("context"))

    async def _close_context(self, context) -> None:
        if context is None:
            return
        try:
            await context.close()
        except Exception as exc:
            logger.warning("关闭 1688 扫码 context 失败 errorType=%s", type(exc).__name__)

    async def _get_session(self, session_id: str) -> dict | None:
        if not session_id or len(session_id) < 32:
            return None
        async with self._lock:
            return self._sessions.get(session_id)

    async def _cleanup_expired(self) -> None:
        now = time.time()
        for sid in [s for s, v in self._sessions.items() if now - v["created_at"] > SESSION_TIMEOUT]:
            await self.cleanup(sid)

    async def aclose(self) -> None:
        for sid in list(self._sessions):
            await self.cleanup(sid)
        async with self._browser_lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None


# 全局单例（FastAPI lifespan 管理生命周期）
alibaba_qr_login_service = AlibabaQrLoginService()
