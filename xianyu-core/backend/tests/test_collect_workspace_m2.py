"""采集工作区接口 + 人工放行闸门测试（§3.1 可视化 / §8.1 半自动闸门）。

覆盖：批次与单件只读、图片原件与路径校验、decide 升 DECIDED、
待确认清单筛选、放行的四道闸门（confirm / 上限 / manual 定价 / 风险未过）。
"""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai import decision as dec_mod
from app.api.deps import get_db
from app.api.routes import api_router
from app.collectors.alibaba.workspace import CollectWorkspace
from app.core.db import get_db
from app.models import Account, Base, Goods, User
from app.users.security import hash_password

BATCH = "2026-09-19T10-00_test"
OFFER = "728001"

PRICED = SimpleNamespace(
    pricing="llm", price_cent=1990, orig_price_cent=2990, delivery="包邮",
    confidence="medium", reasoning="成本10元，同质化高按约2倍取19.9元",
    markup_ratio_actual=1.99,
)
COPIED = SimpleNamespace(
    title_candidates=["1米 Type-C 快充数据线", "type-c数据线 1米 支持快充", "全新1米快充线"],
    desc="成色与规格：全新，长度:1m。\n适合场景：日常充电。\n发货与售后：以所选快递为准。",
)


@pytest_asyncio.fixture
async def ws(tmp_path):
    """工作区：一个批次、一件商品、一张本地图；decision 段留空（未跑定价）。"""
    w = CollectWorkspace(tmp_path)
    w.start_batch(BATCH, trigger="manual", instruction="帮我采一条数据线", offer_ids=[OFFER])
    collect = w.build_collect_section(
        offer_id=OFFER,
        title_raw="厂家直销type-c数据线【爆款】快充",
        price_cent=1000,
        image_urls=["https://cbu01.alicdn.com/x.jpg"],
        raw={"specs": {"长度": "1m"}, "supplier": "某厂"},
    )
    w.write_collect(BATCH, collect, local_images=["images/1.jpg"])
    w.close_batch(BATCH, added=1, skipped=0, failed=0)
    (w.offer_dir(BATCH, OFFER) / "images").mkdir(exist_ok=True)
    (w.offer_dir(BATCH, OFFER) / "images" / "1.jpg").write_bytes(b"\xff\xd8\xff")
    return w


@pytest_asyncio.fixture
async def ctx(ws, monkeypatch):
    """(session_maker, client)：seed admin + 账号 + 1 件 SOURCED 采集商品。"""
    monkeypatch.setattr("app.api.routes.collect.workspace", ws)
    monkeypatch.setattr("app.collectors.pool_flow.CollectWorkspace", lambda *a, **k: ws)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        u = User(username="root", password_hash=hash_password("passw0rd123"), role="admin")
        s.add(u)
        await s.flush()
        a = Account(user_id=u.id, external_uid="unb-1", nickname="shop", status=1)
        s.add(a)
        await s.flush()
        s.add(
            Goods(
                account_id=a.id, category="B", source="1688", source_offer_id=OFFER,
                title="type-c数据线快充", price_cent=1290, source_price_cent=1000,
                status=0, pool_status="SOURCED", local_path=str(ws.offer_dir(BATCH, OFFER)),
            )
        )
        await s.commit()

    async def _get_db():
        async with maker() as s:
            yield s

    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = _get_db
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    token = (
        await client.post("/api/auth/login", json={"username": "root", "password": "passw0rd123"})
    ).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    yield maker, client
    await client.aclose()
    await engine.dispose()


async def _fake_chat_json(system, user, model_cls, **kwargs):
    return PRICED if "定价" in system else COPIED


# ---------------- 只读：批次 / 单件 / 图片 ----------------


async def test_batches_endpoint_merges_ledger_and_meta(ctx):
    _, client = ctx
    rows = (await client.get("/api/collect/batches")).json()
    assert rows[0]["batch_id"] == BATCH
    assert rows[0]["trigger"] == "manual"
    assert rows[0]["instruction"] == "帮我采一条数据线"
    assert rows[0]["count"] == {"added": 1, "skipped": 0, "failed": 0, "blocked": 0}
    assert rows[0]["offer_count"] == 1


async def test_batch_detail_marks_no_decision_yet(ctx):
    _, client = ctx
    body = (await client.get(f"/api/collect/batches/{BATCH}")).json()
    item = body["items"][0]
    assert item["offer_id"] == OFFER
    assert item["stage"] == "sourced"
    assert item["has_decision"] is False
    assert item["blocked_reason"] is None
    assert item["image_count"] == 1
    assert item["image_count_ok"] is True


