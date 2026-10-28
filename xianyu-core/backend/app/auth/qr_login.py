"""闲鱼网页版扫码登录（异步 httpx 实现，蓝本：xianyu-pilot-pro xianyu_qr_login.py）。

三阶段采集 Cookie，最终合并：
- 阶段1 _get_m_h5_tk：请求 h5api，触发服务端 Set-Cookie 下发 _m_h5_tk；
- 阶段2 _get_login_params：访问 mini_login.htm 提取 viewData.loginFormData；
- 阶段3 _poll_once：轮询 query.do，CONFIRMED 后收集全部 Cookie。

会话存进程内存（单副本部署假设，流程方案已定调；多副本时迁 Redis）。
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import random
import re
import secrets
import time

import httpx
import qrcode

from app.auth.cookies import merge_set_cookie_values, missing_critical_keys
from app.core.config import settings

logger = logging.getLogger(__name__)

APP_KEY = settings.mtop_app_key
H5_API = f"{settings.h5_api_base}/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/"
LOGIN_PAGE = "https://passport.goofish.com/mini_login.htm"
QR_GENERATE = "https://passport.goofish.com/newlogin/qrcode/generate.do"
QR_QUERY = "https://passport.goofish.com/newlogin/qrcode/query.do"

H_COMMON = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://passport.goofish.com/",
    "Origin": "https://passport.goofish.com",
}
H_PAGE = {**H_COMMON, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
H_API = {**H_COMMON, "Accept": "application/json, text/plain, */*"}

SESSION_TIMEOUT = 300  # 会话 5 分钟过期
_MAX_SESSIONS = 100


class QrLoginError(RuntimeError):
    """扫码登录流程错误。"""


def _json_or_raise(resp: httpx.Response, stage: str) -> dict:
    """解析 JSON；返回 HTML/风控页时抛明确错误。"""
    if resp.status_code >= 400:
        raise QrLoginError(f"{stage} HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        text = (resp.text or "").strip()
        if text.startswith("<"):
            raise QrLoginError(f"{stage} 返回了无法识别的响应") from exc
        raise QrLoginError(f"{stage} 响应解析失败") from exc


class QrLoginService:
    """扫码登录会话管理（每进程一个实例）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    # ---------- 内部三阶段 ----------

    async def _get_m_h5_tk(self, client: httpx.AsyncClient) -> str:
        """阶段1：空 token POST 触发 _m_h5_tk 下发，再用真实 token 激活。"""
        data_str = '{"bizScene":"home"}'
        form = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": "",
            "sign": "",
            "v": "1.0",
            "type": "originaljson",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get",
            "data": data_str,
        }

        # 第一次 GET — 拿初始 Cookie（cookie2）
        await client.get(H5_API, headers=H_API)

        # 空 token POST — 服务端在 Set-Cookie 里下发 _m_h5_tk
        t_ms = int(time.time() * 1000)
        form["t"] = str(t_ms)
        form["sign"] = hashlib.md5(f"&{t_ms}&{APP_KEY}&{data_str}".encode()).hexdigest()
        await client.post(H5_API, headers=H_API, data=form)

        m_h5_tk = client.cookies.get("_m_h5_tk")
        if not m_h5_tk:
            raise QrLoginError("无法获取 _m_h5_tk Cookie")

        # 真实 token POST — 激活（拿完整业务 Cookie）
        token = m_h5_tk.split("_")[0]
        t_ms2 = int(time.time() * 1000)
        form["t"] = str(t_ms2)
        form["sign"] = hashlib.md5(f"{token}&{t_ms2}&{APP_KEY}&{data_str}".encode()).hexdigest()
        await client.post(H5_API, headers=H_API, data=form)
        return client.cookies.get("_m_h5_tk", "")

    async def _get_login_params(self, client: httpx.AsyncClient) -> dict:
        """阶段2：从登录页提取 loginFormData（含 umidTag）。"""
        params = {
            "lang": "zh_cn",
            "appName": "xianyu",
            "appEntrance": "web",
            "styleType": "vertical",
            "bizParams": "",
            "notLoadSsoView": "false",
            "notKeepLogin": "false",
            "isMobile": "false",
            "qrCodeFirst": "false",
            "stie": "77",
            "rnd": str(random.random()),
        }
        resp = await client.get(LOGIN_PAGE, headers=H_PAGE, params=params)

        match = re.search(r"window\.viewData\s*=\s*(\{.*?\});", resp.text, re.DOTALL)
        if not match:
            match = re.search(r"var\s+viewData\s*=\s*(\{.*?\});", resp.text, re.DOTALL)
        if not match:
            raise QrLoginError("无法从登录页面提取 viewData（页面改版？）")

        login_form = json.loads(match.group(1))["loginFormData"]
        login_form["umidTag"] = "SERVER"
        return login_form

    async def _generate_qrcode(self, client: httpx.AsyncClient, login_form: dict) -> str:
        """阶段3a：生成二维码，返回 base64 data URI，并回填 t/ck 供轮询。"""
        resp = await client.get(QR_GENERATE, headers=H_API, params=login_form)
        result = _json_or_raise(resp, "生成二维码")
        qr_data = result.get("content", {}).get("data") or {}

        code_content = qr_data.get("codeContent")
        if not code_content:
            raise QrLoginError(f"二维码内容为空: {json.dumps(result, ensure_ascii=False)[:300]}")

        login_form["t"] = qr_data.get("t", "")
        login_form["ck"] = qr_data.get("ck", "")

        img = qrcode.make(code_content)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    async def _poll_once(self, client: httpx.AsyncClient, login_form: dict) -> dict:
        """阶段3b：单次轮询扫码状态（非阻塞）。"""
        try:
            resp = await client.post(QR_QUERY, headers=H_API, data=login_form)
            data = _json_or_raise(resp, "轮询二维码状态").get("content", {}).get("data") or {}
            status = data.get("qrCodeStatus", "")
        except Exception as exc:
            logger.warning("轮询异常 errorType=%s", type(exc).__name__)
            return {"status": "error", "message": "登录状态查询失败，请稍后重试"}

        if status == "CONFIRMED":
            if data.get("iframeRedirect"):
                return {
                    "status": "verification_required",
                    "iframe_redirect_url": data.get("iframeRedirectUrl"),
                }
            # 收集 Cookie：jar + 响应头 Set-Cookie 合并（防丢登录态字段，见 cookies.py 注释）
            cookies = dict(client.cookies)
            merge_set_cookie_values(cookies, resp.headers.get_list("set-cookie"))
            missing = missing_critical_keys(cookies)
            if missing:
                logger.warning("CONFIRMED 但缺少关键登录态字段 missing=%s", missing)
            else:
                logger.info("CONFIRMED: 关键登录态字段全部就绪 (%d 个)", len(cookies))
            return {"status": "confirmed", "cookies": cookies}
        if status == "EXPIRED":
            return {"status": "expired"}
        if status == "SCANED":
            return {"status": "scaned"}
        if status == "NEW":
            return {"status": "new"}
        return {"status": "cancelled"}

    # ---------- 公开 API ----------

    async def generate(self) -> dict:
        """创建扫码会话，返回 {session_id, qr_image}。"""
        await self._cleanup_expired()

        async with self._lock:
            if len(self._sessions) >= _MAX_SESSIONS:
                raise QrLoginError("扫码会话容量已满，请稍后重试")
            session_id = secrets.token_urlsafe(32)
            self._sessions[session_id] = {"created_at": time.time(), "status": "initializing"}

        client = httpx.AsyncClient(timeout=20, follow_redirects=False)
        try:
            await self._get_m_h5_tk(client)
            login_form = await self._get_login_params(client)
            qr_image = await self._generate_qrcode(client, login_form)
        except Exception as exc:
            await client.aclose()
            async with self._lock:
                self._sessions.pop(session_id, None)
            # 带上异常类型：httpx 的 ReadTimeout/ConnectError 的 str() 是空串，
            # 只打 {exc} 前端会显示「生成登录二维码失败: 」，等于没报错（口径同 alibaba_login）
            raise QrLoginError(f"生成登录二维码失败: {type(exc).__name__}: {exc}") from exc

        async with self._lock:
            self._sessions[session_id].update(
                {
                    "client": client,
                    "login_form": login_form,
                    "qr_image": qr_image,
                    "status": "new",
                }
            )
        logger.info("扫码登录会话已创建 sessionId=%s...", session_id[:8])
        return {"session_id": session_id, "qr_image": qr_image}

    async def poll(self, session_id: str) -> dict:
        """轮询一次状态；confirmed 时 Cookie 存入会话（只返回脱敏摘要）。"""
        sdata = await self._get_session(session_id)
        if sdata is None:
            return {"status": "expired", "message": "会话不存在或已过期"}
        if time.time() - sdata["created_at"] > SESSION_TIMEOUT:
            await self.cleanup(session_id)
            return {"status": "expired", "message": "登录超时"}

        result = await self._poll_once(sdata["client"], sdata["login_form"])
        sdata["status"] = result["status"]

        if result["status"] == "confirmed":
            cookies = result.pop("cookies")
            sdata["saved_cookies"] = cookies
            result["unb"] = cookies.get("unb", "")
        return result

    async def take_cookies(self, session_id: str) -> dict | None:
        """取走 confirmed 会话的完整 Cookie（供入库），并清理会话。QR 只能确认一次。"""
        sdata = await self._get_session(session_id)
        if sdata is None:
            return None
        cookies = sdata.get("saved_cookies")
        await self.cleanup(session_id)
        return cookies

    async def cleanup(self, session_id: str) -> None:
        async with self._lock:
            sdata = self._sessions.pop(session_id, None)
        if sdata and "client" in sdata:
            await sdata["client"].aclose()

    async def _get_session(self, session_id: str) -> dict | None:
        if not session_id or len(session_id) < 32:
            return None
        async with self._lock:
            return self._sessions.get(session_id)

    async def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items() if now - s["created_at"] > SESSION_TIMEOUT
        ]
        for sid in expired:
            await self.cleanup(sid)

    async def aclose(self) -> None:
        for sid in list(self._sessions):
            await self.cleanup(sid)


# 全局单例（FastAPI lifespan 管理生命周期）
qr_login_service = QrLoginService()
