"""S2 自然语言采集编排测试（app/ai/intent.py + app/collectors/pipeline.py + POST /api/collect/run）。

替换边界只放在**网络出口**：playwright 检索、httpx 详情页/图片、LLM 的 chat_json。
`run_collect_pipeline` 里的控环（轮数上限、追问、逐件记账）一律真跑——
parser 坏了三个月没人发现那次，就是因为单测把被测函数整个 patch 掉了。

★ 验收核心（需求文档 N4/N5 + D-2026-09-19-17）：整条链路跑到 DECIDED 就停，全程不得
出现 approve / publish。`test_pipeline_has_no_approve_or_publish_path` 用 AST 源码扫描 +
运行期绊线两层各锁一次。
"""

import ast
import inspect
import io
import json
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
from app.ai.intent import INTENT_SYSTEM, REFINE_SYSTEM, CollectPlan, parse_intent, rule_parse
from app.ai.structured import StructuredOutputError
from app.api.deps import get_db
from app.api.routes import api_router
from app.auth.account_service import encrypt_cookie
from app.collectors.alibaba import search_browser as sb
from app.collectors.alibaba.executor import AlibabaExecutor
from app.collectors.alibaba.workspace import CollectWorkspace
from app.collectors.pipeline import MAX_SEARCH_ROUNDS, run_collect_pipeline
from app.models import Account, AlibabaAccount, Base, Goods, User
from app.users.security import hash_password

FIXTURES = Path(__file__).parent / "fixtures" / "alibaba"

THERMOS = "623375582529"      # 成本 1540 分，PRICED 的 1990 分落在护栏区间内
CABLE = "1053709411360"       # 成本 250 分，1990 越界 → 走规则兜底，顺带验护栏
RISK_ID = "999000000099"      # 假 ID：详情页返回风控壳

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

# 放行/上架的符号名：编排链路与采集路由里一个都不该出现
GATE_NAMES = ("approve_offers", "publish_goods", "auto_listing", "session_pool")


def read_detail(offer_id: str) -> str:
    return (FIXTURES / f"detail_{offer_id}.html").read_text(encoding="utf-8")


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
    if offer_id == RISK_ID:
        return "<html>x5secdata …_____tmd_____…</html>"
    return read_detail(offer_id)


@pytest.fixture
def net(monkeypatch):
    """把三处对外出口换成假件：检索（浏览器）、详情页与图片（httpx）、发牌延迟。

    `plan` 决定"哪个关键词搜回哪些 ID"，值放异常即模拟该词检索失败。
    """
    box = SimpleNamespace(search=[], plan={}, collected_with=[])

    async def fake_search(keyword, *, cookie_text=None, limit=20, max_pages=1, **kw):
        box.search.append({"keyword": keyword, "cookie": cookie_text, "limit": limit,
                           "max_pages": max_pages})
        ids = box.plan.get(keyword, [])
        if isinstance(ids, Exception):
            raise ids
        return sb.SearchResult(offer_ids=list(ids), source="mtop", pages=1, raw_hits=len(ids))

    monkeypatch.setattr("app.collectors.pipeline.search_offers", fake_search)
    monkeypatch.setattr(AlibabaExecutor, "_fetch_detail", _fake_fetch_detail)
    monkeypatch.setattr(AlibabaExecutor, "_client", lambda self: _resolved(_FakeHttp()))
    # 详情页之间 1–3s 人性化延迟（延迟本身是 M1 测过的），测试里掐成 0
    monkeypatch.setattr("app.collectors.alibaba.executor.random.uniform", lambda *a: 0)
    return box


