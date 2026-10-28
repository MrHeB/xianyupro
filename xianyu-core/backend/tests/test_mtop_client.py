"""MtopClient 离线单测：token 过期重试 + 风控熔断 + 传输层折算（protocol.md §1.3）。"""

import httpx
import pytest

from app.api_client import ErrorKind, MtopClient, XianyuApiError


class FakeResponse:
    def __init__(self, payload: dict, set_cookies: list[str] | None = None):
        self._payload = payload
        self._set_cookies = set_cookies or []

    def json(self) -> dict:
        return self._payload

    class _Headers:
        def __init__(self, set_cookies):
            self._set_cookies = set_cookies

        def get_list(self, name):
            return self._set_cookies if name == "set-cookie" else []

    headers = property(lambda self: FakeResponse._Headers(self._set_cookies))


class FakeHttp:
    """按脚本回放的 httpx.AsyncClient 替身。"""

    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self.responses.pop(0)

    async def aclose(self):
        pass


def make_client(responses: list[FakeResponse], cookie: str = "unb=1; _m_h5_tk=old_1"):
    updates: list[str] = []
    client = MtopClient(
        cookie_provider=lambda: cookie,
        on_cookie_update=updates.append,
    )
    client._http = FakeHttp(responses)
    return client, updates


@pytest.mark.asyncio
async def test_success_returns_data():
    client, updates = make_client(
        [FakeResponse({"ret": ["SUCCESS::调用成功"], "data": {"foo": 1}})]
    )
    assert await client.call("mtop.a.b", "1.0", {}) == {"foo": 1}
    assert updates == []


@pytest.mark.asyncio
async def test_token_expired_retries_once_with_new_token():
    """FAIL_SYS_TOKEN_EXOIRED → 用响应 Set-Cookie 的新 _m_h5_tk 重试 1 次成功。"""
    client, updates = make_client(
        [
            FakeResponse(
                {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]},
                set_cookies=["_m_h5_tk=newtok_99; Path=/; Domain=.goofish.com"],
            ),
            FakeResponse({"ret": ["SUCCESS::调用成功"], "data": {"ok": True}}),
        ]
    )
    result = await client.call("mtop.a.b", "1.0", {"x": 1})
    assert result == {"ok": True}
    # 新 Cookie 已通过回调同步（含新 token）
    assert updates and "_m_h5_tk=newtok_99" in updates[0]
    # 两次请求的签名 token 不同（第二次用新 token）
    reqs = client._http.requests
    assert reqs[0]["params"]["sign"] != reqs[1]["params"]["sign"]


@pytest.mark.asyncio
async def test_token_expired_twice_raises():
    """重试仍 TOKEN 过期 → 抛 XianyuApiError(kind=token_expired)。"""
    client, _ = make_client(
        [
            FakeResponse({"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]}),
            FakeResponse({"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]}),
        ]
    )
    with pytest.raises(XianyuApiError) as exc_info:
        await client.call("mtop.a.b", "1.0", {})
    assert exc_info.value.kind == ErrorKind.TOKEN_EXPIRED


@pytest.mark.asyncio
async def test_risk_control_trips_breaker():
    client, _ = make_client(
        [FakeResponse({"ret": ["RGV587_ERROR::哎哟喂"]})]
    )
    with pytest.raises(XianyuApiError) as exc_info:
        await client.call("mtop.a.b", "1.0", {})
    assert exc_info.value.kind == ErrorKind.RISK_CONTROL
    # 熔断打开：后续请求直接被拒
    with pytest.raises(XianyuApiError) as exc_info2:
        await client.call("mtop.a.b", "1.0", {})
    assert exc_info2.value.kind == ErrorKind.RISK_CONTROL


# ---------- 传输层异常折算（2026-09-20 真机：ConnectError 冒成裸 500） ----------


class FlakyHttp:
    """前 `failures` 次抛传输异常，之后按 `responses` 回放。"""

    def __init__(self, failures: int = 1, responses: list | None = None, exc: Exception | None = None):
        self.failures = failures
        self.responses = list(responses or [])
        self.exc = exc or httpx.ConnectError("")
        self.posts = 0

    async def post(self, url, **kwargs):
        self.posts += 1
        if self.failures:
            self.failures -= 1
            raise self.exc
        return self.responses.pop(0)

    async def aclose(self):
        pass


class NonJsonResponse:
    """网关/反代回了 HTML 错误页：`resp.json()` 抛 ValueError。"""

    status_code = 502

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


@pytest.fixture
def no_sleep(monkeypatch):
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", _sleep)


@pytest.mark.asyncio
async def test_network_blip_on_read_call_is_wrapped_and_retried(no_sleep):
    """只读调用遇网络抖动：折算成 kind=NETWORK 并补发一次，别让整条发布链白跑一轮。"""
    client = MtopClient(cookie_provider=lambda: "unb=1; _m_h5_tk=old_1")
    client._http = FlakyHttp(
        responses=[FakeResponse({"ret": ["SUCCESS::调用成功"], "data": {"ok": 1}})]
    )
    assert await client.call("mtop.a.b", "1.0", {}) == {"ok": 1}
    assert client._http.posts == 2


@pytest.mark.asyncio
async def test_write_call_is_never_automatically_resent(no_sleep):
    """断连 ≠ 闲鱼没收到：写调用自动重发就是重复发布，必须原样抛出去交人工核对。"""
    client = MtopClient(cookie_provider=lambda: "unb=1; _m_h5_tk=old_1")
    client._http = FlakyHttp()
    with pytest.raises(XianyuApiError) as exc_info:
        await client.call("mtop.a.b", "1.0", {}, is_write=True)
    assert exc_info.value.kind == ErrorKind.NETWORK
    assert "ConnectError" in str(exc_info.value)
    assert client._http.posts == 1


@pytest.mark.asyncio
async def test_non_json_body_maps_to_network(no_sleep):
    """网关的 HTML 页不再冒成裸 500，也不把整页拼进报错——只报状态码。"""
    client = MtopClient(cookie_provider=lambda: "unb=1; _m_h5_tk=old_1")
    client._http = FlakyHttp(
        failures=0, responses=[NonJsonResponse(), NonJsonResponse()]
    )
    with pytest.raises(XianyuApiError) as exc_info:
        await client.call("mtop.a.b", "1.0", {}, is_write=True)
    assert exc_info.value.kind == ErrorKind.NETWORK
    assert "响应非 JSON（HTTP 502）" in str(exc_info.value)


@pytest.mark.asyncio
async def test_local_write_throttle_never_leaves_the_process(no_sleep):
    """自己令牌桶没排到号 → kind=LOCAL_THROTTLE，请求一次都没出门。

    折算成 UNKNOWN 会被发布链路当成一次写失败记进止损额度：三次本地排队换来 24h 熔断。
    """
    client = MtopClient(cookie_provider=lambda: "unb=1; _m_h5_tk=old_1")
    client._http = FlakyHttp(
        failures=0, responses=[FakeResponse({"ret": ["SUCCESS::调用成功"], "data": {"ok": 1}})]
    )
    assert client.write_limiter.acquire()  # 吃掉唯一的令牌
    with pytest.raises(XianyuApiError) as exc_info:
        await client.call("mtop.a.b", "1.0", {}, is_write=True)
    assert exc_info.value.kind == ErrorKind.LOCAL_THROTTLE
    assert exc_info.value.retryable is True
    assert client._http.posts == 0
    # 只读调用不受写令牌桶约束
    assert await client.call("mtop.a.b", "1.0", {}) == {"ok": 1}
