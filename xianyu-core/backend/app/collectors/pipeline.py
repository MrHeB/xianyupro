"""采集编排 service（需求文档 §3 / N4-N6）。

把原先写在 Skill 提示词里的八步流程搬成 Python：循环、轮数上限、每轮可用动作全部
写死在这里，大模型只在节点内出**数据**（一个关键词、一份结构化计划），不出**动作**
（D-2026-09-19-15）。

本模块**不 import `listing/publisher`，也不 import `pool_flow.approve_offers`**：
第 ⑤ 步是硬停点，产出待确认清单就返回。放行与上架仍是前端/人工走
`POST /api/goods/batch-approve` 与 `POST /api/goods/{id}/publish` 两道独立确认
（§8.1 防线，用户在 D-2026-09-19-17 里明确不放）。

端点与定时任务共用本函数（N5）——两边的差别只在"谁触发"，不在"跑什么"。
"""

import logging
import time
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.intent import CollectPlan, parse_intent, refine_keyword
from app.collectors.alibaba.executor import AlibabaExecutor
from app.collectors.alibaba.search_browser import SearchBlocked, search_offers
from app.collectors.alibaba.search_quota import SearchQuota
from app.collectors.alibaba.workspace import CollectWorkspace
from app.collectors.pool_flow import decide_offer
from app.core.config import settings

logger = logging.getLogger(__name__)

MAX_SEARCH_ROUNDS = 2  # 第 1 轮用原词，最多再换 1 次词（需求文档 §3 ②）


class CollectRunRequest(BaseModel):
    """一次采集编排的入参——端点请求体与 `scheduled_task.config_json` **同一个形状**。

    放在 service 而不是路由里，是为了让 N5（"端点与定时共用同一段代码"）不漏在参数层：
    两处存的字段集一样，改一处必然要改另一处，不会出现"定时少带一个偏好"的静默分叉。

    三个来源三选一，优先级 offer_ids > keyword > instruction。给了前两个就不花 LLM 调用
    （前端"检索完勾选再采"走那条）。这里刻意不做"至少给一个"的跨字段校验：手动入口要的
    是 400（路由自己判），定时要的是一条能看得到日志的 needs_clarification，两种处置都
    正确，模型层替它们预先选一种反而会逼其中一种绕路。
    """

    account_id: int
    instruction: str | None = Field(default=None, max_length=500)
    keyword: str | None = Field(default=None, max_length=60)
    offer_ids: list[str] | None = Field(default=None, max_length=50)
    preference: str | None = Field(default=None, max_length=200)
    alibaba_account_id: int | None = None
    max_items: int | None = Field(default=None, ge=1, le=50)

    def pipeline_kwargs(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "instruction": self.instruction,
            "keyword": self.keyword,
            "offer_ids": self.offer_ids,
            "preference": self.preference,
            "alibaba_account_id": self.alibaba_account_id,
            "max_items": self.max_items,
        }