def _llm(monkeypatch, *, intent=None, refine=None, decide_ok=True):
    """按 system 常量分派假模型回复；值放异常即模拟该节点不可用。返回意图调用记录。"""
    from app.settings import LlmConfig

    calls: list = []

    async def _cfg(db):
        return LlmConfig(api_key="sk-test", base_url="https://llm.test/v1", model="m-test")

    monkeypatch.setattr("app.settings.get_llm_config", _cfg)

    async def fake_intent(system, user, model_cls, **kwargs):
        calls.append({"system": system, "user": user})
        want = intent if system == INTENT_SYSTEM else refine
        if isinstance(want, Exception):
            raise want
        if system == REFINE_SYSTEM:
            kw = want or {}
            return SimpleNamespace(keyword=kw.get("keyword", "x"), stop=kw.get("stop", True),
                                   reason=kw.get("reason", ""), mode="refine")
        return CollectPlan(**(want or {}))

    async def fake_decide(system, user, model_cls, **kwargs):
        if not decide_ok:
            raise StructuredOutputError("假：LLM 端点不可用")
        return PRICED if system == PRICING_SYSTEM else COPIED

    monkeypatch.setattr("app.ai.intent.chat_json", fake_intent)
    monkeypatch.setattr("app.ai.decision.chat_json", fake_decide)
    return calls


