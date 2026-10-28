"""Cookie 保活与自动续期（蓝本：xianyu-pilot-pro cookie_token_refresher.py）。

三轨刷新调度（单 dispatcher 60s tick，每账号独立随机间隔）：
- Cookie 保活：30 分钟一次，POST passport.goofish.com/newlogin/hasLogin.do，
  成功后从响应 Set-Cookie 合并新 Cookie 回写 DB 与会话池；
- _m_h5_tk 刷新：1.5-2.5 小时随机，三步刷新（GET → 空 token POST → 真实 token POST）；
- ws_token 刷新：10-14 小时——阶段 4-3（messaging）接入 WS 后启用。

错误分级（关键：RGV587 不误标失效）：
- RGV587/滑块 → 只跳过本轮，不置 cookie_status=0；
- SESSION_EXPIRED（hasLogin 明确报登录态失效）→ **先补一次只读 mtop 复核**，
  复核也判失效才置 cookie_status=0 + 发 COOKIE_EXPIRED 事件；
- 已被标 0 的号不再走 hasLogin（它正是误标来源），每 DEAD_PROBE_INTERVAL 只读复核一次，
  复核通过就自动复位 cookie_status=1 并重挂会话池；
- 免密续期（Playwright "快速进入"）→ 浏览器自动化，阶段 4 后期按需实现。
"""

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from app.auth.account_service import decrypt_cookie, encrypt_cookie
from app.auth.cookies import format_cookie_text, merge_set_cookie_values, parse_cookie_text
from app.core.config import settings
from app.messaging.events import Event, EventType, event_bus
from app.models import Account, AccountAuth

logger = logging.getLogger(__name__)

HAS_LOGIN_URL = "https://passport.goofish.com/newlogin/hasLogin.do"
_MH5TK_API = "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get"
_MH5TK_URL = f"{settings.h5_api_base}/{_MH5TK_API}/1.0/"

COOKIE_KEEPALIVE_INTERVAL = 30 * 60          # Cookie 保活 30 分钟
MH5TK_REFRESH_MIN = 1.5 * 3600               # _m_h5_tk 刷新下限
MH5TK_REFRESH_MAX = 2.5 * 3600               # _m_h5_tk 刷新上限
TICK_INTERVAL = 60                            # dispatcher tick
ACCOUNT_JITTER = (2.0, 5.0)                  # 账号间随机间隔，避免齐发
DEAD_PROBE_INTERVAL = 15 * 60                # 已标失效的号：只读复核频率（别永久排除）

# 登录态复核探针：IM 的"当前登录用户"，只读、必须带登录态、体积极小（WS 侧本就 600s 打一次）
PROBE_API = "mtop.taobao.idlemessage.pc.loginuser.get"
PROBE_VERSION = "1.0"

# 风控特征：hasLogin 触发滑块 ≠ Cookie 失效，只跳过本轮（勿误标）
RISK_KEYWORDS = ("RGV587_ERROR", "FAIL_SYS_RGV587_ERROR", "被挤爆啦", "FAIL_SYS_USER_VALIDATE")

_H_COMMON = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://passport.goofish.com/",
    "Origin": "https://passport.goofish.com",
}


# ==================== 纯函数（可单测） ====================


def build_has_login_form(cookie_dict: dict[str, str], unb: str) -> dict:
    """构造 hasLogin.do 保活表单（字段参考 xianyu-pilot-pro CookieRefreshServiceImpl）。"""
    return {
        "appName": "xianyu",
        "fromSite": "77",
        "hid": unb,
        "ltl": "true",
        "appEntrance": "web",
        "_csrf_token": cookie_dict.get("XSRF-TOKEN", ""),
        "umidToken": "",
        "hsiz": cookie_dict.get("cookie2", ""),
        "bizParams": "taobaoBizLoginFrom=web",
        "mainPage": "false",
        "isMobile": "false",
        "lang": "zh_CN",
        "returnUrl": "",
        "isIframe": "true",
        "documentReferer": "https://www.goofish.com/",
        "defaultView": "hasLogin",
        "umidTag": "SERVER",
        "deviceId": cookie_dict.get("cna", ""),
    }


def is_has_login_confirmed(body_text: str) -> bool:
    """hasLogin 响应是否明确确认登录有效（新旧两种格式兼容）。

    旧格式 {"hasLogin": true}；新格式 {"content": {"data": {"hasLogin": true}}}。
    """
    try:
        payload = json.loads(body_text)
    except (ValueError, TypeError):
        return False
    if "hasLogin" in payload:
        return payload.get("hasLogin") is True
    data = payload.get("data")
    if isinstance(data, dict) and "hasLogin" in data:
        return data.get("hasLogin") is True
    content = payload.get("content")
    if isinstance(content, dict):
        cdata = content.get("data")
        if isinstance(cdata, dict) and "hasLogin" in cdata:
            return cdata.get("hasLogin") is True
    return payload.get("success") is True