async def test_batch_detail_reports_blocked_offer_without_goods(ctx, ws):
    """撞风控的件没有 Goods 行，但仍要在批次详情里报出 stage + 原因，不能凭空少一件。"""
    _, client = ctx
    ws.write_blocked(BATCH, "728002", "<html>x5secdata=</html>", reason="risk_shell")
    body = (await client.get(f"/api/collect/batches/{BATCH}")).json()
    blocked = next(i for i in body["items"] if i["offer_id"] == "728002")
    assert blocked["goods_id"] is None
    assert blocked["stage"] == "blocked"
    assert blocked["blocked_reason"] == "risk_shell"
    assert blocked["title_raw"] == ""
    assert blocked["image_count"] == 0


async def test_batch_detail_404_for_unknown(ctx):
    _, client = ctx
    assert (await client.get("/api/collect/batches/nope_offers")).status_code == 404


async def test_offer_detail_returns_four_sections(ctx):
    _, client = ctx
    body = (await client.get(f"/api/collect/offers/{BATCH}/{OFFER}")).json()
    assert set(body["product"]) >= {"schema", "collect", "decision", "media", "status"}
    assert body["product"]["collect"]["source_price_cent"] == 1000
    assert body["goods"]["pool_status"] == "SOURCED"


async def test_offer_image_served_and_bad_name_rejected(ctx, client=None):
    _, client = ctx
    resp = await client.get(f"/api/collect/offers/{BATCH}/{OFFER}/images/1.jpg")
    assert resp.status_code == 200 and resp.content == b"\xff\xd8\xff"
    assert (await client.get(f"/api/collect/offers/{BATCH}/{OFFER}/images/x.png")).status_code == 400
    assert (await client.get(f"/api/collect/offers/{BATCH}/{OFFER}/images/9.jpg")).status_code == 404


def test_batch_id_charset_validated():
    from fastapi import HTTPException

    from app.api.routes.collect import _check_id

    # make_batch_id 会保留中文关键词 slug（§3.1），这类目录名必须放行
    for ok in ("2026-09-19T09-30_数据线", "2026-09-19T11-05_manual", "684521345678"):
        assert _check_id(ok, "batch_id") == ok
    for bad in ("..", "../../etc", "a/b", r"a\b", "x" * 90, "", "带 空格", "a;b"):
        with pytest.raises(HTTPException):
            _check_id(bad, "batch_id")


# ---------------- decide：SOURCED → DECIDED ----------------


async def test_decide_upgrades_to_decided_and_lists_pending(ctx, monkeypatch):
    maker, client = ctx
    monkeypatch.setattr(dec_mod, "chat_json", _fake_chat_json)
    resp = await client.post(f"/api/collect/offers/{BATCH}/{OFFER}/decide", json={})
    assert resp.status_code == 200, resp.text
    d = resp.json()["decision"]
    assert d["pricing"] == "llm" and d["price_cent"] == 1990
    assert d["risk_scan"]["passed"] is True

    again = await client.post(f"/api/collect/offers/{BATCH}/{OFFER}/decide", json={})
    assert again.status_code == 409  # decision 只写一次（§3.3-2）

    pend = (await client.get("/api/collect/pending")).json()
    assert [p["goods_id"] for p in pend] == [1]
    assert pend[0]["pricing"] == "llm" and pend[0]["proposed_price_cent"] == 1990
    assert pend[0]["pool_status"] == "DECIDED"

    async with maker() as s:
        g = await s.get(Goods, 1)
        assert g.pool_status == "DECIDED"


async def test_decide_uses_runtime_llm_config(ctx, monkeypatch):
    """回归（D-2026-09-19-12）：decide 的 provider 必须来自运行时配置（DB 覆盖 .env）。

    原先走 `ai/structured.py` 的 import 期 env 单例，前端设置页里配的 key 对它无效，
    采集链在任何部署下都恒定退化成 fallback-rule。
    """
    from app.settings import LlmConfig

    seen = {}

    async def _chat(system, user, model_cls, *, provider=None, **kwargs):
        seen["provider"] = provider
        return SimpleNamespace(**vars(PRICED)) if "定价" in system else COPIED

    async def _cfg(db):
        return LlmConfig(api_key="sk-page", base_url="https://llm.example/v1", model="m-42")

    monkeypatch.setattr(dec_mod, "chat_json", _chat)
    monkeypatch.setattr("app.settings.get_llm_config", _cfg)
    _, client = ctx

    assert (await client.post(f"/api/collect/offers/{BATCH}/{OFFER}/decide", json={})).status_code == 200
    prov = seen["provider"]
    assert (prov.api_key, prov.base_url, prov.model) == ("sk-page", "https://llm.example/v1", "m-42")