def _gate_symbols(mod) -> set[str]:
    """AST 取 import 名与被调函数名——docstring 里写"不 import approve_offers"不该算命中。"""
    tree = ast.parse(Path(inspect.getsourcefile(mod) or mod.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return names


@pytest_asyncio.fixture
async def db_env(tmp_path):
    """(maker, workspace)：admin + 一个非 admin（越权用例）+ 闲鱼账号 + 可用货源账号。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
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
        s.add(
            AlibabaAccount(
                external_uid="unb-9", user_id=admin.id, nickname="货源", status=1, cookie_status=1,
                encrypted_cookie=encrypt_cookie("unb=9; cookie2=src"),
            )
        )
        await s.commit()
    ids = {"account_admin": ours.id, "account_bob": theirs.id, "alibaba": 1}
    yield maker, ws, ids
    await engine.dispose()


async def _pool(maker) -> list[tuple[str, str, str | None]]:
    async with maker() as s:
        rows = (await s.execute(select(Goods).order_by(Goods.id))).scalars().all()
        return [(g.source_offer_id, g.pool_status, g.external_goods_id) for g in rows]


async def _batches(ws):
    return [b["batch_id"] for b in ws.list_batches()]


# ---------------------------------------------------------------- 意图层


def test_rule_parse_reads_ids_and_yuan_but_never_keywords():
    """兜底档只认字面明确的东西；猜品类是幻觉高发区，缺词就让它缺。"""
    plan = rule_parse(
        f"把 https://detail.1688.com/offer/{THERMOS}.html 和 {CABLE} 采上来，成本 8 元，卖 19.9 元"
    )
    assert plan.offer_ids == [THERMOS]      # 只有 URL 形式算"明确给出"
    assert plan.cost_max_cent == 800 and plan.target_price_cent == 1990
    assert plan.keywords == [] and plan.executable

    vague = rule_parse("帮我找几款适合夏天卖的便宜小东西")
    assert vague.keywords == [] and vague.offer_ids == []
    assert vague.executable is False


def test_collect_plan_hard_boundaries_reject_model_overreach():
    """pydantic 是硬边界：模型给 99 件要当场拒，而不是带着跑出去多烧 96 次详情页。"""
    with pytest.raises(Exception):
        CollectPlan(keywords=["数据线"], quantity=99)
    with pytest.raises(Exception):
        CollectPlan(keywords=["a", "b", "c", "d"])          # 最多 3 个词
    plan = CollectPlan(keywords=["  数据线  ", "数据线", "x" * 90], offer_ids=["12ab", CABLE])
    assert plan.keywords[0] == "数据线" and "  数据线  " not in plan.keywords
    assert all(len(k) <= 40 for k in plan.keywords)
    assert plan.offer_ids == [CABLE]                        # 非纯数字丢掉


async def test_parse_intent_labels_llm_vs_rule(monkeypatch):
    """intent_source 必须如实：rule 档关键词那栏是空的，不能伪装成"已经理解你了"。"""
    _llm(monkeypatch, intent={"keywords": ["保温杯"], "quantity": 5, "target_price_cent": 1990})
    out = await parse_intent(None, "找 5 款保温杯，卖 19.9")
    assert out["intent_source"] == "llm"
    assert out["plan"]["keywords"] == ["保温杯"] and out["plan"]["quantity"] == 5
    assert out["needs_clarification"] == []

    _llm(monkeypatch, intent=StructuredOutputError("端点 502"))
    out2 = await parse_intent(None, "找几款夏天的小东西")
    assert out2["intent_source"] == "rule"
    assert out2["plan"]["keywords"] == []
    assert out2["needs_clarification"], "缺词却不追问，等于替用户猜品类"


async def test_parse_intent_with_empty_text_asks_without_calling_llm(monkeypatch):
    calls = _llm(monkeypatch, intent=AssertionError("空指令不该花钱"))
    out = await parse_intent(None, "   ")
    assert out["intent_source"] == "none"
    assert out["needs_clarification"]
    assert calls == []


async def test_intent_provider_comes_from_runtime_config(monkeypatch):
    """沿用 D-2026-09-19-12 那条回归：意图解析也不能用 import 期的 env 单例。"""
    from app.ai.provider import runtime_provider
    from app.settings import LlmConfig

    async def _cfg(db):
        return LlmConfig(api_key="sk-page", base_url="https://llm.example/v1", model="m-42")

    monkeypatch.setattr("app.settings.get_llm_config", _cfg)
    prov = await runtime_provider(None)
    assert (prov.api_key, prov.base_url, prov.model) == ("sk-page", "https://llm.example/v1", "m-42")


# ---------------------------------------------------------------- 编排层


async def test_pipeline_explicit_offer_ids_end_at_decided(db_env, net, monkeypatch):
    """给了现成 ID 就不必再花一次 LLM：intent_source=explicit，落库停在 DECIDED。"""
    maker, ws, ids = db_env
    _llm(monkeypatch, intent=AssertionError("explicit 通道不该调意图模型"))
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], offer_ids=[THERMOS], workspace=ws
        )
    assert out["status"] == "ok"
    assert out["intent_source"] == "explicit"
    assert out["collected"]["added"] == 1
    assert net.search == []
    assert out["reviewable"] == [out["decided"][0]]
    assert out["decided"][0]["pricing"] == "llm" and out["decided"][0]["risk_passed"] is True
    assert await _pool(maker) == [(THERMOS, "DECIDED", None)]


async def test_pipeline_has_no_approve_or_publish_path(db_env, net, monkeypatch):
    """硬停点：源码里没有放行/上架入口，跑完状态也停在 DECIDED，运行期绊线也没响。"""
    import app.api.routes.collect as collect_route
    import app.collectors.pipeline as pl

    gate_in_pipeline = _gate_symbols(pl)
    for token in GATE_NAMES:
        assert token not in gate_in_pipeline, f"编排模块不该出现 {token}"
    route_symbols = _gate_symbols(collect_route)
    assert not (set(GATE_NAMES) & route_symbols)

    import app.collectors.pool_flow as pf
    import app.listing.publisher as pub

    trips: list[str] = []

    async def trip(name):
        trips.append(name)
        raise AssertionError(f"闸门被绕过：{name} 被调用了")

    monkeypatch.setattr(pf, "approve_offers", lambda *a, **k: trip("approve_offers"))
    monkeypatch.setattr(pub, "publish_goods", lambda *a, **k: trip("publish_goods"))

    maker, ws, ids = db_env
    _llm(monkeypatch, intent={"keywords": ["保温杯"]})
    net.plan["保温杯"] = [THERMOS]
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], instruction="找一款保温杯", workspace=ws
        )
    assert out["status"] == "ok"
    assert trips == []
    assert await _pool(maker) == [(THERMOS, "DECIDED", None)]


async def test_pipeline_stops_before_browser_without_source_account(db_env, net, monkeypatch):
    """没有可用货源账号时在启动浏览器之前就返回（N8），也省掉一次意图 LLM。"""
    maker, ws, ids = db_env
    async with maker() as s:
        acct = (await s.execute(select(AlibabaAccount))).scalar_one()
        acct.cookie_status = 2      # 已过期
        await s.commit()

    calls = _llm(monkeypatch, intent=AssertionError("不该调模型"))
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], instruction="找几款保温杯", workspace=ws
        )
    assert out["status"] == "no_source_account"
    assert net.search == []
    assert calls == []
    assert await _pool(maker) == []


async def test_pipeline_instruction_flows_through_llm_into_search(db_env, net, monkeypatch):
    """一句话 → 模型出词 → 用那个词检索；Cookie 取的是货源账号，不是发布账号。"""
    maker, ws, ids = db_env
    net.plan["保温杯"] = [THERMOS, CABLE]
    calls = _llm(monkeypatch, intent={"keywords": ["保温杯", "随手杯"], "quantity": 2})
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], instruction="找两款保温杯，卖 19.9", workspace=ws
        )
    assert out["intent_source"] == "llm"
    assert [c["keyword"] for c in net.search] == ["保温杯"], "第二个词不该在够数时再搜"
    assert "找两款保温杯，卖 19.9" in calls[0]["user"]
    assert net.search[0]["limit"] == 2
    assert "cookie2=src" in net.search[0]["cookie"]
    assert out["searched"]["rounds"][0]["found"] == 2
    assert out["collected"]["added"] == 2


async def test_pipeline_search_loop_refines_once_and_caps_rounds(db_env, net, monkeypatch):
    """第 1 轮不够才换词，换词也是代码在控环——最多 MAX_SEARCH_ROUNDS 轮。"""
    maker, ws, ids = db_env
    net.plan.update({"数据线": [THERMOS], "typec充电线": [CABLE]})
    _llm(monkeypatch, intent={"keywords": ["数据线"], "quantity": 5},
         refine={"keyword": "typec充电线", "stop": False, "reason": "加规格词"})
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], instruction="找 5 款数据线", workspace=ws
        )
    assert [r["keyword"] for r in out["searched"]["rounds"]] == ["数据线", "typec充电线"]
    assert len(net.search) == MAX_SEARCH_ROUNDS
    assert {c[0] for c in await _pool(maker)} == {THERMOS, CABLE}


async def test_pipeline_refine_says_stop_uses_partial_results(db_env, net, monkeypatch):
    """模型判收手就收，不硬凑第三个词。"""
    maker, ws, ids = db_env
    net.plan["数据线"] = [THERMOS]
    _llm(monkeypatch, intent={"keywords": ["数据线"], "quantity": 5}, refine={"stop": True})
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], instruction="找 5 款数据线", workspace=ws
        )
    assert len(net.search) == 1
    assert out["searched"]["rounds"][-1]["keyword"] is None
    assert out["collected"]["added"] == 1


async def test_pipeline_reports_search_blocked(db_env, net, monkeypatch):
    """检索被风控：一件也没有 → 不建批次、不入池，把原因原样报出去。"""
    maker, ws, ids = db_env
    net.plan["数据线"] = sb.SearchBlocked("risk_shell", "被 1688 风控")
    _llm(monkeypatch, intent={"keywords": ["数据线"]})
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], instruction="找几款数据线", workspace=ws
        )
    assert out["status"] == "search_blocked"
    assert out["searched"]["rounds"][0]["blocked"] == "risk_shell"
    assert await _pool(maker) == []
    assert await _batches(ws) == []


async def test_pipeline_truncates_to_plan_quantity(db_env, net, monkeypatch):
    """模型说 1 件就只采 1 件：搜回 2 个也裁到 quantity。"""
    maker, ws, ids = db_env
    net.plan["保温杯"] = [THERMOS, CABLE]
    _llm(monkeypatch, intent={"keywords": ["保温杯"], "quantity": 1})
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], instruction="找 1 款保温杯", workspace=ws
        )
    assert net.search[0]["limit"] == 1
    assert out["collected"]["added"] == 1
    assert len(await _pool(maker)) == 1


async def test_pipeline_missing_slot_asks_instead_of_guessing(db_env, net, monkeypatch):
    """缺关键槽位 → needs_clarification，且完全不碰 1688。"""
    maker, ws, ids = db_env
    _llm(monkeypatch, intent={"keywords": [], "quantity": 3})
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], instruction="帮我上点货", workspace=ws
        )
    assert out["status"] == "needs_clarification"
    assert any("关键词" in x for x in out["needs_clarification"])
    assert net.search == []
    assert await _pool(maker) == []


async def test_pipeline_isolates_per_item_failures(db_env, net, monkeypatch):
    """单件撞风控、单件出价越界都不该拖垮整批（N6）：逐件记账，护栏照旧生效。"""
    maker, ws, ids = db_env
    _llm(monkeypatch, intent={"keywords": ["保温杯"]})
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"],
            offer_ids=[THERMOS, RISK_ID, CABLE], workspace=ws,
        )
    by_id = {d["offer_id"]: d for d in out["decided"]}
    assert by_id[THERMOS]["price_cent"] == 1990 and by_id[THERMOS]["pricing"] == "llm"
    assert "未采到商品数据" in by_id[RISK_ID]["skipped"]
    assert "price_cent" not in by_id[RISK_ID]
    # 成本 2.5 元、模型还价 19.9 = 7.96 倍，越护栏 → 规则兜底，绝不照着模型价入库
    assert by_id[CABLE]["pricing"] == "fallback-rule"
    assert {d["offer_id"] for d in out["reviewable"]} == {THERMOS, CABLE}
    assert out["collected"]["blocked"] == 1
    assert out["status"] == "ok"
    assert [p[1] for p in await _pool(maker)] == ["DECIDED", "DECIDED"]


async def test_pipeline_decide_error_is_recorded_not_swallowed(db_env, net, monkeypatch):
    """定价节点整个坏掉时：采集成果保住、状态仍 ok，但 error 要写清类型供事后追查。"""
    maker, ws, ids = db_env
    import app.collectors.pipeline as pl

    async def boom(*a, **k):
        raise StructuredOutputError("chat_json 重试 2 次仍失败")

    monkeypatch.setattr(pl, "decide_offer", boom)
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], offer_ids=[THERMOS], workspace=ws
        )
    assert out["decided"] == [
        {"offer_id": THERMOS, "error": "StructuredOutputError: chat_json 重试 2 次仍失败"}
    ]
    assert out["reviewable"] == [] and out["needs_human"] == []
    assert await _pool(maker) == [(THERMOS, "SOURCED", None)]


async def test_pipeline_batch_ledger_records_the_human_instruction(db_env, net, monkeypatch):
    """台账 trigger/instruction 要能区分"人话指令"与"手工/定时"，否则事后查不到谁发起。"""
    maker, ws, ids = db_env
    net.plan["保温杯"] = [THERMOS]
    _llm(monkeypatch, intent={"keywords": ["保温杯"], "quantity": 1})
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], instruction="找一款保温杯", workspace=ws
        )
    meta = ws.read_batch(out["batch_id"])
    assert meta["trigger"] == "nl"
    assert meta["instruction"] == "找一款保温杯"
    assert "保温杯" in out["batch_id"]          # slug 保留中文，目录名可反查
    assert json.dumps(meta["offer_ids"]) == json.dumps([THERMOS])


async def test_pipeline_respects_explicit_unusable_source_account(db_env, net, monkeypatch):
    """显式指定的货源账号登录态已失效 → 同一条 no_source_account 路径，不降级去用别人的。"""
    maker, ws, ids = db_env
    async with maker() as s:
        acct = (await s.execute(select(AlibabaAccount))).scalar_one()
        acct.cookie_status = 0
        await s.commit()
    _llm(monkeypatch, intent=AssertionError("不该调模型"))
    async with maker() as db:
        out = await run_collect_pipeline(
            db, account_id=ids["account_admin"], offer_ids=[THERMOS],
            alibaba_account_id=ids["alibaba"], workspace=ws,
        )
    assert out["status"] == "no_source_account"


# ---------------------------------------------------------------- 端点


@pytest_asyncio.fixture
async def api(db_env, monkeypatch):
    """(maker, client, client_bob, ids)：走真实路由 + 依赖覆盖，工作区指向 tmp。"""
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

    client = await _login("root")
    client_bob = await _login("bob")
    yield maker, client, client_bob, ws, ids
    await client.aclose()
    await client_bob.aclose()


async def test_run_endpoint_requires_a_source(api):
    _, client, _, _, ids = api
    resp = await client.post("/api/collect/run", json={"account_id": ids["account_admin"]})
    assert resp.status_code == 400
    assert "offer_ids" in resp.json()["detail"]
    assert (
        await client.post("/api/collect/run", json={"account_id": 999, "keyword": "数据线"})
    ).status_code == 404


async def test_run_endpoint_enforces_account_ownership(api):
    """bob 不能拿 admin 的发布账号发起采集。"""
    _, _, client_bob, _, ids = api
    resp = await client_bob.post(
        "/api/collect/run", json={"account_id": ids["account_admin"], "keyword": "数据线"}
    )
    assert resp.status_code == 403


async def test_run_endpoint_denies_foreign_alibaba_account(api):
    _, _, client_bob, _, ids = api
    resp = await client_bob.post(
        "/api/collect/run",
        json={"account_id": ids["account_bob"], "keyword": "数据线",
              "alibaba_account_id": ids["alibaba"]},
    )
    assert resp.status_code == 403
    assert (
        await client_bob.post(
            "/api/collect/run",
            json={"account_id": ids["account_bob"], "keyword": "数据线",
                  "alibaba_account_id": 777},
        )
    ).status_code == 404


async def test_run_endpoint_reports_business_status_as_200(api, net, monkeypatch):
    """needs_clarification 是"跑出了个正常结论"，不是 HTTP 错误，前端按状态渲染追问。"""
    _, client, _, _, ids = api
    _llm(monkeypatch, intent={"keywords": []})
    resp = await client.post(
        "/api/collect/run", json={"account_id": ids["account_admin"], "instruction": "帮我上点货"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "needs_clarification"
    assert net.search == []


async def test_run_endpoint_happy_path_lists_pending(api, net, monkeypatch):
    """端点跑通后，待确认清单里就该看到那件 DECIDED 商品——不点放行就不会再往前走。"""
    maker, client, _, _, ids = api
    net.plan["保温杯"] = [THERMOS]
    _llm(monkeypatch, intent={"keywords": ["保温杯"], "quantity": 1, "preference": "要包邮"})
    resp = await client.post(
        "/api/collect/run",
        json={"account_id": ids["account_admin"], "instruction": "找一款保温杯，卖 19.9，要包邮",
              "max_items": 5},
    )
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "ok", body
    assert body["intent_source"] == "llm"
    assert body["plan"]["preference"] == "要包邮"
    assert body["alibaba_account_id"] == ids["alibaba"]

    pend = (await client.get("/api/collect/pending")).json()
    assert [p["offer_id"] for p in pend] == [THERMOS]
    assert pend[0]["pool_status"] == "DECIDED" and pend[0]["pricing"] == "llm"
    async with maker() as s:
        g = (await s.execute(select(Goods))).scalars().one()
        assert g.pool_status == "DECIDED" and g.external_goods_id is None and g.status == 0
