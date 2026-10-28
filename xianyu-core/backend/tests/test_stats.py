"""工作台统计接口测试（阶段 F2）：user 只见自己的数据，admin 全量。

统计口径见 app/api/routes/stats.py（待上架=status0、今日=本地时 0 点起）。
"""

import time
from datetime import datetime

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.api.routes import api_router
from app.core.db import get_db
from app.models import (
    Account,
    AccountAuth,
    Base,
    ChatMessage,
    Goods,
    TradeOrder,
    User,
)
from app.users.security import hash_password


@pytest_asyncio.fixture
async def ctx(tmp_path):
    db_path = (tmp_path / "stats.db").as_posix()
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


async def _seed(maker):
    """alice(1 个启用账号 + 数据) / bob(1 个账号) / admin。"""
    async with maker() as s:
        alice_u = User(username="alice", password_hash=hash_password("passw0rd123"), role="user")
        bob_u = User(username="bob", password_hash=hash_password("passw0rd123"), role="user")
        admin_u = User(username="root", password_hash=hash_password("passw0rd123"), role="admin")
        s.add_all([alice_u, bob_u, admin_u])
        await s.flush()

        a1 = Account(user_id=alice_u.id, external_uid="unb-a1", nickname="alice-shop", status=1)
        a2 = Account(user_id=bob_u.id, external_uid="unb-a2", nickname="bob-shop", status=1)
        a3 = Account(user_id=alice_u.id, external_uid="unb-a3", nickname="alice-off", status=0)
        s.add_all([a1, a2, a3])
        await s.flush()
        s.add_all(
            [
                AccountAuth(account_id=a1.id, encrypted_cookie="x", cookie_status=1),
                AccountAuth(account_id=a2.id, encrypted_cookie="x", cookie_status=1),
                AccountAuth(account_id=a3.id, encrypted_cookie="x", cookie_status=0),
            ]
        )
        # alice 数据
        s.add(Goods(account_id=a1.id, category="A", title="待上架1", price_cent=100, status=0))
        s.add(Goods(account_id=a1.id, category="A", title="在售1", price_cent=200, status=1))
        # 采集池：alice 1 件待确认（DECIDED）+ bob 1 件已放行（MAPPED，不该进 alice 的计数）
        s.add(
            Goods(account_id=a1.id, category="B", source="1688", title="待确认1",
                  price_cent=300, status=0, pool_status="DECIDED")
        )
        s.add(
            Goods(account_id=a2.id, category="B", source="1688", title="已放行1",
                  price_cent=400, status=0, pool_status="MAPPED")
        )
        s.add(
            TradeOrder(
                account_id=a1.id, order_id="o1", category="A", total_amount_cent=100,
                order_status=2, paid_at=datetime.now(),
            )
        )
        s.add(
            ChatMessage(
                account_id=a1.id, conversation_id=1, pnm_id="m1", content_type=0,
                direction="IN", content="hi", message_time=int(time.time() * 1000),
            )
        )
        await s.commit()
    return "alice"


async def _login(client, username):
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "passw0rd123"}
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_user_stats_scoped(ctx):
    maker, client = ctx
    await _seed(maker)
    headers = await _login(client, "alice")
    resp = await client.get("/api/stats/overview", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    # alice 名下：启用 1（a1，a3 停用）＋ 全 3 员
    assert body["accounts"]["total"] == 2
    assert body["accounts"]["enabled"] == 1
    assert body["accounts"]["cookie_ok"] == 1
    assert body["goods"] == {
        "pending_publish": 2, "on_sale": 1, "to_confirm": 1, "approved": 0
    }
    assert body["orders"]["pending_delivery"] == 1
    assert body["orders"]["today"] == 1
    assert body["messages"]["today_in"] == 1


async def test_admin_stats_full_and_guest_401(ctx):
    maker, client = ctx
    await _seed(maker)
    resp = await client.get("/api/stats/overview")
    assert resp.status_code == 401

    headers = await _login(client, "root")
    resp = await client.get("/api/stats/overview", headers=headers)
    body = resp.json()
    assert body["accounts"]["total"] == 3
    assert body["accounts"]["enabled"] == 2
    # admin 全量：alice 的 DECIDED + bob 的 MAPPED 都计到
    assert body["goods"]["to_confirm"] == 1
    assert body["goods"]["approved"] == 1


async def test_stats_account_id_narrows_whole_page(ctx):
    """顶栏切到单个号 → 工作台每一张卡都只算那个号，越权号返回全零而不是 403。"""
    maker, client = ctx
    await _seed(maker)
    async with maker() as s:
        a1 = await s.scalar(select(Account.id).where(Account.external_uid == "unb-a1"))
        a2 = await s.scalar(select(Account.id).where(Account.external_uid == "unb-a2"))

    headers = await _login(client, "root")
    body = (await client.get("/api/stats/overview", params={"account_id": a1}, headers=headers)).json()
    assert body["accounts"] == {"total": 1, "enabled": 1, "cookie_ok": 1}
    assert body["goods"] == {"pending_publish": 2, "on_sale": 1, "to_confirm": 1, "approved": 0}
    assert body["orders"]["today"] == 1
    assert body["messages"]["today_in"] == 1

    # a2 归 bob，alice 切过去只能看到一组零——前端据此显示"这个号今天没数据"
    headers = await _login(client, "alice")
    body = (await client.get("/api/stats/overview", params={"account_id": a2}, headers=headers)).json()
    assert body["accounts"]["total"] == 0
    assert body["goods"]["pending_publish"] == 0
    assert body["messages"]["today_in"] == 0