def is_cookie_expired_response(body_text: str) -> bool:
    """hasLogin 回的是"登录态已失效"类文案。

    ⚠️ **这不是终态判据**：hasLogin 是 passport 侧网页接口，登录态仍可用时它也会回这句
    （2026-09-20 真机：11:32:51 同一份 Cookie 刚发布成功，11:34:13 就被它标 0）。
    置 0 前必须过 `CookieTokenRefresher.probe_session_alive` 的只读复核。
    """
    return "登录态已失效" in (body_text or "") or "请重新登录" in (body_text or "")


def is_risk_response(body_text: str) -> bool:
    return any(kw in (body_text or "") for kw in RISK_KEYWORDS)


# ==================== 调度器 ====================


class KeepaliveResult:
    OK = "ok"
    RISK = "risk"            # 滑块/风控：跳过，不误标
    EXPIRED = "expired"      # 登录态失效（还要只读复核确认）
    UNCONFIRMED = "unconfirmed"
    ERROR = "error"


class ProbeResult:
    """一次只读 mtop 复核的结论。UNDETERMINED 一律**不动** cookie_status。"""

    ALIVE = "alive"
    EXPIRED = "expired"
    UNDETERMINED = "undetermined"


@dataclass
class AccountRefreshState:
    account_id: int
    next_cookie_keepalive: float = 0.0
    next_mh5tk_refresh: float = 0.0
    last_error: str = ""


