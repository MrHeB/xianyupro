"""自然语言 → 采集计划（需求文档 §4.1）。

只做**读人话**这一件事：产出一个结构化的选品意图，不执行任何动作、不碰 DB 业务表、
不 import 采集器。执行与闸门全在 `collectors/pipeline.py`。

三级降级沿用 `ai/decision.py` 的口径：
    [1] LLM 结构化输出（chat_json，pydantic 硬边界）
    [2] LLM 不可用 → 规则兜底（只捞 URL 里的 offer_id 和"成本 X 元 / 卖 Y 元"这类
        明确字样），标 intent_source="rule"，**不猜关键词**
    [3] 规则也捞不出可执行意图 → needs_clarification 非空，由上层转人工追问

"缺关键槽位必须追问，不猜"这条原本是 Skill 提示词里的人工纪律（D-2026-09-19-14 把它
从提示词搬进代码），落点就是 `needs_clarification`：判定在代码里算，不让模型自评。
"""

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.ai.structured import StructuredOutputError, chat_json

logger = logging.getLogger(__name__)

# 用户话术里说的是元；内部一律分。让模型出元、代码换算，比让它心算乘 100 少一个错源。
_MAX_QUANTITY = 20

_OFFER_URL_RE = re.compile(r"offer/(\d{6,20})\.html")
_COST_RE = re.compile(r"成本[^0-9]{0,6}(\d+(?:\.\d+)?)\s*元")
_SELL_RE = re.compile(r"(?:卖|售价|定价|标价)[^0-9]{0,4}(\d+(?:\.\d+)?)\s*元")
_QTY_RE = re.compile(r"(\d{1,2})\s*(?:款|个|件|种|款商品)")