async def test_pending_filter_sourced_and_rejects_other(ctx):
    _, client = ctx
    rows = (await client.get("/api/collect/pending", params={"pool_status": "SOURCED"})).json()
    assert rows[0]["has_decision"] is False
    assert (await client.get("/api/collect/pending", params={"pool_status": "MAPPED"})).status_code == 400


# ---------------- 放行闸门（§8.3） ----------------


async def _decide(ctx, monkeypatch, *, price_cent=1990):
    maker, client = ctx

    async def _chat(system, user, model_cls, **kwargs):
        return (
            SimpleNamespace(**{**vars(PRICED), "price_cent": price_cent})
            if "定价" in system else COPIED
        )

    monkeypatch.setattr(dec_mod, "chat_json", _chat)
    assert (await client.post(f"/api/collect/offers/{BATCH}/{OFFER}/decide", json={})).status_code == 200
    return maker, client


def _write_decision(decision: dict) -> None:
    """把给定 decision 直接写进工作区（绕开 LLM，专测放行闸门）。"""
    import app.api.routes.collect as collect_mod

    ws = collect_mod.workspace
    path = ws.offer_dir(BATCH, OFFER) / "product.json"
    product = json.loads(path.read_text(encoding="utf-8"))
    product["decision"] = decision
    path.write_text(json.dumps(product, ensure_ascii=False), encoding="utf-8")


async def _force_status(maker, status: str):
    from sqlalchemy import select

    async with maker() as s:
        g = (await s.execute(select(Goods))).scalar_one()
        g.pool_status = status
        await s.commit()


async def test_approve_requires_explicit_confirm(ctx):
    _, client = ctx
    resp = await client.post("/api/goods/batch-approve", json={"goods_ids": [1], "confirm": False})
    assert resp.status_code == 400
    assert "确认" in resp.json()["detail"]


async def test_approve_batch_capped_at_five(ctx):
    _, client = ctx
    resp = await client.post(
        "/api/goods/batch-approve", json={"goods_ids": [1, 2, 3, 4, 5, 6], "confirm": True}
    )
    assert resp.status_code == 422  # pydantic max_length 先拦住


async def test_approve_refuses_item_without_decision(ctx):
    """DECIDED 但工作区无 decision（异常态）→ 拒绝，不放水。"""
    maker, client = ctx
    await _force_status(maker, "DECIDED")
    resp = await client.post("/api/goods/batch-approve", json={"goods_ids": [1], "confirm": True})
    assert resp.status_code == 400
    assert "未跑定价" in resp.json()["detail"]


async def test_approve_refuses_manual_pricing(ctx):
    """§8.1：pricing=manual 的行禁止留空放行。"""
    maker, client = ctx
    _write_decision({
        "pricing": "manual", "price_cent": None, "title": "全新数据线",
        "desc": "成色与规格：全新。\n适合场景：充电。\n发货与售后：以所选快递为准。",
        "risk_scan": {"passed": True, "blockers": []},
    })
    await _force_status(maker, "DECIDED")
    resp = await client.post("/api/goods/batch-approve", json={"goods_ids": [1], "confirm": True})
    assert resp.status_code == 400
    assert "manual" in resp.json()["detail"]


async def test_approve_refuses_failed_risk_scan(ctx):
    """§8.4 节点 5：风险扫描未过 → 拒绝，并把命中项原样报出来。"""
    maker, client = ctx
    _write_decision({
        "pricing": "llm", "price_cent": 1990, "title": "全网最低价数据线",
        "desc": "成色与规格：全新。\n适合场景：充电。\n发货与售后：以所选快递为准。",
        "risk_scan": {"passed": False, "blockers": [
            {"code": "ABSOLUTISM", "field": "title", "term": "全网最低"}]},
    })
    await _force_status(maker, "DECIDED")
    resp = await client.post("/api/goods/batch-approve", json={"goods_ids": [1], "confirm": True})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "风险扫描未通过" in detail and "ABSOLUTISM@title:全网最低" in detail


