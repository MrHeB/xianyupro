"""M2 定价/文案测试（§7.1 三级降级链 + decision 写盘契约）。

假 provider 按 system 首行路由「定价 / 文案」两条脚本队列，覆盖：
LLM 正常出价 / 越界回落规则 / LLM 不可用回落 / 无成本价 manual /
文案失败交白卷（不回落上游标题） / confidence 钳制 / decision 只写一次（force）。
"""

import json

import pytest
from pydantic import ValidationError

from app.ai.decision import (
    CopyOut,
    DecisionExists,
    price_and_copy,
    price_product,
)
from app.ai.provider import LlmResult
from app.collectors.alibaba.workspace import CollectWorkspace

BATCH = "2026-09-19T10-00_test"
OFFER = "728001"

PRICE_1990 = json.dumps(
    {
        "pricing": "llm",
        "price_cent": 1990,
        "orig_price_cent": 2990,
        "delivery": "包邮",
        "confidence": "high",  # 故意给 high，验证钳制到 medium
        "reasoning": "成本3.5元，数据线高同质化按约5.7倍取19.9元尾数",
        "markup_ratio_actual": 1.99,
    },
    ensure_ascii=False,
)
COPY_OK = json.dumps(
    {
        "title_candidates": [
            "1米 Type-C 快充线 全新现货",
            "type-c数据线 1米 支持快充",
            "全新1米type-c快充数据线",
        ],
        "desc": "成色与规格：全新，长度:1m。\n适合场景：日常充电。\n发货与售后：以所选快递为准。",
    },
    ensure_ascii=False,
)


class ScriptedProvider:
    """按 system 内容路由：含「定价助手」走 pricing 脚本，「文案助手」走 copy 脚本。"""

    model = "test-model"

    def __init__(self, pricing=(), copy=()):
        self.scripts = {"pricing": list(pricing), "copy": list(copy)}
        self.calls = []  # [(kind, messages)]

    async def chat(self, messages, *, temperature=0.7, max_tokens=1024, response_format=None):
        system = messages[0]["content"]
        kind = "pricing" if "定价助手" in system else "copy"
        self.calls.append((kind, messages))
        script = self.scripts[kind]
        if not script:
            raise AssertionError(f"{kind} 脚本已耗尽（不该发起这次调用）")
        step = script.pop(0)
        return step(messages) if callable(step) else step

    def kinds_called(self):
        return [k for k, _ in self.calls]


def _workspace(tmp_path, price_cent: int = 1000, local_images=None) -> CollectWorkspace:
    ws = CollectWorkspace(tmp_path)
    ws.start_batch(BATCH, trigger="manual", offer_ids=[OFFER])
    collect = ws.build_collect_section(
        offer_id=OFFER,
        title_raw="厂家直销type-c数据线【爆款】快充",
        price_cent=price_cent,
        image_urls=["https://cbu01.alicdn.com/x.jpg"],
        raw={"specs": {"长度": "1m"}, "stock": 9, "category": "数码配件", "supplier": "深圳XX"},
    )
    ws.write_collect(BATCH, collect,
                     local_images=["images/1.jpg"] if local_images is None else local_images)
    return ws


# ---------- 定价三级降级链（§7.1） ----------

async def test_llm_price_in_guardrail_wins(tmp_path):
    ws = _workspace(tmp_path)
    prov = ScriptedProvider(pricing=[LlmResult(ok=True, text=PRICE_1990)], copy=[LlmResult(ok=True, text=COPY_OK)])
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)

    assert decision["pricing"] == "llm"
    assert decision["price_cent"] == 1990
    assert decision["orig_price_cent"] == 2990
    assert decision["confidence"] == "medium"  # 无竞品数据，high 被钳制（§7.3 第 9 条）
    assert decision["decided_by"] == "llm:test-model"
    assert decision["copy_source"] == "llm"
    assert decision["title"] == "1米 Type-C 快充线 全新现货"
    assert decision["evidence"][0]["price_cent"] == 1000
    assert decision["risk_scan"]["passed"] is True  # 干净文案不该误报
    assert decision["risk_rewrite_rounds"] == 0


async def test_out_of_range_price_falls_to_rule(tmp_path):
    ws = _workspace(tmp_path)
    bad = json.dumps({"pricing": "llm", "price_cent": 5000, "reasoning": "这个价肯定没问题放心"})
    prov = ScriptedProvider(pricing=[LlmResult(ok=True, text=bad)], copy=[LlmResult(ok=True, text=COPY_OK)])
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)

    # 1000 分成本上限 3.0×=3000，5000 越界 → [2] 规则兜底 10×1.3=13 → 1290
    assert decision["pricing"] == "fallback-rule"
    assert decision["price_cent"] == 1290
    assert decision["decided_by"].startswith("rule:markup")
    assert "越界" in decision["reasoning"]


