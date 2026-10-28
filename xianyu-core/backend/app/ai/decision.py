"""定价与文案（设计文档 §7 / M2）：chat_json 之上的决策生产层。

三级降级链（§7.1，R4 验收核心）：
    [1] 成本价可用 → LLM 定价（护栏：[成本×1.15, 成本×3.0] + 必须有 reasoning）
    [2] 越界 / 解析失败 / 无 reasoning → 规则兜底 markup_price(成本, 1.3)，标待复核
    [3] 成本价拿不到 → price_cent=null，pricing="manual"，必须人工填

文案（§7.4）刻意**没有**降级链：只有模型能产 title/desc。模型不可用时留空并标
`copy_source="none"`，由风险扫描的 `title_missing` 把这件拦在闸门前——不许拿 1688 源标题
去噪版冒充成品文案，那是"看着像已经写好了、实则必撞 title_too_long"的假成功。

铁律（§4 边界 1）：本模块只产 `decision` 段写进 product.json，不碰 Goods、
不产任何发布动作；prompt 里不写风控闸门（闸门在代码层，见 §7.1 兜底判断）。

`price_and_copy` = §8.4 节点 2–4 的实现体（定价 → 文案 → `risk_scan` 扫描 → 命中可改写
违规回文案，≤ `RISK_REWRITE_ROUNDS` 轮）。扫描规则见 `app/ai/risk_scan.py`。
"""

import json
import logging
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.ai.risk_scan import rewrite_terms, run_risk_scan
from app.ai.structured import StructuredOutputError, chat_json
from app.collectors.alibaba.transform import clean_title, markup_price
from app.core.config import settings

logger = logging.getLogger(__name__)

# §7.3 第 3 条：售价硬区间（成本倍率），越界即视为无效出价
COST_MIN_MULT = 1.15
COST_MAX_MULT = 3.0
# §7.2：pydantic 硬边界（分），防模型输出 0.01 元或天价
PRICE_CENT_MIN = 100
PRICE_CENT_MAX = 1_000_000
# §8.4 节点 4 / §6 步骤④：风险命中后回第 3 步改写重扫的上限
RISK_REWRITE_ROUNDS = 2


class DecisionExists(RuntimeError):
    """已有 decision 段（§3.3-2：LLM 只写一次，覆盖需显式 force）。"""


# ---------------- Schema（§7.2：pydantic 即硬边界） ----------------

class PricingOut(BaseModel):
    pricing: Literal["llm", "fallback-rule", "manual"]
    price_cent: int | None = Field(default=None, ge=PRICE_CENT_MIN, le=PRICE_CENT_MAX)
    orig_price_cent: int | None = Field(default=None, ge=PRICE_CENT_MIN, le=5_000_000)
    delivery: Literal["包邮", "按距离计费", "一口价", "无需邮寄"] = "包邮"
    confidence: Literal["low", "medium", "high"] = "low"
    reasoning: str = ""
    markup_ratio_actual: float | None = None

    @model_validator(mode="after")
    def _llm_must_be_full(self):
        # 「无 reasoning 的 llm 出价」按 §7.1 视为无效 → schema 层拒掉，触发修正轮
        if self.pricing == "llm":
            if self.price_cent is None:
                raise ValueError("pricing=llm 必须给出 price_cent")
            if len(self.reasoning.strip()) < 8:
                raise ValueError("pricing=llm 必须给出 reasoning")
        return self


class CopyOut(BaseModel):
    title_candidates: list[str] = Field(min_length=3, max_length=3)
    desc: str = Field(min_length=10)

    @model_validator(mode="after")
    def _title_len(self):
        for t in self.title_candidates:
            if not 0 < len(t) <= 30:  # §7.4：标题 ≤ 30 字
                raise ValueError(f"标题须为 1–30 字：{t!r}")
        return self


# ---------------- 提示词（§7.3 / §7.4，照文档落，勿口语化改写） ----------------