class CookieTokenRefresher:
    """常驻保活调度器（lifespan 启动，每 60s tick）。"""

    def __init__(self, session_factory):
        self._session_factory = session_factory  # async_sessionmaker
        self._states: dict[int, AccountRefreshState] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._running = False
        self._task: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="cookie-refresher")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("保活 tick 异常")
            await asyncio.sleep(TICK_INTERVAL)

    # ---------- 每轮调度 ----------

    async def _tick(self) -> None:
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(Account, AccountAuth)
                    .join(AccountAuth, AccountAuth.account_id == Account.id)
                    .where(Account.status == 1, AccountAuth.cookie_status.in_((0, 1)))
                )
            ).all()

        now = time.time()
        for account, auth in rows:
            state = self._states.get(account.id)
            if state is None:
                # 新账号：立即排一次保活；_m_h5_tk 延后随机排。
                # 已标失效的号排在同量级的窗口里——**绝不能再把 0 排除在调度外**：
                # 一旦误标，原实现永不再保活该号，cookie_status 只能靠重新扫码回 1（自锁）。
                dead = auth.cookie_status != 1
                state = AccountRefreshState(
                    account_id=account.id,
                    next_cookie_keepalive=now
                    + (random.uniform(20, 90) if dead else random.uniform(10, 60)),
                    next_mh5tk_refresh=now + random.uniform(MH5TK_REFRESH_MIN, MH5TK_REFRESH_MAX),
                )
                self._states[account.id] = state

            lock = self._locks.setdefault(account.id, asyncio.Lock())
            if lock.locked():
                continue
            try:
                if now >= state.next_cookie_keepalive:
                    await self._do_keepalive(account, auth, state)
                    await asyncio.sleep(random.uniform(*ACCOUNT_JITTER))
                elif now >= state.next_mh5tk_refresh:
                    await self._do_mh5tk_refresh(account, auth, state)
                    await asyncio.sleep(random.uniform(*ACCOUNT_JITTER))
            except Exception:
                logger.exception("账号保活异常 accountId=%d", account.id)

    # ---------- Cookie 保活 ----------

    async def _do_keepalive(self, account: Account, auth: AccountAuth, state: AccountRefreshState) -> None:
        dead = auth.cookie_status != 1
        state.next_cookie_keepalive = time.time() + (
            DEAD_PROBE_INTERVAL if dead else COOKIE_KEEPALIVE_INTERVAL
        )
        cookie_text = decrypt_cookie(auth.encrypted_cookie)

        if dead:
            # 已标失效的号不打 hasLogin（它就是误标来源），只复核一次能否自愈
            if await self.probe_session_alive(cookie_text, account.id) == ProbeResult.ALIVE:
                state.last_error = ""
                await self._revive(account.id, cookie_text)
            return

        result = await self.call_has_login(cookie_text, account.external_uid)

        if result["kind"] == KeepaliveResult.OK:
            if result.get("new_cookie_text"):
                await self._persist(account.id, result["new_cookie_text"])
            state.last_error = ""
            logger.debug("hasLogin 保活成功 accountId=%d", account.id)
        elif result["kind"] == KeepaliveResult.RISK:
            # hasLogin 缺 bx-ua 等反爬令牌易触发 Baxia：跳过本轮，不误标失效
            state.last_error = "risk_control"
            logger.warning("hasLogin 触发风控，跳过本轮 accountId=%d", account.id)
        elif result["kind"] == KeepaliveResult.EXPIRED:
            # hasLogin 的文案不是终态判据（真机 2026-09-20：同一份 Cookie 一分钟前刚发布成功）
            verdict = await self.probe_session_alive(cookie_text, account.id)
            if verdict == ProbeResult.ALIVE:
                state.last_error = "haslogin_false_expired"
                logger.warning(
                    "hasLogin 报登录态失效、只读 mtop 仍通过 ⇒ 判为误报，cookie_status 保持 1 accountId=%d",
                    account.id,
                )
            elif verdict == ProbeResult.EXPIRED:
                state.last_error = "session_expired"
                await self._mark_expired(account.id)
            else:
                state.last_error = "probe_undetermined"
                logger.warning(
                    "hasLogin 报失效但只读复核未判定 ⇒ 本轮不动 cookie_status accountId=%d",
                    account.id,
                )
        else:
            state.last_error = result.get("error", "unconfirmed")
            logger.warning("hasLogin 未确认 accountId=%d kind=%s", account.id, result["kind"])

    async def probe_session_alive(self, cookie_text: str, account_id: int) -> str:
        """一次只读 mtop（`loginuser.get`）复核登录态：alive / expired / undetermined。

        只有服务端明说 `FAIL_SYS_SESSION_EXPIRED` 才判 expired；网络抖动、风控、未识别错误
        一律 undetermined（宁可少标一次失效，也不要制造一个不可逆的 0）。
        """
        from app.api_client import ErrorKind, MtopClient, XianyuApiError

        client = MtopClient(cookie_provider=lambda: cookie_text)
        try:
            await client.call(PROBE_API, PROBE_VERSION, {})
            return ProbeResult.ALIVE
        except XianyuApiError as exc:
            if exc.kind == ErrorKind.SESSION_EXPIRED:
                return ProbeResult.EXPIRED
            logger.warning(
                "登录态只读复核未判定 accountId=%d kind=%s：%s", account_id, exc.kind.value, exc
            )
            return ProbeResult.UNDETERMINED
        except Exception:
            logger.exception("登录态只读复核异常 accountId=%d", account_id)
            return ProbeResult.UNDETERMINED
        finally:
            await client.aclose()

    async def call_has_login(self, cookie_text: str, unb: str) -> dict:
        """POST hasLogin.do 保活。返回 {kind, new_cookie_text?, error?}。"""
        cookie_dict = parse_cookie_text(cookie_text)
        form = build_has_login_form(cookie_dict, unb)
        headers = {**_H_COMMON, "Cookie": cookie_text}

        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                resp = await client.post(HAS_LOGIN_URL, data=form, headers=headers)
        except Exception as exc:
            return {"kind": KeepaliveResult.ERROR, "error": str(exc)}

        body = resp.text or ""
        if is_risk_response(body):
            return {"kind": KeepaliveResult.RISK}
        confirmed = resp.status_code == 200 and is_has_login_confirmed(body)
        if not confirmed and is_cookie_expired_response(body):
            return {"kind": KeepaliveResult.EXPIRED}

        # 合并响应 Set-Cookie（保活会滚动 _m_h5_tk 等）
        merged = dict(cookie_dict)
        merge_set_cookie_values(merged, resp.headers.get_list("set-cookie"))
        new_cookie_text = format_cookie_text(merged)

        if confirmed:
            return {"kind": KeepaliveResult.OK, "new_cookie_text": new_cookie_text}
        return {"kind": KeepaliveResult.UNCONFIRMED, "error": "hasLogin 未明确确认登录状态"}

    # ---------- _m_h5_tk 刷新 ----------

    async def _do_mh5tk_refresh(self, account: Account, auth: AccountAuth, state: AccountRefreshState) -> None:
        state.next_mh5tk_refresh = time.time() + random.uniform(MH5TK_REFRESH_MIN, MH5TK_REFRESH_MAX)
        cookie_text = decrypt_cookie(auth.encrypted_cookie)
        new_cookie, new_token = await self.refresh_m_h5_tk(cookie_text)

        if new_cookie and new_token:
            await self._persist(account.id, new_cookie, m_h5_tk=new_token)
            state.last_error = ""
            logger.info("_m_h5_tk 刷新成功 accountId=%d", account.id)
        else:
            state.last_error = "mh5tk_refresh_failed"
            logger.warning("_m_h5_tk 刷新失败 accountId=%d", account.id)

    async def refresh_m_h5_tk(self, cookie_text: str) -> tuple[str | None, str | None]:
        """三步刷新 _m_h5_tk（蓝本：ws_token.refresh_m_h5_tk）。

        1. GET 刷新端点拿初始 Cookie；2. 空 token POST 触发新 _m_h5_tk 下发；
        3. 真实 token POST 激活。失败返回 (None, None)。
        """
        data_str = json.dumps({"bizScene": "home"}, separators=(",", ":"))
        app_key = settings.mtop_app_key

        # 把原 Cookie 注入 jar（domain 放宽，模拟原实现的 CookieManager 还原）
        jar = httpx.Cookies()
        for k, v in parse_cookie_text(cookie_text).items():
            jar.set(k, v, domain=".goofish.com")

        form = {
            "jsv": "2.7.2",
            "appKey": app_key,
            "t": "",
            "sign": "",
            "v": "1.0",
            "type": "originaljson",
            "dataType": "json",
            "timeout": "20000",
            "api": _MH5TK_API,
            "data": data_str,
        }

        try:
            async with httpx.AsyncClient(timeout=15, cookies=jar, follow_redirects=False) as client:
                # Step 1: GET
                await client.get(_MH5TK_URL, headers=_H_COMMON)

                # Step 2: 空 token POST
                t1 = int(time.time() * 1000)
                form["t"] = str(t1)
                form["sign"] = hashlib.md5(f"&{t1}&{app_key}&{data_str}".encode()).hexdigest()
                await client.post(_MH5TK_URL, data=form, headers=_H_COMMON)

                new_m_h5_tk = client.cookies.get("_m_h5_tk")
                if not new_m_h5_tk:
                    return None, None

                # Step 3: 真实 token POST 激活
                token = new_m_h5_tk.split("_")[0]
                t2 = int(time.time() * 1000)
                form["t"] = str(t2)
                form["sign"] = hashlib.md5(f"{token}&{t2}&{app_key}&{data_str}".encode()).hexdigest()
                await client.post(_MH5TK_URL, data=form, headers=_H_COMMON)

                # 合并：原 Cookie 为底，jar 新值覆盖
                merged = parse_cookie_text(cookie_text)
                for k, v in client.cookies.items():
                    merged[k] = v
                return format_cookie_text(merged), new_m_h5_tk
        except Exception:
            logger.exception("refresh_m_h5_tk 异常")
            return None, None

    # ---------- 持久化与状态 ----------

    async def _persist(self, account_id: int, cookie_text: str, *,
                       m_h5_tk: str | None = None) -> None:
        """新 Cookie 回写 DB（重加密）+ 更新会话池内存凭据。"""
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(AccountAuth).where(AccountAuth.account_id == account_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.encrypted_cookie = encrypt_cookie(cookie_text)
            if m_h5_tk:
                row.encrypted_token = encrypt_cookie(m_h5_tk)
            await db.commit()

        # 内存会话池同步（避免 api_client 用旧 Cookie）
        from app.auth.session_pool import session_pool

        session = session_pool.get(account_id)
        if session:
            session.cookie = cookie_text

    async def _revive(self, account_id: int, cookie_text: str) -> None:
        """只读复核通过 ⇒ cookie_status 复位 1，并把会话重挂回内存会话池。

        没有这一步，误标一次就得靠人工重新扫码才能恢复（`auto_listing` 每轮都会因
        会话池里没有该账号而整域跳过发布）。
        """
        from app.auth.session_pool import session_pool

        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(AccountAuth).where(AccountAuth.account_id == account_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return
            row.cookie_status = 1
            account = await db.get(Account, account_id)
            await db.commit()

        session_pool.register(account_id, account.external_uid if account else "", cookie_text)
        self._states.pop(account_id, None)  # 新状态：按正常 30min 保活节奏重排
        logger.warning(
            "只读复核通过 ⇒ cookie_status 复位为 1 并重挂会话池 accountId=%d（此前是误标或已自行恢复）",
            account_id,
        )

    async def _mark_expired(self, account_id: int) -> None:
        """真失效（hasLogin 报文案 + 只读复核也判失效）：cookie_status=0 + 事件。"""
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(AccountAuth).where(AccountAuth.account_id == account_id)
                )
            ).scalar_one_or_none()
            if row:
                row.cookie_status = 0
                await db.commit()

        self._states.pop(account_id, None)
        await event_bus.publish(
            Event(
                type=EventType.COOKIE_EXPIRED,
                account_id=account_id,
                payload={"reason": "hasLogin 报失效且只读 mtop 复核同判，需重新扫码"},
            )
        )
        logger.warning("账号登录态失效 accountId=%d，已置 cookie_status=0 并发事件", account_id)