async def test_llm_unavailable_falls_to_rule(tmp_path):
    ws = _workspace(tmp_path)
    prov = ScriptedProvider(
        pricing=[LlmResult(ok=True, text="garbage"), LlmResult(ok=True, text="still garbage")],
        copy=[LlmResult(ok=True, text=COPY_OK)],
    )
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)
    assert decision["pricing"] == "fallback-rule"
    assert decision["price_cent"] == 1290


async def test_no_cost_goes_manual_without_pricing_call(tmp_path):
    ws = _workspace(tmp_path, price_cent=0)
    prov = ScriptedProvider(copy=[LlmResult(ok=True, text=COPY_OK)])  # pricing 脚本为空
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)

    assert "pricing" not in prov.kinds_called()  # [3] 级根本不问模型
    assert decision["pricing"] == "manual"
    assert decision["price_cent"] is None
    assert decision["decided_by"] == "pending:manual"


async def test_model_self_judged_manual_kept(tmp_path):
    ws = _workspace(tmp_path)
    m = json.dumps({"pricing": "manual", "reasoning": "目标价10元低于成本×1.15下限，冲突"})
    prov = ScriptedProvider(pricing=[LlmResult(ok=True, text=m)], copy=[LlmResult(ok=True, text=COPY_OK)])
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)
    assert decision["pricing"] == "manual"
    assert decision["price_cent"] is None
    assert "冲突" in decision["reasoning"]


async def test_fake_orig_price_nulled(tmp_path):
    ws = _workspace(tmp_path)
    bad = json.dumps({"pricing": "llm", "price_cent": 1990, "orig_price_cent": 1500,
                      "reasoning": "数据线按两倍左右卖比较稳妥"})  # 划线价 ≤ 售价 = 编造
    prov = ScriptedProvider(pricing=[LlmResult(ok=True, text=bad)], copy=[LlmResult(ok=True, text=COPY_OK)])
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)
    assert decision["pricing"] == "llm"
    assert decision["orig_price_cent"] is None


# ---------- 文案（刻意无兜底）----------

async def test_copy_failure_leaves_blank_copy_never_upstream_title(tmp_path):
    """§7.4 文案没有降级链：模型写不出来就交白卷，**不许拿 1688 源标题去噪版冒充成品文案**。

    旧行为（2026-09-20 改掉）是回落 `clean_title(title_raw)`：那文本必撞 `title_too_long`，
    而该项在 `NOT_COPY_FIXABLE` 里 → 改写循环不救，件件卡在放行闸门前，面板还显示得
    像文案已经写好了。白卷则直接被 `title_missing` 拦下，原因看得见、也修得动（重跑）。
    """
    ws = _workspace(tmp_path)
    prov = ScriptedProvider(
        pricing=[LlmResult(ok=True, text=PRICE_1990)],
        copy=[LlmResult(ok=True, text="写不出"), LlmResult(ok=True, text="还是写不出")],
    )
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)

    assert decision["pricing"] == "llm"  # 定价与文案互不拖累
    assert decision["copy_source"] == "none"
    assert decision["title"] == ""
    assert decision["desc"] == ""
    assert decision["copy_error"]  # 失败原因留档，供面板解释
    codes = {h["code"] for h in decision["risk_scan"]["blockers"]}
    assert {"title_missing", "desc_missing"} <= codes
    assert decision["risk_scan"]["passed"] is False
    assert decision["risk_rewrite_rounds"] == 0  # 白卷没有违规词可回喂，不烧第二次 LLM


async def test_copy_title_over_30_rejected_by_schema():
    with pytest.raises(ValidationError):
        CopyOut(title_candidates=["字" * 31, "b", "c"], desc="够长的描述文本")


# ---------- decision 写盘契约（§3.2 / §3.3） ----------

async def test_decision_written_and_stage_decided(tmp_path):
    ws = _workspace(tmp_path)
    prov = ScriptedProvider(pricing=[LlmResult(ok=True, text=PRICE_1990)], copy=[LlmResult(ok=True, text=COPY_OK)])
    await price_and_copy(ws, BATCH, OFFER, provider=prov)

    product = ws.read_product(BATCH, OFFER)
    assert product["status"]["stage"] == "decided"
    assert [h["stage"] for h in product["status"]["history"]] == ["sourced", "decided"]
    assert product["decision"]["status"] == "proposed"
    assert product["decision"]["confirmed_by"] is None
    # collect 段原文未被文案/定价污染（§3.3-1）
    assert product["collect"]["title_raw"] == "厂家直销type-c数据线【爆款】快充"
    assert product["media"]["local_images"] == ["images/1.jpg"]


async def test_decision_exists_requires_force(tmp_path):
    ws = _workspace(tmp_path)
    prov = ScriptedProvider(
        pricing=[LlmResult(ok=True, text=PRICE_1990)] * 2,
        copy=[LlmResult(ok=True, text=COPY_OK)] * 2,
    )
    await price_and_copy(ws, BATCH, OFFER, provider=prov)
    with pytest.raises(DecisionExists):
        await price_and_copy(ws, BATCH, OFFER, provider=prov)
    # force 覆盖走通（改写后要重新走确认：status 回到 proposed、confirmed 清空）
    d = await price_and_copy(ws, BATCH, OFFER, provider=prov, force=True)
    assert d["confirmed_by"] is None