class CollectPlan(BaseModel):
    """一次自然语言采集请求的意图。账号类参数不在这里（那是执行参数，由端点校验归属）。"""

    keywords: list[str] = Field(default_factory=list, max_length=3)
    quantity: int = Field(default=3, ge=1, le=_MAX_QUANTITY)
    cost_max_cent: int | None = Field(default=None, ge=1, le=10_000_000)
    target_price_cent: int | None = Field(default=None, ge=1, le=10_000_000)
    preference: str | None = Field(default=None, max_length=200)
    offer_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("keywords", mode="after")
    @classmethod
    def _clean_keywords(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for k in v:
            k = (k or "").strip()[:40]
            if k and k not in out:
                out.append(k)
        return out

    @field_validator("offer_ids", mode="after")
    @classmethod
    def _only_digit_ids(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for oid in v:
            oid = str(oid).strip()
            if oid.isdigit() and 6 <= len(oid) <= 20 and oid not in out:
                out.append(oid)
        return out

    @property
    def executable(self) -> bool:
        """有没有足够信息开跑：要么给了现成 ID，要么有词可搜。"""
        return bool(self.offer_ids or self.keywords)


INTENT_SYSTEM = """你是 1688 选品指令解析器。把用户的一句话拆成结构化采集计划，不要执行任何动作。

必须遵守：
1. keywords 是拿去 1688 搜索的**商品词**（名词短语，不含"找/几款/帮我"这类指令词），最多 3 个，
   按相关性降序。用户没表达品类就是空数组，**不要根据常识补一个**。
2. quantity 是想采的商品件数，默认 3，上限 20。用户说"几款""来点"等模糊量词按 3，并在
   preference 里注明是估的。
3. cost_max_cent = 用户能接受的**采购成本上限**，单位**分**（5 元 = 500）。
   target_price_cent = 用户想要的**闲鱼售价**，单位**分**（19.9 元 = 1990）。
   没提到就置 null，不要自己推算市场价。
4. 别把成本价当售价、也别把售价当成本：出现"成本/进货/拿货"归第 3 条，
   出现"卖/售价/定价"归第 4 条。
5. offer_ids：用户直接给出的 1688 商品 ID（纯数字）。给了就走 ID 通道，不必再给 keywords。
6. preference：把用户关于定价与文案的原文诉求原样留下（"要包邮""别写代发"），没有就 null。

只输出 JSON，不要任何解释文字、不要 markdown 围栏。"""

REFINE_SYSTEM = """你在替一次 1688 选品检索换搜索词。上一轮关键词搜到的商品数不够，
你要给出**更可能搜到货**的下一个关键词。

必须遵守：
1. 只能改写法（同义词、上位词、加规格词、去修饰词），**不得偷换品类**：
   用户要找数据线，就不能改成充电宝来凑数。
2. 新词必须是名词性商品词，≤ 12 字，不能是句子、不能带空格堆砌。
3. 与已试过的词不能重复；没有更好的改法就 stop=true，**不要硬凑第三个词**。

只输出 JSON：{"keyword":"…","stop":false,"reason":"…"}"""


def rule_parse(instruction: str) -> CollectPlan:
    """LLM 不可用时的兜底：只认字面上写得明明白白的东西。

    刻意不做关键词提取——中文分词猜品类是幻觉高发区，猜错了会搜回一批不相干的货，
    还看起来像成功。缺词就让它缺，交给 needs_clarification 转人工。
    """
    text = instruction or ""
    cent_cost = _COST_RE.search(text)
    cent_sell = _SELL_RE.search(text)
    qty = _QTY_RE.search(text)
    return CollectPlan(
        keywords=[],
        quantity=int(qty.group(1)) if qty else 3,
        cost_max_cent=_yuan_to_cent(cent_cost.group(1)) if cent_cost else None,
        target_price_cent=_yuan_to_cent(cent_sell.group(1)) if cent_sell else None,
        preference=text.strip()[:200] or None,
        offer_ids=_OFFER_URL_RE.findall(text),
    )


def _yuan_to_cent(raw: str) -> int:
    return int(round(float(raw) * 100))


def _needs_clarification(plan: CollectPlan, max_items: int) -> list[str]:
    """缺槽判定由代码算。只列**会改变执行结果**的缺失项。"""
    missing: list[str] = []
    if not plan.executable:
        missing.append("搜索关键词或直接给出 offer_id")
    if plan.quantity > max_items:
        missing.append(f"数量 {plan.quantity} 超过单次上限 {max_items}，请调小")
    return missing


async def parse_intent(db, instruction: str, *, max_items: int = _MAX_QUANTITY) -> dict:
    """人话 → {plan, needs_clarification, intent_source}。

    `intent_source` 一定是 "llm" 或 "rule"，前端要如实显示——rule 意味着关键词那栏是空的，
    用户得自己补词，不能伪装成"已经理解你了"。
    """
    from app.ai.provider import runtime_provider

    instruction = (instruction or "").strip()
    if not instruction:
        plan = CollectPlan()
        return {
            "plan": plan.model_dump(),
            "needs_clarification": _needs_clarification(plan, max_items),
            "intent_source": "none",
        }

    try:
        provider = await runtime_provider(db)
        plan = await chat_json(
            INTENT_SYSTEM,
            f"用户原话：{instruction[:500]}\n\n单次采集上限 {max_items} 件。",
            CollectPlan,
            provider=provider,
            temperature=0.2,
            max_tokens=512,
        )
        source = "llm"
    except StructuredOutputError as exc:
        logger.warning("意图解析退回规则兜底: %s", str(exc)[:150])
        plan, source = rule_parse(instruction), "rule"

    return {
        "plan": plan.model_dump(),
        "needs_clarification": _needs_clarification(plan, max_items),
        "intent_source": source,
    }


async def refine_keyword(
    db, *, plan: CollectPlan, tried: list[str], got: int, want: int
) -> tuple[str | None, str]:
    """搜到的不够时，让模型换一个词。返回 (下一个关键词 | None, 理由)。

    None = 模型自己判收手，或 LLM 不可用。**不重试**：这是链路里一个可选优化节点，
    拿不到更好的词就用已经拿到的，别为它把整批卡住。
    """
    from app.ai.provider import runtime_provider

    class _Next(BaseModel):
        keyword: str = Field(min_length=2, max_length=12)
        stop: bool = False
        reason: str = ""
        mode: Literal["refine"] = "refine"

    try:
        provider = await runtime_provider(db)
        out = await chat_json(
            REFINE_SYSTEM,
            "已试过：" + "、".join(tried) + "\n"
            f"当前拿到 {got} 个，需要 {want} 个。\n"
            f"原始诉求：{plan.preference or '（无）'}",
            _Next,
            provider=provider,
            temperature=0.4,
            max_tokens=256,
        )
    except StructuredOutputError as exc:
        logger.info("换词节点不可用，用已搜到的结果继续: %s", str(exc)[:120])
        return None, "换词不可用，保留已有结果"

    if out.stop or out.keyword.strip() in tried:
        return None, out.reason or "模型判定不再换词"
    return out.keyword.strip(), out.reason
