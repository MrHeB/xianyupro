"""mtop 请求客户端（protocol.md §1）。

统一封装：公共参数 + 签名 + 错误分类 + 限流/熔断。
阶段 4-2 验收：用只读接口（在售列表/订单查询）验证签名正确。
"""

import asyncio
import logging
from typing import Any, Callable

import httpx

from app.api_client.errors import ErrorKind, XianyuApiError, classify_error
from app.api_client.rate_limiter import CircuitBreaker, TokenBucket
from app.api_client.sign import compact_json, extract_token, signed_params
from app.core.cookies import format_cookie_text, merge_set_cookie_values, parse_cookie_text
from app.core.config import settings

logger = logging.getLogger(__name__)

# Chrome UA + Referer 全套（protocol.md §1.1）
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
}


class MtopClient:
    """单账号 mtop 客户端（每账号一个实例，凭据由 auth 会话池注入）。"""

    def __init__(
        self,
        cookie_provider: Callable[[], str],  # 实时取最新 Cookie（含续期后的）
        *,
        app_key: str | None = None,
        base_url: str | None = None,
        on_cookie_update: Callable[[str], None] | None = None,
    ):
        self._cookie_provider = cookie_provider
        self._on_cookie_update = on_cookie_update
        self.app_key = app_key or settings.mtop_app_key
        self.base_url = base_url or settings.h5_api_base
        # 写操作限流 + 风控熔断（跨进程部署时迁 Redis）
        self.write_limiter = TokenBucket(rate_per_minute=1.0)
        self.risk_breaker = CircuitBreaker(cooldown_seconds=600.0)
        self._http = httpx.AsyncClient(timeout=20.0, headers=DEFAULT_HEADERS)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(
        self,
        api: str,
        version: str,
        data: dict[str, Any],
        *,
        is_write: bool = False,
    ) -> dict:
        """发起一次 mtop 调用。

        - is_write：写操作（发布/发货/回复），先过令牌桶；
        - TOKEN 过期自动用响应 Set-Cookie 的新 _m_h5_tk 重试 1 次（protocol.md §1.3）；
        - 网络/传输异常折算成 `kind=NETWORK`（只读调用自动补发 1 次，写调用不补发）；
        - 令牌桶排队失败折算成 `kind=LOCAL_THROTTLE`（请求没出门，不该被当成平台拒单）；
        - 返回 data dict；失败抛 XianyuApiError（kind 决定上层策略）。
        """
        if self.risk_breaker.is_open:
            raise XianyuApiError(ErrorKind.RISK_CONTROL, "熔断中，拒绝请求（RGV587 冷却 10min）")

        if is_write and not self.write_limiter.acquire():
            wait = self.write_limiter.wait_seconds
            raise XianyuApiError(
                ErrorKind.LOCAL_THROTTLE, f"写操作限流：{wait:.0f}s 后重试"
            )

        cookie = self._cookie_provider()
        try:
            resp, payload, ret = await self._post(api, version, data, cookie)
        except XianyuApiError as exc:
            # 只读调用遇网络抖动补发一次（否则整条发布链白跑一轮，还被记一次写失败）；
            # 写调用**不自动重发**——连接断了不等于闲鱼没收到，重发就等于重复发布。
            if exc.kind != ErrorKind.NETWORK or is_write:
                raise
            logger.info("mtop 网络抖动，1s 后重发一次 api=%s：%s", api, exc)
            await asyncio.sleep(1.0)
            resp, payload, ret = await self._post(api, version, data, cookie)

        if ret and str(ret[0]).startswith("SUCCESS"):
            return payload.get("data", {})

        kind = classify_error(ret, str(payload.get("error", "")))
        if kind == ErrorKind.TOKEN_EXPIRED:
            # token 过期/为空：响应 Set-Cookie 携带新 _m_h5_tk，重试 1 次
            new_cookie = self._merged_cookie(cookie, resp)
            if new_cookie:
                logger.info("mtop token 过期，Set-Cookie 换新 token 重试 api=%s", api)
                resp, payload, ret = await self._post(api, version, data, new_cookie)
                if ret and str(ret[0]).startswith("SUCCESS"):
                    return payload.get("data", {})
                kind = classify_error(ret, str(payload.get("error", "")))

        if kind == ErrorKind.RISK_CONTROL:
            self.risk_breaker.trip()  # 命中风控 → 熔断 10 分钟
        raise XianyuApiError(kind, f"mtop {api} 失败: {ret}", ret=ret)

    async def _post(
        self, api: str, version: str, data: dict[str, Any], cookie: str
    ) -> tuple[httpx.Response, dict, list]:
        """一次实际 POST。返回 (resp, payload, ret)——调用方判 ret/取 Set-Cookie。

        传输层异常一律折算成 `ErrorKind.NETWORK`：不折算的话 `httpx.ConnectError`
        会一路冒到路由外，前端只看到一句「500 Internal Server Error」＋日志里 60 行栈，
        而它真实含义是"请求没发出去/连接被断"，跟闲鱼接口无关（2026-09-20 踩过）。
        """
        token = extract_token(cookie)
        params = signed_params(api, version, data, token, self.app_key)
        url = f"{self.base_url}/{api}/{version}/"

        try:
            resp = await self._http.post(
                url, params=params, data={"data": compact_json(data)}, headers={"Cookie": cookie}
            )
        except httpx.TimeoutException as exc:
            raise XianyuApiError(
                ErrorKind.NETWORK, f"mtop {api} 网络超时（{type(exc).__name__}）"
            ) from exc
        except httpx.TransportError as exc:
            raise XianyuApiError(
                ErrorKind.NETWORK, f"mtop {api} 网络异常（{type(exc).__name__}，请求未送达闲鱼）"
            ) from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            # 拿到的不是 JSON（多为反代/网关的 HTML 错误页）——只报状态码，不把整页铺进报错
            raise XianyuApiError(
                ErrorKind.NETWORK, f"mtop {api} 响应非 JSON（HTTP {resp.status_code}）"
            ) from exc
        return resp, payload, payload.get("ret") or []

    def _merged_cookie(self, cookie: str, resp: httpx.Response) -> str | None:
        """把响应 Set-Cookie 合并进当前 Cookie（新 _m_h5_tk 等）。

        合并结果回传 on_cookie_update，让会话池内存凭据同步。
        """
        set_cookies = resp.headers.get_list("set-cookie")
        if not set_cookies:
            return None
        merged = parse_cookie_text(cookie)
        changed = merge_set_cookie_values(merged, set_cookies)
        if not changed:
            return None
        new_cookie = format_cookie_text(merged)
        if self._on_cookie_update:
            try:
                self._on_cookie_update(new_cookie)
            except Exception:
                logger.exception("on_cookie_update 回调失败")
        return new_cookie