async def test_price_product_prefers_user_preference_flow(tmp_path):
    """preference 透传到定价 user prompt（§7.3 第 8 条冲突判 manual 的输入面）。"""
    ws = _workspace(tmp_path)
    collect = ws.read_product(BATCH, OFFER)["collect"]
    from app.ai.decision import _pricing_user_prompt

    assert "用户偏好：无" in _pricing_user_prompt(collect, None)
    assert "用户偏好：不要超过15元" in _pricing_user_prompt(collect, "不要超过15元")
    # 且真的到了模型
    prov = ScriptedProvider(pricing=[lambda msgs: LlmResult(ok=True, text=PRICE_1990) if "不要超过15元" in msgs[1]["content"] else LlmResult(ok=False, error="x")])
    out = await price_product(collect, preference="不要超过15元", provider=prov)
    assert out["price_cent"] == 1990


# ---------- 风险扫描接线（§8.4 节点 4：命中违规回第 3 步改写，≤2 轮） ----------

BAD_COPY = json.dumps(
    {
        "title_candidates": ["Type-C 快充线 全网最低价", "1米数据线 第一选择", "全新快充线"],
        "desc": "全新 1 米 type-c 快充线，支持快充。具体细节加微信 13800001111 详聊，包真。",
    },
    ensure_ascii=False,
)


async def test_risk_blocker_triggers_one_rewrite(tmp_path):
    ws = _workspace(tmp_path)
    prov = ScriptedProvider(
        pricing=[LlmResult(ok=True, text=PRICE_1990)],
        copy=[LlmResult(ok=True, text=BAD_COPY), LlmResult(ok=True, text=COPY_OK)],
    )
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)

    assert decision["risk_rewrite_rounds"] == 1
    assert decision["risk_scan"]["passed"] is True
    assert decision["title"] == "1米 Type-C 快充线 全新现货"
    assert prov.kinds_called() == ["pricing", "copy", "copy"]  # 定价不重跑，只重写文案
    # 违规词确实回喂给了模型（不是静默丢掉）
    retry_prompt = prov.calls[-1][1][1]["content"]
    assert "上一版命中违规词" in retry_prompt
    assert "13800001111" in retry_prompt and "全网最低" in retry_prompt


async def test_failed_rewrite_round_keeps_previous_draft(tmp_path):
    """改写那一轮模型挂了：留初版（模型写的、只是还带违规词），不用白卷盖掉好文案。"""
    ws = _workspace(tmp_path)
    prov = ScriptedProvider(
        pricing=[LlmResult(ok=True, text=PRICE_1990)],
        copy=[LlmResult(ok=True, text=BAD_COPY)]
        + [LlmResult(ok=True, text="模型挂了")] * 2,  # 改写那一轮含 chat_json 的 1 次修正重试
    )
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)

    assert decision["copy_source"] == "llm"
    assert decision["title"] == "Type-C 快充线 全网最低价"  # 初版原样留着
    assert decision["risk_rewrite_rounds"] == 1
    assert decision["copy_error"]  # 但这一轮失败留了痕
    assert decision["risk_scan"]["passed"] is False  # 违规词还在，闸门照拦


async def test_risk_rewrite_rounds_capped_at_two(tmp_path):
    ws = _workspace(tmp_path)
    prov = ScriptedProvider(
        pricing=[LlmResult(ok=True, text=PRICE_1990)],
        copy=[LlmResult(ok=True, text=BAD_COPY)] * 3,  # 1 次初稿 + 2 次改写后不再有脚本
    )
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)

    assert decision["risk_rewrite_rounds"] == 2  # §8.4 上限，不无限重试
    assert decision["risk_scan"]["passed"] is False
    codes = {h["code"] for h in decision["risk_scan"]["blockers"]}
    assert {"absolutism", "off_platform_contact", "contact_number"} <= codes


async def test_unfixable_blocker_does_not_rewrite(tmp_path):
    """图片超 9 张属结构问题，重跑文案救不了 → 不该浪费 LLM 调用（手册 §1.7 规则优先）。"""
    ws = _workspace(tmp_path, local_images=[f"images/{i}.jpg" for i in range(1, 11)])
    prov = ScriptedProvider(
        pricing=[LlmResult(ok=True, text=PRICE_1990)],
        copy=[LlmResult(ok=True, text=COPY_OK)],  # 只够一次，多调即 AssertionError
    )
    decision = await price_and_copy(ws, BATCH, OFFER, provider=prov)

    assert decision["risk_rewrite_rounds"] == 0
    assert decision["risk_scan"]["passed"] is False
    assert decision["risk_scan"]["image_count_ok"] is False