async def test_publish_refuses_unapproved_collect_item(ctx):
    """§8.1 闸门：1688 件未放行（SOURCED/DECIDED）不能从「行内上架」绕过发布。"""
    _, client = ctx
    resp = await client.post("/api/goods/1/publish")
    assert resp.status_code == 400
    assert "尚未放行" in resp.json()["detail"]


async def _publish_fails_with(ctx, monkeypatch, kind, message):
    """把 MAPPED 件送去上架，发布链路按 kind 抛错；回 (status_code, detail, pool_status)。"""
    from app.api_client.errors import XianyuApiError

    maker, client = ctx
    await _force_status(maker, "MAPPED")
    # 路由在函数内 `from app.auth.session_pool import session_pool`，按对象打补丁即可
    from app.auth.session_pool import session_pool

    monkeypatch.setattr(session_pool, "get", lambda _aid: SimpleNamespace(is_alive=True))

    async def boom(_db, _goods, _session):
        raise XianyuApiError(kind, message)

    monkeypatch.setattr("app.listing.publisher.publish_goods", boom)
    resp = await client.post("/api/goods/1/publish")
    async with maker() as s:
        status = (await s.get(Goods, 1)).pool_status
    return resp.status_code, resp.json()["detail"], status


async def test_publish_network_failure_keeps_item_mapped(ctx, monkeypatch):
    """网络类失败（请求没送达闲鱼）→ 502 可读 detail，但**不置 FAILED**：
    置了 FAILED 就掉出 auto_listing 的扫描范围，一次本地断网变成要人工重点的活。"""
    from app.api_client.errors import ErrorKind

    code, detail, status = await _publish_fails_with(
        ctx,
        monkeypatch,
        ErrorKind.NETWORK,
        "mtop 网络异常（ConnectError，请求未送达闲鱼）",
    )
    assert code == 502
    assert "ConnectError" in detail
    assert status == "MAPPED"


async def test_publish_platform_failure_marks_failed(ctx, monkeypatch):
    """对照组：平台真的回了错误（非网络）→ 502 且 pool_status=FAILED。"""
    from app.api_client.errors import ErrorKind

    code, detail, status = await _publish_fails_with(
        ctx, monkeypatch, ErrorKind.BIZ_ERROR, "FAIL_BIZ_SKU_PRICE_ILLEGAL::价格非法"
    )
    assert code == 502
    assert status == "FAILED"


async def test_publish_local_throttle_returns_429_and_keeps_item_mapped(ctx, monkeypatch):
    """自己令牌桶没排到号 → 429（稍后重发），不改 pool_status。"""
    from app.api_client.errors import ErrorKind

    code, detail, status = await _publish_fails_with(
        ctx, monkeypatch, ErrorKind.LOCAL_THROTTLE, "写操作限流：30s 后重试"
    )
    assert code == 429
    assert "30s" in detail
    assert status == "MAPPED"


async def test_approve_flips_to_mapped_when_clean(ctx, monkeypatch):
    maker, client = ctx
    await _decide(ctx, monkeypatch)
    resp = await client.post("/api/goods/batch-approve", json={"goods_ids": [1], "confirm": True})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"approved": [1], "applied": 1}
    async with maker() as s:
        g = await s.get(Goods, 1)
        assert g.pool_status == "MAPPED"
        assert g.price_cent == 1990
        assert g.title == "1米 Type-C 快充数据线"  # decision 标题落库
        assert g.raw_payload["confirmed_by"] == "root"
    # 放行后不再出现在待确认清单
    assert (await client.get("/api/collect/pending")).json() == []


# ---------------- 人工改写通道（§9 缺口⑨）----------------


async def test_edit_rescans_and_is_the_only_way_out_of_a_blocked_title(ctx, monkeypatch):
    """没 LLM 时"标题超长"这类拦截项走不完 ≤2 轮改写 → 人工改短重扫后必须能放行。"""
    maker, client = ctx
    await _decide(ctx, monkeypatch)

    long_title = "内外304不锈钢保温杯 异形可乐瓶户外运动水杯车载不锈钢小口瓶"
    resp = await client.patch("/api/goods/1", json={"title": long_title})
    assert resp.status_code == 200, resp.text
    scan = resp.json()["risk_scan"]
    assert scan["passed"] is False
    assert [b["code"] for b in scan["blockers"]] == ["title_too_long"]
    assert (await client.post(
        "/api/goods/batch-approve", json={"goods_ids": [1], "confirm": True}
    )).status_code == 400

    fixed = await client.patch("/api/goods/1", json={"title": "304 不锈钢小口保温杯 550ml"})
    assert fixed.json()["risk_scan"]["passed"] is True
    assert (await client.post(
        "/api/goods/batch-approve", json={"goods_ids": [1], "confirm": True}
    )).status_code == 200
    async with maker() as s:
        g = await s.get(Goods, 1)
        assert g.title == "304 不锈钢小口保温杯 550ml"  # 改的是 decision，放行抄的就是它