PRICING_SYSTEM = """你是闲鱼二手/无货源店铺的定价助手。你的任务：给定一件从 1688 采集到的商品的\
**采购成本价**，给出一个适合挂在闲鱼的售价、划线原价，以及是否包邮。

必须遵守：
1. 你拿到的价格单位是「分」，输出也必须是「分」的整数。
2. 成本价是进货价，不是市场价。你无法得知市场行情，因此禁止表现出"市场价就是这样"的确定语气。
3. 售价必须落在 [成本×1.15, 成本×3.0] 区间内。超出即视为无效，改判 pricing="manual"。
   理由：下限覆盖运费+包装+售后准备；上限避免价格离谱到无人问津或被判定恶意抬价。
4. 常见倍率参考（不是硬规则，需在 reasoning 里说明为何偏离）：
   - 低值易耗/高同质化（数据线、手机壳、袜子）：2.5–4.0 倍
   - 中值日用（小家电、收纳、厨具）：1.8–2.8 倍
   - 高值/强规格（家具、器械、大件）：1.3–1.8 倍
5. 尾数定价：>10 元用 x.9 / x.90（如 1990 分 = 19.9 元）；≤10 元保留到分。
6. 划线原价（orig_price_cent）必须 > 售价，且 ≤ 成本×5；填不出来就置 null。
   这是"折扣感"字段，禁止编造一个不存在的更高历史价。
7. delivery 只能取："包邮" / "按距离计费" / "一口价" / "无需邮寄"。
   成本价里已含邮费、或商品轻小 → 倾向"包邮"；大件/高运费 → "无需邮寄"。
8. 若用户给了目标价或加价偏好，优先尊重，但仍受第 3 条硬区间约束；
   冲突时不要折中，直接 pricing="manual" 并在 reasoning 说明冲突原因。
9. confidence 最多给 "medium"（你没有市场数据）；只有当输入里出现竞品在售价格样本时才可给 "high"。

只输出 JSON，不要任何解释文字、不要 markdown 围栏。"""

COPY_SYSTEM = """你是闲鱼无货源店铺的文案助手。基于给定的 1688 商品事实，产出 3 版标题候选与 1 版描述。

必须遵守：
1. 只描述输入里真实存在的规格事实（原始标题、类目、规格），禁止编造参数、材质、品牌、认证。
2. 禁用绝对化词：最、第一、唯一、完美、顶级、100%。
3. 禁止站外链接与联系方式（微信/QQ/二维码话术）。
4. 禁止虚构使用经历（"亲测""我用过"）。
5. 禁止货源话术：代发、厂家直销、批发、一件代发、源头工厂。
6. 每版标题 ≤ 30 字且含核心规格；不要营销括号词。
7. 描述分三段：成色与规格 / 适合场景 / 发货与售后；发货只写"以所选快递为准"，不承诺时效。
8. 无依据不得写"99新"等二手成色，按"全新"表述。

只输出 JSON：{"title_candidates":["…","…","…"],"desc":"…"}，不要任何其它文字。"""


def _yuan(cent: int) -> str:
    return f"{cent / 100:.2f}"


def _pricing_user_prompt(collect: dict, preference: str | None) -> str:
    cost = int(collect.get("source_price_cent") or 0)
    lines = [
        f"商品：{collect.get('title_raw') or ''}",
        f"类目（1688 原始）：{collect.get('category_raw') or '未知'}",
        f"规格：{json.dumps(collect.get('specs') or {}, ensure_ascii=False)}",
        f"采购成本价：{cost} 分（{_yuan(cost)} 元）",
        f"卖家：{collect.get('seller') or '未知'}    库存：{collect.get('stock') if collect.get('stock') is not None else '未知'}",
        f"图片数：{len(collect.get('image_urls') or [])}",
        f"用户偏好：{preference or '无'}",
        "竞品样本：无（故 confidence 上限 medium）",
        "",
        '按 schema 输出：{"pricing":"llm|fallback-rule|manual","price_cent":int,'
        '"orig_price_cent":int|null,"delivery":"…","confidence":"low|medium|high",'
        '"reasoning":"…","markup_ratio_actual":float}',
    ]
    return "\n".join(lines)


