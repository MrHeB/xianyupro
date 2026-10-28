"""auth 阶段 4-1 离线单测：Cookie 工具 + hasLogin 响应分类 + 保活表单 + 失效判定。"""

import json
import time
from types import SimpleNamespace

import pytest
from app.api_client import ErrorKind, XianyuApiError
from app.auth import refresher as rf
from app.auth.cookies import (
    format_cookie_text,
    merge_set_cookie_values,
    missing_critical_keys,
    parse_cookie_text,
)
from app.auth.refresher import (
    ProbeResult,
    build_has_login_form,
    is_cookie_expired_response,
    is_has_login_confirmed,
    is_risk_response,
)


# ---- cookies.py ----


def test_parse_and_format_roundtrip():
    text = "unb=123; _m_h5_tk=abc_987; cookie2=xyz"
    assert parse_cookie_text(text) == {"unb": "123", "_m_h5_tk": "abc_987", "cookie2": "xyz"}
    assert format_cookie_text(parse_cookie_text(text)) == text


def test_parse_empty_and_malformed():
    assert parse_cookie_text("") == {}
    assert parse_cookie_text("novalue; a=1") == {"a": "1"}


def test_merge_set_cookie_values_overrides():
    base = {"unb": "123", "_m_h5_tk": "old"}
    changed = merge_set_cookie_values(
        base,
        ["_m_h5_tk=new; Path=/; Domain=.taobao.com", "havana_lgc2_77=x; Domain=.alibaba.com"],
    )
    assert base["_m_h5_tk"] == "new"
    assert base["havana_lgc2_77"] == "x"
    assert set(changed) == {"_m_h5_tk", "havana_lgc2_77"}


def test_merge_set_cookie_values_no_change_when_same():
    base = {"unb": "123"}
    assert merge_set_cookie_values(base, ["unb=123; Path=/"]) == []


def test_missing_critical_keys():
    assert missing_critical_keys({"unb": "1"}) != []
    full = {k: "v" for k in ("havana_lgc2_77", "_hvn_lgc_", "havana_lgc_exp", "unb", "_m_h5_tk")}
    assert missing_critical_keys(full) == []


# ---- refresher 响应分类 ----


def test_has_login_confirmed_old_format():
    assert is_has_login_confirmed(json.dumps({"hasLogin": True}))


def test_has_login_confirmed_new_format():
    assert is_has_login_confirmed(json.dumps({"content": {"data": {"hasLogin": True}}}))


def test_has_login_confirmed_rejects_false_and_garbage():
    assert not is_has_login_confirmed(json.dumps({"hasLogin": False}))
    assert not is_has_login_confirmed("<html>blocked</html>")
    assert not is_has_login_confirmed("")


def test_cookie_expired_response():
    assert is_cookie_expired_response('{"titleMsg":"您的登录态已失效，请重新登录"}')
    assert not is_cookie_expired_response('{"hasLogin":true}')


def test_risk_response():
    assert is_risk_response("RGV587_ERROR::哎呀出错了")
    assert is_risk_response("FAIL_SYS_USER_VALIDATE::x")
    assert not is_risk_response('{"hasLogin":true}')


def test_build_has_login_form_fields():
    form = build_has_login_form({"XSRF-TOKEN": "t1", "cookie2": "c2", "cna": "cn"}, "unb1")
    assert form["hid"] == "unb1"
    assert form["_csrf_token"] == "t1"
    assert form["hsiz"] == "c2"
    assert form["deviceId"] == "cn"
    assert form["appName"] == "xianyu"
    assert form["defaultView"] == "hasLogin"


# ---- 失效判定：hasLogin 文案不是终态，必须只读 mtop 复核（2026-09-20 真机误标） ----


def _acct(cookie_status: int = 1):
    return (
        SimpleNamespace(id=1, external_uid="u1"),
        SimpleNamespace(cookie_status=cookie_status, encrypted_cookie="enc:v1:x"),
    )


def _make_refresher(monkeypatch, *, has_login=None, probe=ProbeResult.ALIVE):
    """不碰 DB、不碰网络的调度器实例——只验判定分派。"""
    r = rf.CookieTokenRefresher(session_factory=None)
    calls = {"expired": [], "revived": [], "has_login": []}
    monkeypatch.setattr(rf, "decrypt_cookie", lambda _c: "unb=1; _m_h5_tk=t_1; cookie2=c2")

    async def _has_login(_cookie, _unb):
        calls["has_login"].append(1)
        return dict(has_login or {"kind": rf.KeepaliveResult.OK})

    async def _probe(_cookie, _account_id):
        return probe

    async def _mark_expired(account_id):
        calls["expired"].append(account_id)

    async def _revive(account_id, _cookie):
        calls["revived"].append(account_id)

    monkeypatch.setattr(r, "call_has_login", _has_login)
    monkeypatch.setattr(r, "probe_session_alive", _probe)
    monkeypatch.setattr(r, "_mark_expired", _mark_expired)
    monkeypatch.setattr(r, "_revive", _revive)
    return r, calls


