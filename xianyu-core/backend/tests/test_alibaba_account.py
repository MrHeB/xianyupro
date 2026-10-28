"""1688 货源账号：扫码登录（Playwright）+ 入库 + REST（离线，不起浏览器、不触网）。

重点覆盖只有 1688 才有的两处逻辑：跨域同名 Cookie 的取值优先级，以及登录成功后必须
显式走一遍 marketSigninJump 把 .1688.com 会话种上——漏了这一步，拿到的是 .taobao.com
域 Cookie，详情页照样被 x5sec punish。
"""

import time

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.api.routes import api_router
from app.auth.alibaba_account_service import (
    AlibabaAccountError,
    upsert_alibaba_account_from_cookies,
)
from app.auth.alibaba_login import (
    AlibabaQrLoginService,
    cookies_from_context,
    missing_alibaba_keys,
)
from app.models import AlibabaAccount, Base, User
from app.users.deps import get_current_user
from app.users.security import hash_password


# ---------------- 跨域 Cookie 取值 ----------------


def _rec(name, value, domain=".taobao.com"):
    return {"name": name, "value": value, "domain": domain, "path": "/"}


def test_cookies_from_context_prefers_1688_domain():
    """同名 cookie2 有两份时留 .1688.com 那份：这串 Cookie 是拿去打 detail.1688.com 的。"""
    cookies = cookies_from_context(
        [
            _rec("cookie2", "TAOBAO", ".taobao.com"),
            _rec("unb", "123", ".taobao.com"),
            _rec("cookie2", "ALI", ".1688.com"),
        ]
    )
    assert cookies == {"cookie2": "ALI", "unb": "123"}


def test_cookies_from_context_skips_empty_values():
    cookies = cookies_from_context([_rec("unb", ""), _rec("cookie2", "C2")])
    assert cookies == {"cookie2": "C2"}
    assert missing_alibaba_keys(cookies) == ["unb"]


def test_cookie2_alone_is_not_login():
    """cookie2 匿名阶段就会下发，判据必须看 unb，否则会把匿名会话当已登录入库。"""
    anonymous = cookies_from_context([_rec("cookie2", "C2"), _rec("_tb_token_", "T")])
    assert missing_alibaba_keys(anonymous) == ["unb"]


# ---------------- poll：浏览器会话状态 ----------------


class _FakeContext:
    """按脚本依次回吐 Cookie，模拟"种跳前 / 种跳后"两个快照。"""

    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self.closed = False

    async def cookies(self):
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]

    async def close(self):
        self.closed = True


class _FakePage:
    def __init__(self):
        self.seed_calls = 0

    async def goto(self, url, **kw):
        self.seed_calls += 1

    async def wait_for_timeout(self, ms):
        return None

    async def query_selector(self, sel):
        return None

    async def inner_text(self, sel):
        return ""


def _service_with(context, page, created_at=None):
    svc = AlibabaQrLoginService()
    sid = "s" * 40
    svc._sessions[sid] = {
        "created_at": time.time() if created_at is None else created_at,
        "status": "new",
        "context": context,
        "page": page,
        "seeded": False,
    }
    return svc, sid


async def test_poll_not_confirmed_until_unb_arrives():
    svc, sid = _service_with(_FakeContext([[_rec("cookie2", "C2")]]), _FakePage())
    assert (await svc.poll(sid))["status"] in ("new", "scaned")
    assert await svc.take_cookies(sid) is None


async def test_poll_confirmed_seeds_1688_domain_once_and_stores_cookies():
    tao = [_rec("cookie2", "TAOBAO"), _rec("unb", "9"), _rec("tracknick", "nick%40x")]
    after = tao + [_rec("cookie2", "ALI", ".1688.com")]
    page = _FakePage()
    svc, sid = _service_with(_FakeContext([tao, after, after]), page)

    result = await svc.poll(sid)
    assert result["status"] == "confirmed"
    assert result["missing"] == []
    assert result["unb"] == "9"
    assert result["nickname"] == "nick@x"  # tracknick 是 URL 编码的
    assert page.seed_calls == 1  # 登录后必须走那一跳
    assert "cookies" not in result  # 轮询响应绝不带 Cookie 值

    stored = await svc.take_cookies(sid)
    assert stored["cookie2"] == "ALI"
    assert await svc._get_session(sid) is None  # 取走即清理，二维码只能确认一次


async def test_poll_keeps_pre_seed_cookies_when_seed_loses_keys():
    """种跳后反而缺关键字段（1688 域覆盖出空值）就退回种跳前那份，不能误报缺登录态。"""
    before = [_rec("cookie2", "TAOBAO"), _rec("unb", "9")]
    after = [_rec("cookie2", "TAOBAO"), _rec("unb", "9"), _rec("cookie2", "", ".1688.com")]
    svc, sid = _service_with(_FakeContext([before, after, after]), _FakePage())

    result = await svc.poll(sid)
    assert result["missing"] == []
    assert (await svc.take_cookies(sid))["cookie2"] == "TAOBAO"


async def test_poll_expired_session_is_dropped():
    """created_at 远超 SESSION_TIMEOUT → 会话连同 context 一起关掉。"""
    ctx = _FakeContext([[]])
    svc, sid = _service_with(ctx, _FakePage(), created_at=0)
    assert (await svc.poll(sid))["status"] == "expired"
    assert ctx.closed is True


