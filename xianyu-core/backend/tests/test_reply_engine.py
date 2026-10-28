"""AI 回复引擎测试：内存 SQLite + 假 LLM/假发送器，验证完整事件流。

覆盖：入库幂等、自发过滤、只回文本、旧消息不回、manual 模式不回、
回复入库 + 会话状态机更新、按账号人设（每个闲鱼号回复风格不同）。
"""

import time
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select

from app.ai.reply_engine import ReplyEngine
from app.messaging.events import Event, EventBus, EventType
from app.models import Base, ChatMessage, Conversation


class FakeProvider:
    def __init__(self, text: str = "在的，亲~"):
        self.text = text
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        from app.ai.provider import LlmResult

        return LlmResult(ok=True, text=self.text)


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[int, str, str]] = []

    async def __call__(self, account_id: int, peer_id: str, text: str) -> bool:
        self.sent.append((account_id, peer_id, text))
        return True


class LocalBus(EventBus):
    """同步等待 handler 完成的总线（测试用，避免 create_task 时序问题）。"""

    async def publish(self, event: Event) -> None:
        for handler in self._handlers.get(event.type, []):
            await self._safe_run(handler, event)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def bus():
    return LocalBus()


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def engine(session_factory, provider, sender, bus):
    eng = ReplyEngine(
        session_factory,
        provider=provider,
        sender=sender,
        uid_lookup=lambda _aid: "seller001",
        bus=bus,
    )
    eng.start()
    return eng


def _message_event(
    *,
    account_id: int = 1,
    sender: str = "buyer001",
    pnm_id: str = "pnm-1",
    content_type: int = 1,
    content: str = "在吗",
    message_time: int | None = None,
) -> Event:
    return Event(
        type=EventType.MESSAGE_RECEIVED,
        account_id=account_id,
        payload={
            "sId": "47812870000",
            "pnmId": pnm_id,
            "senderUserId": sender,
            "senderUserName": "买家小明",
            "contentType": content_type,
            "msgContent": content,
            "messageTime": message_time or int(time.time() * 1000),
            "xyGoodsId": "",
        },
    )


async def test_full_reply_flow(session_factory, engine, sender, provider):
    await bus_publish(engine, _message_event())

    # ① 发送器被调用，内容来自 LLM
    assert sender.sent == [(1, "buyer001", "在的，亲~")]

    # ② 入库：1 条 IN + 1 条 OUT（is_auto_reply=1）
    async with session_factory() as db:
        msgs = (await db.execute(select(ChatMessage).order_by(ChatMessage.id))).scalars().all()
        assert [m.direction for m in msgs] == ["IN", "OUT"]
        assert msgs[0].is_auto_reply == 0
        assert msgs[1].is_auto_reply == 1
        assert msgs[1].content == "在的，亲~"

        # ③ 会话已建立，昵称回填，last_auto_reply_at 更新
        conv = (await db.execute(select(Conversation))).scalar_one()
        assert conv.peer_id == "buyer001"
        assert conv.peer_name == "买家小明"
        assert conv.last_auto_reply_at is not None

    # ④ prompt 带 system + 历史
    assert provider.calls
    messages = provider.calls[0]
    assert messages[0]["role"] == "system"
    assert any(m["role"] == "user" and m["content"] == "在吗" for m in messages[1:])


async def test_duplicate_pnm_not_replied(session_factory, engine, sender):
    await bus_publish(engine, _message_event(pnm_id="pnm-dup"))
    await bus_publish(engine, _message_event(pnm_id="pnm-dup"))
    assert len(sender.sent) == 1
    async with session_factory() as db:
        count = len((await db.execute(select(ChatMessage))).scalars().all())
        assert count == 2  # 1 IN + 1 OUT，重复推送未再入库


async def test_no_pnm_sync_message_deduped_by_content(session_factory, engine, sender):
    """WS 重连回放的同步消息无 pnmId：按 账号+会话+内容 去重，只落一条、只回一次。"""
    await bus_publish(engine, _message_event(pnm_id=""))
    await bus_publish(engine, _message_event(pnm_id=""))
    assert len(sender.sent) == 1
    async with session_factory() as db:
        msgs = (
            await db.execute(select(ChatMessage).order_by(ChatMessage.id))
        ).scalars().all()
        assert len(msgs) == 2  # 1 IN + 1 OUT
        assert msgs[0].pnm_id.startswith("gen-")


async def test_no_pnm_different_content_both_stored(session_factory, engine, sender):
    await bus_publish(engine, _message_event(pnm_id="", content="在吗"))
    await bus_publish(engine, _message_event(pnm_id="", content="价格多少"))
    async with session_factory() as db:
        ins = (
            await db.execute(select(ChatMessage).where(ChatMessage.direction == "IN"))
        ).scalars().all()
        assert {m.content for m in ins} == {"在吗", "价格多少"}


