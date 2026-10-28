"""S3 定时采集接线测试（collect_1688_task + 检索配额 N7 + /api/collect/schedule）。

替换边界仍只在网络出口（playwright 检索 / httpx 详情页与图片 / chat_json）：handler 的
配置读取、状态→异常映射、配额扣费、runner 的到期与租约链路一律真跑。

★ 验收三句（需求文档 §5 S3 行）：到点自动产出清单、逐件失败记账、配额超限能拒。
再加一句 N5：定时与手动调的是**同一个** `run_collect_pipeline`——所以这里也断言定时
那条路不会放行/上架（绊线同 S2，只是入口换成 scheduler）。
"""

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai.decision import PRICING_SYSTEM
from app.ai.intent import INTENT_SYSTEM, REFINE_SYSTEM, CollectPlan
from app.api.deps import get_db
from app.api.routes import api_router
from app.auth.account_service import encrypt_cookie
from app.collectors.alibaba import search_quota as sq
from app.collectors.alibaba.executor import AlibabaExecutor
from app.collectors.alibaba.search_browser import SearchResult
from app.collectors.alibaba.workspace import CollectWorkspace
from app.collectors.pipeline import run_collect_pipeline
from app.core.config import settings
from app.models import Account, AlibabaAccount, Base, Goods, ScheduledTask, User
from app.scheduler.task_runner import (
    COLLECT_TASK_TYPE,
    TaskRunner,
    build_default_runner,
    collect_1688_task,
    ensure_default_tasks,
)
from app.users.security import hash_password

FIXTURES = Path(__file__).parent / "fixtures" / "alibaba"

THERMOS = "623375582529"
CABLE = "1053709411360"

PRICED = SimpleNamespace(
    pricing="llm", price_cent=1990, orig_price_cent=2990, delivery="包邮",
    confidence="medium", reasoning="成本15.4元，同质化高按约1.3倍取19.9元",
    markup_ratio_actual=1.29,
)
COPIED = SimpleNamespace(
    title_candidates=["1米 Type-C 快充数据线", "type-c数据线 1米 支持快充", "全新1米快充线"],
    desc="成色与规格：全新，长度:1m。\n适合场景：日常充电。\n发货与售后：以所选快递为准。",
)

_buf = io.BytesIO()
Image.new("RGB", (16, 16), "white").save(_buf, "JPEG")
JPEG_BYTES = _buf.getvalue()


def _resolved(v):
    async def _c():
        return v
    return _c()


class _FakeHttp:
    async def get(self, url, **kwargs):
        return SimpleNamespace(
            content=JPEG_BYTES, status_code=200, raise_for_status=lambda: None
        )

    async def aclose(self):
        pass


async def _fake_fetch_detail(self, offer_id):
    return (FIXTURES / f"detail_{offer_id}.html").read_text(encoding="utf-8")


@pytest.fixture
def net(monkeypatch):
    """检索/详情页/延迟三处出口；`plan` 决定"哪个词搜回哪些 ID"，值放异常即模拟检索失败。"""
    box = SimpleNamespace(search=[], plan={})

    async def fake_search(keyword, *, cookie_text=None, limit=20, max_pages=1, **kw):
        box.search.append(keyword)
        ids = box.plan.get(keyword, [])
        if isinstance(ids, Exception):
            raise ids
        return SearchResult(offer_ids=list(ids), source="mtop", pages=1, raw_hits=len(ids))

    monkeypatch.setattr("app.collectors.pipeline.search_offers", fake_search)
    monkeypatch.setattr(AlibabaExecutor, "_fetch_detail", _fake_fetch_detail)
    monkeypatch.setattr(AlibabaExecutor, "_client", lambda self: _resolved(_FakeHttp()))
    monkeypatch.setattr("app.collectors.alibaba.executor.random.uniform", lambda *a: 0)
    return box