# ---------------- 入库 ----------------


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'ali.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    await engine.dispose()


async def _add_user(maker, username, role="user"):
    async with maker() as s:
        u = User(username=username, password_hash=hash_password("passw0rd123"), role=role)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


async def test_upsert_requires_unb(maker):
    async with maker() as s:
        with pytest.raises(AlibabaAccountError, match="unb"):
            await upsert_alibaba_account_from_cookies(s, {"cookie2": "C"})


async def test_upsert_sets_status_and_nickname(maker):
    alice = await _add_user(maker, "alice")
    async with maker() as s:
        row = await upsert_alibaba_account_from_cookies(
            s, {"unb": "888", "cookie2": "C2"}, owner_user_id=alice.id, nickname="小卖部"
        )
        assert (row.cookie_status, row.status, row.nickname) == (1, 1, "小卖部")
        assert row.last_login_at is not None
        assert row.encrypted_cookie.startswith("enc:v1:")


async def test_upsert_revives_soft_deleted_row(maker):
    alice = await _add_user(maker, "alice")
    cookies = {"unb": "888", "cookie2": "C2"}
    async with maker() as s:
        row = await upsert_alibaba_account_from_cookies(s, cookies, owner_user_id=alice.id)
        row_id = row.id
    async with maker() as s:
        (await s.get(AlibabaAccount, row_id)).deleted = 1
        await s.commit()
    async with maker() as s:
        again = await upsert_alibaba_account_from_cookies(s, cookies, owner_user_id=alice.id)
        assert again.id == row_id and again.deleted == 0


async def test_upsert_rejects_cross_owner_binding(maker):
    alice = await _add_user(maker, "alice")
    bob = await _add_user(maker, "bob")
    cookies = {"unb": "888", "cookie2": "C2"}
    async with maker() as s:
        await upsert_alibaba_account_from_cookies(s, cookies, owner_user_id=alice.id)
    async with maker() as s:
        with pytest.raises(AlibabaAccountError, match="其他登录用户"):
            await upsert_alibaba_account_from_cookies(s, cookies, owner_user_id=bob.id)


# ---------------- REST ----------------


def _client_for(maker, user):
    """以指定身份打全量路由（鉴权走 dependency_overrides，不造真 token）。"""
    app = FastAPI()
    app.include_router(api_router)

    async def _get_db():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_scoped_by_owner(maker):
    alice = await _add_user(maker, "alice")
    bob = await _add_user(maker, "bob")
    admin = await _add_user(maker, "root", role="admin")
    async with maker() as s:
        await upsert_alibaba_account_from_cookies(s, {"unb": "A1", "cookie2": "C"}, owner_user_id=alice.id)
        await upsert_alibaba_account_from_cookies(s, {"unb": "B1", "cookie2": "C"}, owner_user_id=bob.id)

    async with _client_for(maker, alice) as client:
        rows = (await client.get("/api/alibaba/accounts")).json()
        assert [r["external_uid"] for r in rows] == ["A1"]
        assert "encrypted_cookie" not in rows[0]  # 列表绝不回传 Cookie

    async with _client_for(maker, admin) as client:
        rows = (await client.get("/api/alibaba/accounts")).json()
        assert sorted(r["external_uid"] for r in rows) == ["A1", "B1"]


async def test_delete_soft_deletes_and_blocks_non_owner(maker):
    alice = await _add_user(maker, "alice")
    bob = await _add_user(maker, "bob")
    async with maker() as s:
        row = await upsert_alibaba_account_from_cookies(
            s, {"unb": "A1", "cookie2": "C"}, owner_user_id=alice.id
        )
        row_id = row.id

    async with _client_for(maker, bob) as client:
        assert (await client.delete(f"/api/alibaba/accounts/{row_id}")).status_code == 403

    async with _client_for(maker, alice) as client:
        assert (await client.delete(f"/api/alibaba/accounts/{row_id}")).json() == {"deleted": row_id}

    async with maker() as s:
        assert (await s.get(AlibabaAccount, row_id)).deleted == 1
        assert (await s.execute(select(AlibabaAccount).where(AlibabaAccount.deleted == 0))).scalar_one_or_none() is None


async def test_poll_rejects_cookies_without_1688_session(maker, monkeypatch):
    """扫码成功但没拿到 unb/cookie2 → 502，且不入库（存进去只会让人误以为已登录）。"""
    alice = await _add_user(maker, "alice")
    from app.api.routes import alibaba as ali_mod

    session_id = "s" * 40

    async def _poll(sid):
        return {"status": "confirmed", "missing": ["unb"]}

    async def _take(sid):
        return {"cookie2": "C2"}

    monkeypatch.setattr(ali_mod.alibaba_qr_login_service, "poll", _poll)
    monkeypatch.setattr(ali_mod.alibaba_qr_login_service, "take_cookies", _take)

    async with _client_for(maker, alice) as client:
        r = await client.get(f"/api/alibaba/qr-login/{session_id}")
        assert r.status_code == 502
        assert "未拿到 1688 登录态" in r.json()["detail"]

    async with maker() as s:
        assert (await s.execute(select(AlibabaAccount))).scalars().all() == []