async def run_collect_pipeline(
    db: AsyncSession,
    *,
    account_id: int,
    instruction: str | None = None,
    keyword: str | None = None,
    offer_ids: list[str] | None = None,
    preference: str | None = None,
    alibaba_account_id: int | None = None,
    max_items: int | None = None,
    trigger: str = "nl",
    workspace: CollectWorkspace | None = None,
    search_quota: SearchQuota | None = None,
) -> dict[str, Any]:
    """一句话（或一组显式参数）→ 采集 → 逐件定价文案扫描 → 待确认清单。

    返回 `status`：
      - "ok"                   跑完，`pending_review` 里是待放行清单
      - "needs_clarification"  缺关键槽位，`needs_clarification` 列出缺什么（不猜，不跑）
      - "no_source_account"    没有登录态可用的 1688 货源账号（启动浏览器之前就拒，N8）
      - "search_blocked"       检索一件也没拿到（风控/登录墙/配额用尽，见 `searched.rounds`）

    `search_quota` 只有定时路径会传（N7）：手动入口的闸门是人工点击，不给它加频控。
    """
    workspace = workspace or CollectWorkspace()
    max_items = max(1, min(int(max_items or settings.collect_pipeline_max_items), 50))
    started = time.monotonic()

    from app.auth.alibaba_account_service import (
        alibaba_cookie_text,
        pick_usable_alibaba_account,
    )

    # ---- 前置：货源账号登录态。详情页与搜索页匿名都只拿到 x5sec 壳，----
    # 先问一句再决定要不要开浏览器，否则整批 blocked 还白烧一次 chromium。
    src = None
    if alibaba_account_id is not None:
        from app.models import AlibabaAccount

        src = await db.get(AlibabaAccount, alibaba_account_id)
        if src is None or src.deleted or src.status != 1 or src.cookie_status != 1:
            src = None
    else:
        src = await pick_usable_alibaba_account(db)
    if src is None:
        return {
            "status": "no_source_account",
            "message": "没有登录态可用的 1688 货源账号，先到「1688 货源账号」扫码（Cookie 数小时即失效）",
        }
    cookie = await alibaba_cookie_text(db, src.id)

    # ---- ① 意图解析 ----
    if keyword or offer_ids:
        # 显式参数优先：前端"搜完勾选再采"走这条，不必再花一次 LLM
        plan = CollectPlan(
            keywords=[keyword] if keyword else [],
            quantity=len(offer_ids) if offer_ids else 3,
            offer_ids=list(offer_ids or []),
            preference=preference,
        )
        intent_source, needs = "explicit", []
    else:
        parsed = await parse_intent(db, instruction or "", max_items=max_items)
        plan = CollectPlan(**parsed["plan"])
        intent_source, needs = parsed["intent_source"], parsed["needs_clarification"]
        preference = preference or plan.preference
    if needs:
        return {
            "status": "needs_clarification",
            "needs_clarification": needs,
            "plan": plan.model_dump(),
            "intent_source": intent_source,
        }

    # ---- ② 货源定位（代码控环）----
    ids: list[str] = list(plan.offer_ids)
    searched: dict[str, Any] = {"rounds": [], "keyword_source": "given"}
    if not ids:
        wanted = min(plan.quantity, max_items)
        tried: list[str] = []
        blocked_reason: str | None = None
        for _ in range(MAX_SEARCH_ROUNDS):
            kw = next((k for k in plan.keywords if k not in tried), None)
            if kw is None and tried:
                kw, why = await refine_keyword(
                    db, plan=plan, tried=tried, got=len(ids), want=wanted
                )
                if kw is None:
                    searched["rounds"].append({"keyword": None, "stop_reason": why})
                    break
            if kw is None:
                break
            tried.append(kw)
            # 配额按"开一次浏览器"扣，不是按"跑一次定时"扣：换词的那一轮和第一轮一样贵。
            # 拿不到名额就地收手，已搜到的照用——这不是失败，是"今天到这儿"。
            if search_quota is not None and not search_quota.acquire(src.id):
                blocked_reason = "quota_exceeded"
                searched["quota_exhausted"] = True
                searched["rounds"].append({"keyword": kw, "blocked": blocked_reason})
                break
            try:
                found = await search_offers(
                    kw, cookie_text=cookie, limit=wanted - len(ids), max_pages=2
                )
            except SearchBlocked as exc:
                blocked_reason = exc.reason
                searched["rounds"].append({"keyword": kw, "blocked": exc.reason})
                break
            for oid in found.offer_ids:
                if oid not in ids:
                    ids.append(oid)
            searched["rounds"].append({"keyword": kw, "found": len(found.offer_ids),
                                       "source": found.source})
            searched["keyword_source"] = found.source
            if len(ids) >= wanted:
                break
        ids = ids[:wanted]
        if not ids:
            return {
                "status": "search_blocked",
                "message": f"检索一件也没拿到（{blocked_reason or 'no_result'}）"
                + ("：本小时该货源账号的检索次数已用完，下轮再试"
                   if blocked_reason == "quota_exceeded" else ""),
                "searched": searched,
                "plan": plan.model_dump(),
                "intent_source": intent_source,
            }

    # ---- ③ 采集（连续 3 件撞风控即中止，executor 内部已实现）----
    executor = AlibabaExecutor(cookie_text=cookie, workspace=workspace)
    try:
        collected = await executor.collect_by_offer_ids(
            db, account_id, ids, trigger=trigger,
            instruction=instruction or keyword or " ".join(plan.keywords),
        )
    finally:
        await executor.aclose()
    batch_id = collected["batch_id"]

    # ---- ④ 逐件定价/文案/风险扫描：单件失败不拖垮整批（N6）----
    decided: list[dict[str, Any]] = []
    for offer_id in workspace.list_offer_ids(batch_id):
        if offer_id not in ids:
            continue
        try:
            product = workspace.read_product(batch_id, offer_id)
        except (OSError, ValueError) as exc:
            decided.append({"offer_id": offer_id, "error": f"product.json 读取失败：{exc}"})
            continue
        # 风控壳/解析空的件也落 product.json（collect 段是空标题占位），判 stage 才准
        stage = (product.get("status") or {}).get("stage") or ""
        if stage == "blocked" or not (product.get("collect") or {}).get("title_raw"):
            decided.append({"offer_id": offer_id, "skipped": "未采到商品数据（见 blocked_reason）"})
            continue
        try:
            out = await decide_offer(
                db, batch_id=batch_id, offer_id=offer_id,
                preference=preference, force=False, workspace=workspace,
            )
        except Exception as exc:  # noqa: BLE001 —— N6：单件失败要记账继续跑，但不能吞掉原因
            logger.warning("decide 失败 batch=%s offer=%s: %s", batch_id, offer_id, exc)
            decided.append({"offer_id": offer_id, "error": f"{type(exc).__name__}: {exc}"})
            continue
        d = out["decision"]
        scan = d.get("risk_scan") or {}
        decided.append({
            "offer_id": offer_id,
            "goods_id": out.get("goods_id"),
            "title": d.get("title"),
            "price_cent": d.get("price_cent"),
            "source_price_cent": (product.get("collect") or {}).get("source_price_cent"),
            "pricing": d.get("pricing"),
            "copy_source": d.get("copy_source"),
            "confidence": d.get("confidence"),
            "risk_passed": scan.get("passed"),
            "risk_blockers": scan.get("blockers") or [],
        })

    # ---- ⑤ 待确认清单（硬停点）----
    return {
        "status": "ok",
        "batch_id": batch_id,
        "intent_source": intent_source,
        "plan": plan.model_dump(),
        "searched": searched,
        "collected": {k: v for k, v in collected.items() if k != "batch_id"},
        "decided": decided,
        "reviewable": [x for x in decided if x.get("risk_passed") and x.get("price_cent")],
        "needs_human": [x["offer_id"] for x in decided
                        if x.get("pricing") == "manual" or x.get("risk_passed") is False],
        "alibaba_account_id": src.id,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