@pytest.mark.asyncio
async def test_has_login_expired_is_vetoed_by_readonly_probe(monkeypatch):
    """hasLogin 说失效、只读 mtop 仍通 ⇒ 判为误报，不置 cookie_status=0。"""
    r, calls = _make_refresher(
        monkeypatch,
        has_login={"kind": rf.KeepaliveResult.EXPIRED},
        probe=ProbeResult.ALIVE,
    )
    account, auth = _acct()
    state = rf.AccountRefreshState(account_id=1)
    await r._do_keepalive(account, auth, state)
    assert calls["expired"] == []
    assert state.last_error == "haslogin_false_expired"


@pytest.mark.asyncio
async def test_mark_expired_only_when_probe_agrees(monkeypatch):
    """对照组：两条判据同说失效，才真的置 0 + 发事件。"""
    r, calls = _make_refresher(
        monkeypatch,
        has_login={"kind": rf.KeepaliveResult.EXPIRED},
        probe=ProbeResult.EXPIRED,
    )
    account, auth = _acct()
    await r._do_keepalive(account, auth, rf.AccountRefreshState(account_id=1))
    assert calls["expired"] == [1]


@pytest.mark.asyncio
async def test_undetermined_probe_leaves_cookie_status_alone(monkeypatch):
    """复核判不动（网络抖动/风控）⇒ 宁可少标一次，也不制造不可逆的 0。"""
    r, calls = _make_refresher(
        monkeypatch,
        has_login={"kind": rf.KeepaliveResult.EXPIRED},
        probe=ProbeResult.UNDETERMINED,
    )
    account, auth = _acct()
    await r._do_keepalive(account, auth, rf.AccountRefreshState(account_id=1))
    assert calls["expired"] == []


@pytest.mark.asyncio
async def test_dead_account_gets_readonly_probe_instead_of_has_login(monkeypatch):
    """cookie_status=0 的号也要留在调度里（旧实现只选 1 ⇒ 误标后永不自愈）。"""
    r, calls = _make_refresher(monkeypatch, probe=ProbeResult.ALIVE)
    account, auth = _acct(cookie_status=0)
    state = rf.AccountRefreshState(account_id=1)
    await r._do_keepalive(account, auth, state)
    assert calls["has_login"] == [], "已标失效的号不该再打 hasLogin"
    assert calls["revived"] == [1]
    # 复核节奏走 DEAD_PROBE_INTERVAL，不是正常的 30min
    assert state.next_cookie_keepalive - time.time() <= rf.DEAD_PROBE_INTERVAL + 1


def _patch_probe_client(monkeypatch, exc: Exception | None = None):
    """换掉 probe 里现构的 MtopClient（函数内 import，故打 `app.api_client` 上的名字）。"""
    seen = {"apis": [], "closed": 0}

    class Fake:
        def __init__(self, *, cookie_provider):
            self.cookie_provider = cookie_provider

        async def call(self, api, version, data, *, is_write=False):
            seen["apis"].append((api, is_write))
            if exc is not None:
                raise exc
            return {"userNumId": "123"}

        async def aclose(self):
            seen["closed"] += 1

    monkeypatch.setattr("app.api_client.MtopClient", Fake)
    return seen


@pytest.mark.asyncio
async def test_probe_uses_one_readonly_call_and_closes_client(monkeypatch):
    seen = _patch_probe_client(monkeypatch)
    r = rf.CookieTokenRefresher(session_factory=None)
    assert await r.probe_session_alive("unb=1", 1) == ProbeResult.ALIVE
    assert seen["apis"] == [(rf.PROBE_API, False)], "探针必须只读（占写令牌桶就白排一次队）"
    assert seen["closed"] == 1


@pytest.mark.asyncio
async def test_probe_expired_only_on_session_expired(monkeypatch):
    """服务端明说 SESSION_EXPIRED 才判失效；网络抖动一类一律未判定。"""
    r = rf.CookieTokenRefresher(session_factory=None)
    _patch_probe_client(monkeypatch, XianyuApiError(ErrorKind.SESSION_EXPIRED, "会话过期"))
    assert await r.probe_session_alive("unb=1", 1) == ProbeResult.EXPIRED

    _patch_probe_client(monkeypatch, XianyuApiError(ErrorKind.NETWORK, "ConnectError"))
    assert await r.probe_session_alive("unb=1", 1) == ProbeResult.UNDETERMINED

    _patch_probe_client(monkeypatch, RuntimeError("意外炸了"))
    assert await r.probe_session_alive("unb=1", 1) == ProbeResult.UNDETERMINED