async def test_no_pnm_empty_content_deduped_by_time(session_factory, engine, sender):
    """空内容系统卡片无文本指纹：按 会话+类型+messageTime 去重。"""
    t = 1789112533000
    await bus_publish(engine, _message_event(pnm_id="", content="", content_type=8, message_time=t))
    await bus_publish(engine, _message_event(pnm_id="", content="", content_type=8, message_time=t))
    async with session_factory() as db:
        assert len((await db.execute(select(ChatMessage))).scalars().all()) == 1


async def test_self_message_ignored(session_factory, engine, sender):
    await bus_publish(engine, _message_event(sender="seller001"))
    assert sender.sent == []
    async with session_factory() as db:
        assert (await db.execute(select(ChatMessage))).scalars().first() is None


async def test_image_message_stored_but_not_replied(session_factory, engine, sender):
    await bus_publish(engine, _message_event(content_type=2, content="https://img/1.jpg"))
    assert sender.sent == []
    async with session_factory() as db:
        msg = (await db.execute(select(ChatMessage))).scalars().one()
        assert msg.direction == "IN"
        assert msg.content_type == 2


async def test_stale_message_not_replied(session_factory, engine, sender):
    old = int((time.time() - 3600) * 1000)  # 1 小时前
    await bus_publish(engine, _message_event(message_time=old))
    assert sender.sent == []
    async with session_factory() as db:
        msg = (await db.execute(select(ChatMessage))).scalars().one()  # 仍入库
        assert msg.direction == "IN"


async def test_manual_mode_not_replied(session_factory, engine, sender, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ai_reply_mode", "manual")
    await bus_publish(engine, _message_event())
    assert sender.sent == []
    async with session_factory() as db:
        # manual 模式：消息照常入库，只是不回
        msg = (await db.execute(select(ChatMessage))).scalars().one()
        assert msg.direction == "IN"


async def test_llm_failure_silent(session_factory, engine, sender, provider):
    from app.ai.provider import LlmResult

    async def fail(messages, **kwargs):
        return LlmResult(ok=False, error="boom")

    provider.chat = fail
    await bus_publish(engine, _message_event())
    assert sender.sent == []
    async with session_factory() as db:
        # 生成失败降级为不回复，但 IN 消息已入库
        msg = (await db.execute(select(ChatMessage))).scalars().one()
        assert msg.direction == "IN"


async def test_min_interval_prevents_burst(session_factory, engine, sender):
    await bus_publish(engine, _message_event(pnm_id="p1", content="在吗"))
    await bus_publish(engine, _message_event(pnm_id="p2", content="还在吗"))
    # 同会话 8s 内第二条被"间隔过短"闸门拦截
    assert len(sender.sent) == 1


async def bus_publish(engine: ReplyEngine, event: Event) -> None:
    """直接走 engine 的 handler（LocalBus 的 publish 亦可，这里省一层）。"""
    await engine.handle_message(event)


# ---------- 按账号人设（全局打底 + 账号叠加） ----------


async def seed_personas(session_factory, *, base_prompt: str, personas: dict[int, str]) -> None:
    from app.models import AccountReplyConfig, AppSetting

    async with session_factory() as db:
        db.add(AppSetting(key="ai_system_prompt", value=base_prompt))
        for account_id, persona in personas.items():
            db.add(AccountReplyConfig(account_id=account_id, persona_prompt=persona))
        await db.commit()


async def test_each_account_gets_its_own_prompt(session_factory, engine, provider):
    await seed_personas(
        session_factory,
        base_prompt="你是闲鱼卖家客服",
        personas={1: "风格A：只说短句，称呼对方亲", 2: "风格B：东北口音，爱用整句方言"},
    )
    await bus_publish(engine, _message_event(account_id=1, pnm_id="a1"))
    await bus_publish(engine, _message_event(account_id=2, pnm_id="a2", sender="buyer002"))

    first, second = provider.calls[0][0]["content"], provider.calls[1][0]["content"]
    assert first.startswith("你是闲鱼卖家客服") and "风格A" in first and "风格B" not in first
    assert second.startswith("你是闲鱼卖家客服") and "风格B" in second and "风格A" not in second


async def test_account_without_persona_uses_global_only(session_factory, engine, provider):
    await seed_personas(
        session_factory, base_prompt="你是闲鱼卖家客服", personas={2: "风格B"}
    )
    await bus_publish(engine, _message_event(account_id=1, pnm_id="b1"))
    assert provider.calls[0][0]["content"] == "你是闲鱼卖家客服"


def test_compose_prompt_keeps_base_when_persona_blank():
    from app.settings import compose_system_prompt

    assert compose_system_prompt("全局", None) == "全局"
    assert compose_system_prompt("全局", "   ") == "全局"
    merged = compose_system_prompt("全局", "人设")
    assert "全局" in merged and "人设" in merged