async def test_edit_fills_price_and_switches_manual_to_human(ctx):
    """`pricing=manual` 说"必须人工先填价"——这条就是那个填价的地方。"""
    import app.api.routes.collect as collect_mod

    _, client = ctx
    _write_decision({
        "pricing": "manual", "price_cent": None, "title": "1米 Type-C 快充数据线",
        "desc": "成色与规格：全新。\n适合场景：充电。\n发货与售后：以所选快递为准。",
        "risk_scan": {"passed": True, "blockers": []},
    })
    assert (await client.post(
        "/api/goods/batch-approve", json={"goods_ids": [1], "confirm": True}
    )).status_code == 400

    # 只改标题不填价：不能炸（decision 里就是没有 price_cent），但仍不放行
    only_title = await client.patch("/api/goods/1", json={"title": "1米 Type-C 快充线 1条装"})
    assert only_title.status_code == 200, only_title.text
    assert only_title.json()["price_cent"] is None
    assert (await client.post(
        "/api/goods/batch-approve", json={"goods_ids": [1], "confirm": True}
    )).status_code == 400

    resp = await client.patch("/api/goods/1", json={"price_cent": 1990})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pricing"] == "human" and resp.json()["price_cent"] == 1990
    assert (await client.post(
        "/api/goods/batch-approve", json={"goods_ids": [1], "confirm": True}
    )).status_code == 200
    product = collect_mod.workspace.read_product(BATCH, OFFER)
    assert product["decision"]["edited_by"] == "root"
    assert product["decision"]["pricing"] == "human"


async def test_edit_refuses_non_collect_rows_and_live_items(ctx):
    maker, client = ctx
    assert (await client.patch("/api/goods/999", json={"title": "x"})).status_code == 400
    assert (await client.patch("/api/goods/1", json={})).status_code == 400  # 一项都没改
    await _force_status(maker, "PUSHED")
    resp = await client.patch("/api/goods/1", json={"price_cent": 2990})
    assert resp.status_code == 400 and "已在售" in resp.json()["detail"]


# ---------------- 自然语言原文进台账（§6 R1 / R7 追溯）----------------


async def test_collect_1688_writes_nl_instruction_to_ledger(ctx, ws, monkeypatch):
    """带 instruction → 批次台账 trigger=nl 并存原文；不带 → 仍记 manual（存量行为不变）。"""
    _, client = ctx
    captured = {}

    async def _fake(
        db, account_id, offer_ids, *,
        batch_id=None, trigger="manual", instruction=None, alibaba_account_id=None,
    ):
        captured.update(trigger=trigger, instruction=instruction, account_id=account_id)
        bid = batch_id or "2026-09-19T11-00_nl"
        ws.start_batch(bid, trigger=trigger, instruction=instruction, offer_ids=list(offer_ids))
        ws.close_batch(bid, added=len(offer_ids), skipped=0, failed=0)
        return {
            "batch_id": bid, "added": len(offer_ids),
            "skipped": 0, "failed": 0, "blocked": 0,
        }

    monkeypatch.setattr("app.collectors.alibaba.import_1688_by_offer_ids", _fake)

    text = "帮我在 1688 找 3 款 Type-C 数据线，成本 5 元内，卖 19.9 左右"
    resp = await client.post(
        "/api/goods/collect-1688",
        json={"account_id": 1, "offer_ids": ["728002"], "instruction": text},
    )
    assert resp.status_code == 200, resp.text
    assert captured["trigger"] == "nl" and captured["instruction"] == text

    rows = (await client.get("/api/collect/batches")).json()
    row = next(r for r in rows if r["batch_id"] == "2026-09-19T11-00_nl")
    assert row["trigger"] == "nl" and row["instruction"] == text

    await client.post("/api/goods/collect-1688", json={"account_id": 1, "offer_ids": ["728003"]})
    assert captured["trigger"] == "manual" and captured["instruction"] is None