def _llm(monkeypatch, *, intent=None, refine=None):
    from app.settings import LlmConfig

    async def _cfg(db):
        return LlmConfig(api_key="sk-test", base_url="https://llm.test/v1", model="m-test")

    monkeypatch.setattr("app.settings.get_llm_config", _cfg)

    async def fake_intent(system, user, model_cls, **kwargs):
        want = intent if system == INTENT_SYSTEM else refine
        if isinstance(want, Exception):
            raise want
        if system == REFINE_SYSTEM:
            kw = want or {}
            return SimpleNamespace(keyword=kw.get("keyword", "x"), stop=kw.get("stop", True),
                                   reason=kw.get("reason", ""), mode="refine")
        return CollectPlan(**(want or {}))

    async def fake_decide(system, user, model_cls, **kwargs):
        return PRICED if system == PRICING_SYSTEM else COPIED

    monkeypatch.setattr("app.ai.intent.chat_json", fake_intent)
    monkeypatch.setattr("app.ai.decision.chat_json", fake_decide)


@pytest_asyncio.fixture
async def db_env(tmp_path, monkeypatch):
    """内存库 + 工作区 + admin/non-admin 各一个闲鱼账号 + 一个可用货源账号。

    handler 不给 pipeline 传 workspace（生产上就是 settings 那个工作区根），所以这里把
    settings 指到 tmp：既让断言看得见批次，也顺带钉住"定时落在同一个工作区根"这条口径
    —— 不这么做的话测试会往真 `data/collect/` 里写批次。
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "collect_workspace_path", str(tmp_path))
    ws = CollectWorkspace(tmp_path)
    async with maker() as s:
        admin = User(username="root", password_hash=hash_password("passw0rd123"), role="admin")
        bob = User(username="bob", password_hash=hash_password("passw0rd123"), role="user")
        s.add_all([admin, bob])
        await s.flush()
        ours = Account(user_id=admin.id, external_uid="unb-1", nickname="shop", status=1)
        theirs = Account(user_id=bob.id, external_uid="unb-2", nickname="bob-shop", status=1)
        s.add_all([ours, theirs])
        await s.flush()
        src = AlibabaAccount(
            external_uid="unb-9", user_id=admin.id, nickname="货源", status=1, cookie_status=1,
            encrypted_cookie=encrypt_cookie("unb=9; cookie2=src"),
        )
        s.add(src)
        await s.commit()
        ids = {
            "account_admin": ours.id, "account_bob": theirs.id,
            "alibaba": src.id, "admin": admin.id,
        }
    yield maker, ws, ids
    await engine.dispose()


async def _pool(maker):
    async with maker() as s:
        rows = (await s.execute(select(Goods).order_by(Goods.id))).scalars().all()
        return [(g.source_offer_id, g.pool_status, g.external_goods_id) for g in rows]


async def _task(maker, **values) -> ScheduledTask:
    """取（或按需建）那条 collect_1688 任务行，并按入参改写字段。"""
    async with maker() as s:
        row = (
            await s.execute(
                select(ScheduledTask).where(ScheduledTask.task_type == COLLECT_TASK_TYPE)
            )
        ).scalar_one_or_none()
        if row is None:
            row = ScheduledTask(
                task_type=COLLECT_TASK_TYPE, cron_expression="*/30 * * * *", enabled=1
            )
            s.add(row)
        for k, v in values.items():
            setattr(row, k, v)
        await s.commit()
        await s.refresh(row)
        return row


async def _set_cookie_status(maker, value: int) -> None:
    async with maker() as s:
        row = (await s.execute(select(AlibabaAccount))).scalar_one()
        row.cookie_status = value
        await s.commit()


@pytest.fixture(autouse=True)
def _clean_quota():
    """窗口是进程内状态：不清会跨用例累计，第 7 次检索莫名其妙被拒在别的用例头上。"""
    sq.search_quota.reset()
    yield
    sq.search_quota.reset()


def _clock(start: float = 1000.0):
    """可手拨的单调时钟：测滑动窗口不靠 sleep。"""
    box = {"t": start}

    def now() -> float:
        return box["t"]

    def advance(seconds: float) -> None:
        box["t"] += seconds

    now.advance = advance  # type: ignore[attr-defined]
    return now


# ---------------------------------------------------------------- 配额本体


def test_quota_counts_each_browser_round():
    """按"开一次浏览器"扣，不是按"跑一次定时"扣：一轮里的第二次换词同样要名额。"""
    q = sq.SearchQuota(2)
    assert q.acquire(7) and q.acquire(7)
    assert q.remaining(7) == 0
    assert q.acquire(7) is False
    assert q.acquire(8) is True, "配额按货源账号隔离：换账号不该被上一个账号用光"


def test_quota_window_slides_and_limit_zero_denies():
    clock = _clock()
    q = sq.SearchQuota(1, clock=clock)
    assert q.acquire(1)
    clock.advance(3599)
    assert q.acquire(1) is False, "窗口未满就不能放行，否则一小时能跑 1.x 轮"
    clock.advance(2)
    assert q.remaining(1) == 1
    assert sq.SearchQuota(0).acquire(1) is False, "配 0 是关掉检索，不是随便跑"


# ---------------------------------------------------------------- handler


@pytest.mark.asyncio
async def test_handler_without_config_does_nothing(db_env, net):
    """没配置就不该开浏览器、不该建批次——补种出来的行默认就是这个状态。"""
    maker, ws, ids = db_env
    task = await _task(maker, config_json=None, enabled=0)
    await collect_1688_task(maker, task)
    assert net.search == [] and await _pool(maker) == [] and ws.list_batches() == []


@pytest.mark.asyncio
async def test_handler_skips_broken_config_without_raising(db_env, net, caplog):
    """配置非法是"人得回去改表单"，不是"系统跑炸了"：记 error 日志但不进连续失败计数。"""
    maker, ws, ids = db_env
    task = await _task(maker, config_json={"account_id": "不存在的账号"})
    with caplog.at_level("ERROR"):
        await collect_1688_task(maker, task)
    assert net.search == [] and await _pool(maker) == []
    assert any("配置非法" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_handler_at_fire_time_produces_decided_list(db_env, net, monkeypatch, caplog):
    """★ 到点自动产出清单：一次 _tick 就把商品备到 DECIDED，且台账记的是 scheduled。"""
    maker, ws, ids = db_env
    net.plan["保温杯"] = [THERMOS]
    _llm(monkeypatch, intent={"keywords": ["保温杯"], "quantity": 1})
    await _task(maker, enabled=1, config_json={
        "account_id": ids["account_admin"], "instruction": "找一款保温杯，卖 19.9",
        "alibaba_account_id": ids["alibaba"],
    })
    runner = TaskRunner(maker)
    runner.register(COLLECT_TASK_TYPE, collect_1688_task)
    with caplog.at_level("INFO"):
        await runner._tick()

    assert [(o, st, ext) for o, st, ext in await _pool(maker)] == [(THERMOS, "DECIDED", None)]
    batch = ws.list_batches()[0]
    assert batch["trigger"] == "scheduled" and batch["instruction"] == "找一款保温杯，卖 19.9"
    assert batch["count"]["added"] == 1 and batch["status"] == "done"
    task = await _task(maker)
    assert task.consecutive_failure_count == 0 and task.last_run_at is not None
    assert any("定时采集完成" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_handler_raises_when_the_list_is_empty(db_env, net, monkeypatch):
    """跑完了却没清单（这里是货源 Cookie 失效）→ 抛错，让连续失败计数替人盯着。"""
    maker, ws, ids = db_env
    _llm(monkeypatch, intent={"keywords": ["保温杯"], "quantity": 1})
    await _set_cookie_status(maker, 0)
    task = await _task(maker, enabled=1, config_json={
        "account_id": ids["account_admin"], "instruction": "找一款保温杯",
    })
    with pytest.raises(RuntimeError, match="no_source_account"):
        await collect_1688_task(maker, task)
    assert net.search == [] and await _pool(maker) == []


@pytest.mark.asyncio
async def test_handler_records_per_item_failures_without_breaking_the_batch(
    db_env, net, monkeypatch, caplog
):
    """★ 逐件记账（N6）的无人值守面：第二件定价炸了，第一件照样备料，原因进日志。

    pipeline 那侧的逐件记账 S2 已测；这里测的是 handler——它才是没人看屏幕时的那道兜底。
    """
    maker, ws, ids = db_env
    import app.collectors.pipeline as pl

    real_decide = pl.decide_offer

    async def flaky(db, *, batch_id, offer_id, **kwargs):
        if offer_id == CABLE:
            raise RuntimeError("假：定价节点炸了")
        return await real_decide(db, batch_id=batch_id, offer_id=offer_id, **kwargs)

    net.plan["数据线"] = [THERMOS, CABLE]
    _llm(monkeypatch, intent={"keywords": ["数据线"], "quantity": 2})
    monkeypatch.setattr(pl, "decide_offer", flaky)
    task = await _task(maker, enabled=1, config_json={
        "account_id": ids["account_admin"], "keyword": "数据线",
    })
    with caplog.at_level("WARNING"):
        await collect_1688_task(maker, task)

    pool = await _pool(maker)
    assert (THERMOS, "DECIDED", None) in pool, "第一件不该被第二件拖死"
    assert all(st != "MAPPED" for _, st, _ in pool), "定时跑完不该出现放行后的状态"
    assert all(ext is None for _, _, ext in pool), "全程不碰平台"
    assert any("单件失败" in r.message and CABLE in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_handler_quota_stops_the_second_round(db_env, net, monkeypatch):
    """★ 配额超限能拒：只剩 1 个名额时第二轮换词开不了浏览器，已搜到的照用。"""
    maker, ws, ids = db_env
    net.plan.update({"数据线": [THERMOS], "typec充电线": [CABLE]})
    _llm(monkeypatch, intent={"keywords": ["数据线"], "quantity": 5},
         refine={"keyword": "typec充电线", "stop": False, "reason": "加规格词"})
    monkeypatch.setattr("app.collectors.alibaba.search_quota.search_quota", sq.SearchQuota(1))
    task = await _task(maker, enabled=1, config_json={
        "account_id": ids["account_admin"], "instruction": "找 5 款数据线",
        "alibaba_account_id": ids["alibaba"],
    })
    await collect_1688_task(maker, task)

    assert net.search == ["数据线"], "第二轮没有名额就不该再开浏览器"
    assert await _pool(maker) == [(THERMOS, "DECIDED", None)]


@pytest.mark.asyncio
async def test_handler_quota_exhausted_before_first_round_raises(db_env, net, monkeypatch):
    """一个名额都没有 → 一件也没搜到，报 blocked 而不是空批次台账。"""
    maker, ws, ids = db_env
    _llm(monkeypatch, intent={"keywords": ["数据线"]})
    monkeypatch.setattr("app.collectors.alibaba.search_quota.search_quota", sq.SearchQuota(0))
    task = await _task(maker, enabled=1, config_json={
        "account_id": ids["account_admin"], "keyword": "数据线",
    })
    with pytest.raises(RuntimeError, match="search_blocked"):
        await collect_1688_task(maker, task)
    assert net.search == [] and ws.list_batches() == []


@pytest.mark.asyncio
async def test_manual_run_is_not_quota_gated(db_env, net, monkeypatch):
    """N7 的分工：手动入口的闸门是人点的那一下，配额只管无人值守（不传配额对象即可）。"""
    maker, ws, ids = db_env
    net.plan["数据线"] = [THERMOS]
    _llm(monkeypatch, intent={"keywords": ["数据线"], "quantity": 1})
    monkeypatch.setattr("app.collectors.alibaba.search_quota.search_quota", sq.SearchQuota(0))
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], keyword="数据线",
            alibaba_account_id=ids["alibaba"], trigger="manual", workspace=ws,
        )
    assert out["status"] == "ok" and net.search == ["数据线"]


# ---------------------------------------------------------------- 接线


def test_default_runner_registers_collect_handler():
    runner = build_default_runner(lambda: None)
    assert runner._handlers[COLLECT_TASK_TYPE] is collect_1688_task


@pytest.mark.asyncio
async def test_ensure_default_tasks_seeds_collect_disabled(db_env):
    """补种出来的定时采集行是关的：没人配过计划之前不该自己开浏览器打 1688。"""
    maker, _, _ = db_env
    await ensure_default_tasks(maker)
    async with maker() as s:
        row = (
            await s.execute(
                select(ScheduledTask).where(ScheduledTask.task_type == COLLECT_TASK_TYPE)
            )
        ).scalar_one()
    assert row.enabled == 0 and row.config_json is None


# ---------------------------------------------------------------- 配置接口


@pytest_asyncio.fixture
async def api(db_env, monkeypatch):
    maker, ws, ids = db_env
    monkeypatch.setattr("app.api.routes.collect.workspace", ws)

    async def _get_db():
        async with maker() as s:
            yield s

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)

    async def _login(username):
        client = AsyncClient(transport=transport, base_url="http://test")
        resp = await client.post(
            "/api/auth/login", json={"username": username, "password": "passw0rd123"}
        )
        assert resp.status_code == 200, resp.text
        client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        return client

    root = await _login("root")
    bob = await _login("bob")
    yield maker, root, bob, ws, ids
    await root.aclose()
    await bob.aclose()


async def test_schedule_put_requires_a_source(api):
    _, root, _, _, ids = api
    resp = await root.put("/api/collect/schedule", json={
        "enabled": True, "config": {"account_id": ids["account_admin"]}
    })
    assert resp.status_code == 400


async def test_schedule_put_rejects_unknown_accounts(api):
    _, root, _, _, ids = api
    assert (await root.put("/api/collect/schedule", json={
        "enabled": True, "config": {"account_id": 999, "keyword": "数据线"}
    })).status_code == 404
    assert (await root.put("/api/collect/schedule", json={
        "enabled": True,
        "config": {"account_id": ids["account_admin"], "keyword": "数据线",
                   "alibaba_account_id": 777},
    })).status_code == 404


async def test_schedule_put_is_admin_only(api):
    _, _, bob, _, ids = api
    resp = await bob.put("/api/collect/schedule", json={
        "enabled": True, "config": {"account_id": ids["account_bob"], "keyword": "数据线"}
    })
    assert resp.status_code == 403


async def test_schedule_round_trip_shows_enabled_interval_and_quota(api):
    maker, root, _, _, ids = api
    put = await root.put("/api/collect/schedule", json={
        "enabled": True,
        "config": {
            "account_id": ids["account_admin"], "instruction": "找几款保温杯，卖 19.9",
            "preference": "要包邮", "alibaba_account_id": ids["alibaba"], "max_items": 3,
        },
    })
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["enabled"] is True and body["interval_seconds"] == 1800
    assert body["config"]["instruction"] == "找几款保温杯，卖 19.9"
    assert body["quota"] == {
        "alibaba_account_id": ids["alibaba"],
        "per_hour": 6, "remaining": 6,
    }

    rows = (await root.get("/api/collect/schedule")).json()
    assert [r["task_id"] for r in rows] == [body["task_id"]], "重复 PUT 只更新那一行"

    off = await root.put("/api/collect/schedule", json={
        "enabled": False, "config": {"account_id": ids["account_admin"], "keyword": "保温杯"}
    })
    assert off.json()["enabled"] is False
    task = await _task(maker)
    assert task.enabled == 0 and task.config_json["keyword"] == "保温杯"
