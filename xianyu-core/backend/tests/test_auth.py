"""系统用户鉴权 + 数据范围测试（阶段 F1）：

覆盖：注册/登录/me、错误密码/停用账号、401 无 token、
user 角色隔离（列表过滤 + 他人账号写操作拒绝）、admin 全量可见。
"""

import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.api.routes import api_router
from app.core.db import get_db
from app.models import Account, Base, User
from app.users.security import hash_password


async def _make_user(maker, username: str, role: str = "user") -> User:
    async with maker() as s:
        u = User(username=username, password_hash=hash_password("passw0rd123"), role=role)
        s.add(u)
        await s.commit()
        await s.refresh(u)
        return u


async def _make_account(maker, owner_user_id: int | None, nickname: str) -> Account:
    from app.models import AccountAuth

    async with maker() as s:
        a = Account(user_id=owner_user_id, external_uid=f"unb-{nickname}", nickname=nickname)
        s.add(a)
        await s.flush()
        s.add(AccountAuth(account_id=a.id, encrypted_cookie="test-cipher"))
        await s.commit()
        await s.refresh(a)
        return a


@pytest_asyncio.fixture
async def ctx(tmp_path):
    db_path = (tmp_path / "auth.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with maker() as s:
            yield s

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = _get_db
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    yield maker, client
    await client.aclose()
    await engine.dispose()


async def _login(client, username: str = "lisi") -> dict:
    resp = await client.post("/api/auth/login", json={"username": username, "password": "passw0rd123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_register_login_me(ctx):
    maker, client = ctx
    resp = await client.post(
        "/api/auth/register", json={"username": "test1", "password": "passw0rd123"}
    )
    assert resp.status_code == 200
    body = resp.json()["user"]
    assert body["role"] == "user"

    # 重复注册 → 409
    resp = await client.post(
        "/api/auth/register", json={"username": "test1", "password": "passw0rd123"}
    )
    assert resp.status_code == 409

    # 弱密码 / 非法用户名
    assert (await client.post("/api/auth/register", json={"username": "x", "password": "short"})).status_code == 422

    headers = await _login(client, "test1")
    resp = await client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["user"]["username"] == "test1"


async def test_login_bad_password_and_disabled(ctx):
    maker, client = ctx
    await _make_user(maker, "tom")
    resp = await client.post("/api/auth/login", json={"username": "tom", "password": "wrongpass1"})
    assert resp.status_code == 401

    resp = await client.post("/api/auth/login", json={"username": "nobody", "password": "passw0rd123"})
    assert resp.status_code == 401

    # 停用账号 → 403
    async with maker() as s:
        u = await s.scalar(select(User).where(User.username == "tom"))
        u.status = 0
        await s.commit()
    resp = await client.post("/api/auth/login", json={"username": "tom", "password": "passw0rd123"})
    assert resp.status_code == 403


async def test_no_token_401(ctx):
    maker, client = ctx
    for path in ("/api/accounts", "/api/goods", "/api/orders", "/api/settings/reply-mode"):
        resp = await client.get(path)
        assert resp.status_code == 401, path


async def test_user_scope_isolation(ctx):
    """普通用户只见自己绑定的账号；他人账号写操作被拒。"""
    maker, client = ctx
    alice = await _make_user(maker, "alice")
    bob = await _make_user(maker, "bob")
    await _make_account(maker, alice.id, "alice-shop")
    bob_shop = await _make_account(maker, bob.id, "bob-shop")

    headers = await _login(client, "alice")
    resp = await client.get("/api/accounts", headers=headers)
    assert resp.status_code == 200
    names = [row["nickname"] for row in resp.json()]
    assert names == ["alice-shop"]

    # 拿他人账号扫码入库 → 归属冲突（直接走 service 层断言）
    from app.auth.account_service import AccountServiceError, upsert_account_from_cookies

    async with maker() as s:
        try:
            await upsert_account_from_cookies(
                s, {"unb": "unb-bob-shop"}, owner_user_id=alice.id
            )
        except AccountServiceError:
            pass
        else:
            raise AssertionError("预期归属冲突未触发")

    # 对他人账号写商品 → 403
    resp = await client.post(
        "/api/goods",
        json={"account_id": bob_shop.id, "title": "hack", "category": "A", "price_cent": 100},
        headers=headers,
    )
    assert resp.status_code in (403, 404), resp.text


async def test_admin_sees_all(ctx):
    maker, client = ctx
    alice = await _make_user(maker, "alice")
    await _make_account(maker, alice.id, "alice-shop")
    admin = await _make_user(maker, "root", role="admin")

    headers = await _login(client, "root")
    resp = await client.get("/api/accounts", headers=headers)
    assert {r["nickname"] for r in resp.json()} == {"alice-shop"}

    # admin 可改全局 reply-mode
    resp = await client.put("/api/settings/reply-mode", json={"mode": "hybrid"}, headers=headers)
    assert resp.status_code == 200


async def test_user_cannot_change_reply_mode(ctx):
    maker, client = ctx
    await _make_user(maker, "lisi")
    headers = await _login(client, "lisi")
    resp = await client.put("/api/settings/reply-mode", json={"mode": "manual"}, headers=headers)
    assert resp.status_code == 403


async def test_account_id_filter_on_list_endpoints(ctx):
    """顶栏「当前账号」要求列表接口能按 account_id 收窄。

    越权/无数据的号返回空列表而不是 403：这一层只是筛选，可见域已由 visible_account_ids
    兜住，报错会让前端切成"加载失败"而看不出是没数据。
    """
    from app.models import CardGroup, Goods

    maker, client = ctx
    alice = await _make_user(maker, "alice")
    a1 = await _make_account(maker, alice.id, "a1")
    a2 = await _make_account(maker, alice.id, "a2")
    bob = await _make_user(maker, "bob")
    b1 = await _make_account(maker, bob.id, "b1")
    await _make_user(maker, "root", role="admin")

    async with maker() as s:
        g1 = Goods(account_id=a1.id, category="A", title="a1-商品", price_cent=100)
        s.add(g1)
        s.add(Goods(account_id=a2.id, category="A", title="a2-商品", price_cent=200))
        await s.flush()
        s.add_all([CardGroup(name="绑到a1", goods_id=g1.id), CardGroup(name="未绑商品")])
        await s.commit()

    root = await _login(client, "root")
    assert {r["title"] for r in (await client.get("/api/goods", headers=root)).json()} == {
        "a1-商品",
        "a2-商品",
    }
    only = (await client.get("/api/goods", params={"account_id": a1.id}, headers=root)).json()
    assert [r["title"] for r in only] == ["a1-商品"]
    assert only[0]["account_id"] == a1.id

    # 未绑商品的卡密组没有归属号，切到任何单号视图下都得看得见，否则库存像凭空少了一批
    cards = (await client.get("/api/delivery/cards", params={"account_id": a1.id}, headers=root)).json()
    assert {c["name"] for c in cards} == {"绑到a1", "未绑商品"}
    by_name = {c["name"]: c for c in cards}
    assert by_name["绑到a1"]["account_id"] == a1.id
    assert by_name["未绑商品"]["account_id"] is None

    headers = await _login(client, "alice")
    got = (await client.get("/api/goods", params={"account_id": a1.id}, headers=headers)).json()
    assert [r["title"] for r in got] == ["a1-商品"]
    # 别人的号在自己的视图里 = 空，而不是 403
    assert (await client.get("/api/goods", params={"account_id": b1.id}, headers=headers)).json() == []