def _copy_user_prompt(collect: dict, avoid: list[str] | None = None) -> str:
    title_raw = collect.get("title_raw") or ""
    lines = [
        f"商品（1688 原始标题）：{title_raw}",
        f"类目：{collect.get('category_raw') or '未知'}",
        f"规格：{json.dumps(collect.get('specs') or {}, ensure_ascii=False)}",
        f"去噪打底标题：{clean_title(title_raw) or title_raw}",
        f"图片数：{len(collect.get('image_urls') or [])}",
    ]
    if avoid:
        lines += [
            "",
            f"上一版命中违规词：{'、'.join(avoid)}",
            "重写时必须彻底去掉这些词及其同义词，禁止改用谐音、拆字、加空格绕过。",
        ]
    return "\n".join(lines)


# ---------------- 定价（§7.1 三级降级链，本函数永不抛错——降级链本身就是兜底） ----------------

def _fallback_rule(cost_cent: int, reason: str) -> dict:
    rate = settings.collect_markup_rate
    return {
        "pricing": "fallback-rule",
        "price_cent": markup_price(cost_cent, rate),
        "orig_price_cent": None,
        "delivery": "包邮",
        "confidence": "low",
        "reasoning": f"[2] 规则兜底：{reason}；按成本×{rate} 计算，待人工复核。",
        "decided_by": f"rule:markup-{rate}",
    }


def _manual(reason: str) -> dict:
    return {
        "pricing": "manual",
        "price_cent": None,
        "orig_price_cent": None,
        "delivery": "包邮",
        "confidence": "low",
        "reasoning": f"[3] {reason}（必须人工填价后才可放行）。",
        "decided_by": "pending:manual",
    }


async def price_product(collect: dict, *, preference: str | None = None, provider=None) -> dict:
    """产出 decision 的价格字段（§7.3 兜底：绝不把模型输出直接入库，先过护栏）。"""
    cost = int(collect.get("source_price_cent") or 0)
    if cost <= 0 or not collect.get("price_available", cost > 0):
        return _manual("未拿到 1688 成本价")

    try:
        out = await chat_json(
            PRICING_SYSTEM, _pricing_user_prompt(collect, preference),
            PricingOut, provider=provider, temperature=0.3,
        )
    except StructuredOutputError as exc:
        return _fallback_rule(cost, f"LLM 定价不可用（{str(exc)[:120]}）")

    if out.pricing == "manual":
        merged = _manual(out.reasoning or "模型判定偏好与护栏区间冲突")
        merged["delivery"] = out.delivery
        return merged
    if out.pricing == "fallback-rule":
        return _fallback_rule(cost, out.reasoning or "模型自判回落规则")

    low, high = round(cost * COST_MIN_MULT), round(cost * COST_MAX_MULT)
    if not low <= out.price_cent <= high:
        return _fallback_rule(
            cost, f"LLM 出价 {out.price_cent} 分越界（允许区间 [{low}, {high}]）"
        )

    # 划线原价：必须 > 售价且 ≤ 成本×5，否则视作编造，置 null（§7.3 第 6 条）
    orig = out.orig_price_cent
    if orig is not None and not (out.price_cent < orig <= round(cost * 5)):
        orig = None
    return {
        "pricing": "llm",
        "price_cent": out.price_cent,
        "orig_price_cent": orig,
        "delivery": out.delivery,
        # 无竞品数据源（§7.3 第 9 条 + §5 不提供 search_competitors）→ 上限锁 medium
        "confidence": "medium" if out.confidence == "high" else out.confidence,
        "reasoning": out.reasoning,
        "markup_ratio_actual": round(out.price_cent / cost, 2),
        "decided_by": f"llm:{getattr(provider, 'model', None) or settings.openai_model}",
    }


# ---------------- 文案（§7.4：成品文案只出自模型） ----------------

