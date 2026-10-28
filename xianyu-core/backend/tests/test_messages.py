"""消息查看接口测试：账号过滤 + 会话列表（消息页按账号切换、按买家下钻）。"""

import time

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_db
from app.api.routes import api_router
from app.core.db import get_db
from app.models import Account, Base, ChatMessage, Conversation, User
from app.users.security import hash_password


@pytest_asyncio.fixture
async def ctx(tmp_path):
    db_path = (tmp_path / "messages.db").as_posix()
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
    """alice 两个闲鱼号（a1/a2）各一个买家会话；bob 一个号（a3）隐藏会话。"""
    now_ms = int(time.time() * 1000)
    async with maker() as s:
        alice = User(username="alice", password_hash=hash_password("passw0rd123"), role="user")
        bob = User(username="bob", password_hash=hash_password("passw0rd123"), role="user")
        s.add_all([alice, bob])
        await s.flush()
        a1 = Account(user_id=alice.id, external_uid="unb-a1", nickname="shop-one", status=1)
        a2 = Account(user_id=alice.id, external_uid="unb-a2", nickname="shop-two", status=1)
        a3 = Account(user_id=bob.id, external_uid="unb-a3", nickname="bob-shop", status=1)
        s.add_all([a1, a2, a3])
        await s.flush()

        c1 = Conversation(account_id=a1.id, peer_id="buyer-1", peer_name="买家一")
        c2 = Conversation(account_id=a2.id, peer_id="buyer-2")  # 无昵称，回落 peer_id
        c3 = Conversation(account_id=a3.id, peer_id="buyer-3", peer_name="bob家的")
        s.add_all([c1, c2, c3])
        await s.flush()
        s.add_all(
            [
                ChatMessage(
                    account_id=a1.id, conversation_id=c1.id, pnm_id="m-a1-old",
                    content_type=1, direction="IN", content="在吗", message_time=now_ms - 60_000,
                ),
                ChatMessage(
                    account_id=a1.id, conversation_id=c1.id, pnm_id="m-a1-new",
                    content_type=1, direction="OUT", content="在的", is_auto_reply=1,
                    message_time=now_ms,
                ),
                ChatMessage(
                    account_id=a2.id, conversation_id=c2.id, pnm_id="m-a2",
                    content_type=1, direction="IN", content="怎么卖", message_time=now_ms - 30_000,
                ),
                ChatMessage(
                    account_id=a3.id, conversation_id=c3.id, pnm_id="m-a3",
                    content_type=1, direction="IN", content="bob 的消息", message_time=now_ms,
                ),
            ]
        )
        await s.commit()
        return {"a1": a1.id, "a2": a2.id, "a3": a3.id, "c1": c1.id, "c2": c2.id, "c3": c3.id}


async def _login(client, username):
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "passw0rd123"}
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_messages_filter_by_account(ctx):
    maker, client = ctx
    ids = await _seed(maker)
    headers = await _login(client, "alice")

    resp = await client.get("/api/messages", params={"account_id": ids["a2"]}, headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1 and rows[0]["account_id"] == ids["a2"]
    assert rows[0]["content"] == "怎么卖"

    resp = await client.get("/api/messages", headers=headers)
    mine = {r["account_id"] for r in resp.json()}
    assert ids["a3"] not in mine  # 不越权看 bob 的消息


async def test_conversations_list_and_scoping(ctx):
    maker, client = ctx
    ids = await _seed(maker)
    headers = await _login(client, "alice")

    resp = await client.get("/api/messages/conversations", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["id"] for r in rows} == {ids["c1"], ids["c2"]}  # c3 属 bob，不可见
    newest = rows[0]  # 按最新消息倒序：c1 的最后一条比 c2 新
    assert newest["id"] == ids["c1"]
    assert newest["peer_name"] == "买家一"
    assert newest["last_content"] == "在的"
    assert newest["last_direction"] == "OUT"
    assert newest["message_count"] == 2

    by_name = {r["id"]: r for r in rows}
    assert by_name[ids["c2"]]["peer_id"] == "buyer-2"
    assert by_name[ids["c2"]]["last_content"] == "怎么卖"

    resp = await client.get(
        "/api/messages/conversations", params={"account_id": ids["a2"]}, headers=headers
    )
    assert [r["id"] for r in resp.json()] == [ids["c2"]]