async def draft_copy(
    collect: dict, *, provider=None, avoid: list[str] | None = None
) -> dict:
    """定价之外的一段：产标题/描述。**模型写不出来就交白卷，不拿 1688 源标题顶上。**

    `clean_title(title_raw)` 只是喂给模型的打底材料（见 `_copy_user_prompt`）。从前它也充当
    模型失败时的成品文案，后果是：源标题动辄 >30 字、带「超级/爆款」，落进 decision 必撞
    `title_too_long`，而该 code 在 `NOT_COPY_FIXABLE` 里 → 改写循环天生不救它，于是每一件都
    以「风险扫描未过」卡死在放行闸门前，面板上却显示得像文案已经写好了。
    """
    try:
        out = await chat_json(
            COPY_SYSTEM, _copy_user_prompt(collect, avoid),
            CopyOut, provider=provider, temperature=0.7,
        )
    except StructuredOutputError as exc:
        logger.warning("文案模型不可用，decision 留空待重跑或人工改写: %s", str(exc)[:150])
        return {
            "title": "",
            "desc": "",
            "title_candidates": [],
            "copy_source": "none",
            "copy_error": str(exc)[:200],
        }
    return {
        "title": out.title_candidates[0],
        "desc": out.desc,
        "title_candidates": out.title_candidates,
        "copy_source": "llm",
        "copy_error": None,
    }


# ---------------- 编排：读 product.json → 定价+文案 → 写 decision ----------------

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _build_decision(collect: dict, priced: dict, copied: dict) -> dict:
    return {
        "status": "proposed",
        **priced,
        "title": copied["title"],
        "desc": copied["desc"],
        "title_candidates": copied["title_candidates"],
        "copy_source": copied["copy_source"],
        "copy_error": copied.get("copy_error"),
        "evidence": [
            {
                "type": "cost",
                "price_cent": int(collect.get("source_price_cent") or 0),
                "source": collect.get("url") or "1688 详情页",
                "at": collect.get("fetched_at"),
            }
        ],
        "risk_scan": None,  # 下面循环里回填；未过扫描的 decision 不可放行（§8.4 节点 5）
        "decided_at": _now_iso(),
        "confirmed_by": None,
        "confirmed_at": None,
    }


async def price_and_copy(
    workspace,
    batch_id: str,
    offer_id: str,
    *,
    preference: str | None = None,
    provider=None,
    force: bool = False,
) -> dict:
    """§5 工具 4（文档名 `price_and_write_copy`）的 service 本体，MCP/REST 共用（§4 铁律 3）。

    流程 = §8.4 节点 2–4：定价 → 文案 → 风险扫描 → 命中可改写违规则回文案重扫（≤2 轮）。
    循环放本层而非 Skill 层：定时任务无人值守也要跑到 `decided` 为止（§8.1）。

    返回写入的 decision 段；decision 已存在且未 force → 抛 DecisionExists。
    """
    product = workspace.read_product(batch_id, offer_id)
    if product.get("decision") and not force:
        raise DecisionExists(f"{batch_id}/{offer_id} 已有 decision，覆盖需 force=True")

    collect = product["collect"]
    priced = await price_product(collect, preference=preference, provider=provider)
    copied = await draft_copy(collect, provider=provider)
    decision = _build_decision(collect, priced, copied)

    rounds = 0
    while True:
        scan = run_risk_scan({**product, "decision": decision})
        decision["risk_scan"] = scan
        terms = rewrite_terms(scan)
        if not terms or rounds >= RISK_REWRITE_ROUNDS:
            break
        rounds += 1
        logger.info("风险命中 %d 个可改写词，第 %d 轮重写 offer=%s: %s",
                    len(terms), rounds, offer_id, "、".join(terms))
        copied = await draft_copy(collect, provider=provider, avoid=terms)
        if copied["copy_source"] != "llm":
            # 改写这一轮模型挂了：留上一版（那是模型写的、只是还带着违规词），别用白卷覆盖
            decision["copy_error"] = copied.get("copy_error")
            logger.warning("第 %d 轮文案改写失败，保留上一版 offer=%s", rounds, offer_id)
            break
        decision.update(
            title=copied["title"],
            desc=copied["desc"],
            title_candidates=copied["title_candidates"],
            copy_source=copied["copy_source"],
        )
    decision["risk_rewrite_rounds"] = rounds

    workspace.write_decision(batch_id, offer_id, decision)
    logger.info(
        "decision 已产出 batch=%s offer=%s pricing=%s price=%s risk_passed=%s",
        batch_id, offer_id, decision["pricing"], decision["price_cent"],
        decision["risk_scan"]["passed"],
    )
    return decision
