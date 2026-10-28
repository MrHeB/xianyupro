"""采集池流转 service（设计文档 §8.1 / §8.3）：定价（decide）与人工放行（approve）。

REST 与 MCP 工具共用本模块（§4 铁律 3），两处口径必须一致：

- `decide_offer`：§5 工具 4 的本体——跑 §8.4 节点 2–4（定价 → 文案 → 风险扫描 → ≤2 轮重写），
  写 product.json 的 decision 段，并把 Goods 从 SOURCED 升到 DECIDED（= 待确认）。
  **不产生任何闲鱼平台请求**（§4 铁律 2：触达平台的写出口只有 publisher）。
- `approve_offers`：§8.3「批量放行」——校验 decision 完整 + 风险扫描通过，把 decision 的
  标题/文案/价格写进 Goods 并置 MAPPED，之后才由存量 `auto_listing` 或行内「上架」发出。

放行闸门全部落代码层（风控手册 §1.7），不依赖前端置灰，也不给模型留 force/skip_risk 余地。
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.decision import DecisionExists, price_and_copy
from app.ai.risk_scan import run_risk_scan
from app.collectors.alibaba.workspace import CollectWorkspace
from app.collectors.base import PoolStatus
from app.models import Goods

logger = logging.getLogger(__name__)

# §8.3 第 1 条：单次批量放行上限
MAX_APPROVE_BATCH = 5


class ApprovalError(ValueError):
    """放行前置校验失败（整批拒绝，逐件报因）。"""


def offer_dir_of(goods: Goods) -> Path | None:
    """Goods.local_path 指向 offers/<offer_id>/（采集时写入）。"""
    return Path(goods.local_path) if goods.local_path else None


def read_workspace_product(goods: Goods, workspace: CollectWorkspace) -> dict | None:
    """按 Goods 反查 product.json（目录结构 <batch>/offers/<offer_id>/product.json）。"""
    d = offer_dir_of(goods)
    if d is None or not (d / "product.json").is_file():
        return None
    batch_id = d.parent.parent.name
    try:
        return workspace.read_product(batch_id, d.name)
    except (OSError, ValueError) as exc:
        logger.warning("product.json 读取失败 goods_id=%d: %s", goods.id, exc)
        return None


async def _llm_provider(db: AsyncSession):
    """定价/文案用的 provider，读运行时 LLM 配置（DB 覆盖 .env），与 reply_engine 同源。

    实现收在 `ai/provider.runtime_provider`（意图解析也要同一个口径），这里只留别名。
    """
    from app.ai.provider import runtime_provider

    return await runtime_provider(db)


async def decide_offer(
    db: AsyncSession,
    *,
    batch_id: str,
    offer_id: str,
    preference: str | None = None,
    force: bool = False,
    workspace: CollectWorkspace | None = None,
) -> dict:
    """定价 + 文案 + 风险扫描（§8.4 节点 2–4），成功后 Goods SOURCED → DECIDED。

    Returns: {"batch_id","offer_id","decision":…,"goods_id":…|None}
    Raises: DecisionExists（已有 decision 且未 force）——由调用方转 409。
    """
    workspace = workspace or CollectWorkspace()
    decision = await price_and_copy(
        workspace, batch_id, offer_id, preference=preference, force=force,
        provider=await _llm_provider(db),
    )
    goods = (
        await db.execute(select(Goods).where(Goods.source_offer_id == offer_id))
    ).scalar_one_or_none()
    if goods is not None and goods.pool_status == PoolStatus.SOURCED.value:
        goods.pool_status = PoolStatus.DECIDED.value
        await db.commit()
    return {
        "batch_id": batch_id,
        "offer_id": offer_id,
        "goods_id": goods.id if goods else None,
        "decision": decision,
    }


def _approval_blockers(goods: Goods, product: dict | None) -> list[str]:
    """单件可放行性校验（§8.3 / §8.4 节点 5）：返回缺项列表，空表示放行。"""
    if goods.pool_status == PoolStatus.PUSHED.value or goods.external_goods_id:
        return ["已在售（已绑定闲鱼商品 ID）"]
    if goods.pool_status == PoolStatus.MAPPED.value:
        return ["已放行，等待上架（无需重复放行）"]
    if product is None:
        return ["本地工作区缺 product.json（先采集或重新采集）"]
    decision = product.get("decision")
    if not decision:
        return ["未跑定价/文案（先点「生成定价与文案」）"]
    missing: list[str] = []
    if decision.get("copy_source") == "none":
        missing.append(
            "文案未产出（模型当时不可用："
            f"{(decision.get('copy_error') or '见 decision')[:80]}），重跑定价文案或人工改写"
        )
    if decision.get("pricing") == "manual" or not decision.get("price_cent"):
        # §8.1：pricing=manual 的行禁止留空放行
        missing.append("定价为 manual（成本价缺失或模型判越界），必须人工先填价")
    if not (decision.get("title") or "").strip():
        missing.append("decision 缺标题")
    scan = decision.get("risk_scan")
    if not scan:
        missing.append("未过风险扫描（decision.risk_scan 为空）")
    elif not scan.get("passed"):
        hits = [
            f"{b.get('code')}@{b.get('field')}:{b.get('term')}"
            for b in scan.get("blockers") or []
        ]
        missing.append(f"风险扫描未通过：{'、'.join(hits) or '见详情'}")
    if not (product.get("media") or {}).get("local_images"):
        missing.append("本地无图片（发布至少 1 张）")
    return missing


async def approve_offers(
    db: AsyncSession,
    *,
    goods_ids: list[int],
    confirmed_by: str = "user",
    workspace: CollectWorkspace | None = None,
) -> dict:
    """批量放行（DECIDED → MAPPED）：任一件不合格则**整批拒绝**并逐件报因（§8.4 节点 6）。

    Returns: {"approved": [goods_id…], "applied": 件数}
    Raises: ApprovalError(detail=str) —— 调用方转 400。
    """
    workspace = workspace or CollectWorkspace()
    if not goods_ids:
        raise ApprovalError("未选择商品")
    if len(goods_ids) > MAX_APPROVE_BATCH:
        raise ApprovalError(f"单次批量放行上限 {MAX_APPROVE_BATCH} 件（当前 {len(goods_ids)} 件）")

    rows = (
        await db.execute(select(Goods).where(Goods.id.in_(goods_ids)))
    ).scalars().all()
    found = {g.id: g for g in rows}
    failures: list[dict[str, Any]] = []
    prepared: list[tuple[Goods, dict]] = []
    for gid in goods_ids:
        goods = found.get(gid)
        if goods is None:
            failures.append({"goods_id": gid, "reasons": ["商品不存在"]})
            continue
        reasons = _approval_blockers(goods, read_workspace_product(goods, workspace))
        if reasons:
            failures.append(
                {"goods_id": gid, "title": goods.title, "source_offer_id": goods.source_offer_id,
                 "reasons": reasons}
            )
        else:
            prepared.append((goods, read_workspace_product(goods, workspace)))

    if failures:
        raise ApprovalError("整批未放行（存在不合格项）：" + "；".join(
            f"#{f['goods_id']} {f.get('title', '')}：{'、'.join(f['reasons'])}" for f in failures
        ))

    for goods, product in prepared:
        decision = product["decision"]
        goods.title = decision["title"][:128]
        goods.description = decision["desc"]
        goods.price_cent = int(decision["price_cent"])
        orig = decision.get("orig_price_cent")
        raw = dict(goods.raw_payload or {})
        if orig:
            raw["orig_price_cent"] = orig
        raw["confirmed_by"] = confirmed_by
        raw["confirmed_at"] = decision.get("decided_at")
        goods.raw_payload = raw
        goods.pool_status = PoolStatus.MAPPED.value
        logger.info(
            "人工放行 goods_id=%d offer_id=%s price=%d risk_passed=True",
            goods.id, goods.source_offer_id, goods.price_cent,
        )
    await db.commit()
    return {"approved": [g.id for g, _ in prepared], "applied": len(prepared)}


async def edit_offer(
    db: AsyncSession,
    *,
    goods_id: int,
    title: str | None = None,
    desc: str | None = None,
    price_cent: int | None = None,
    editor: str = "user",
    workspace: CollectWorkspace | None = None,
) -> dict:
    """人工改写采集件的标题/描述/售价，并立刻重扫风险（§9 缺口⑨）。

    改动落进 `decision` 段而不是只改 Goods：放行时 `approve_offers` 会把 decision 抄进
    Goods，只改行等于白改。填过价就把 `pricing` 置 `human`——留着 `manual` 的话
    放行闸门会一直拦着，而这条通道正是为它准备的。
    抛 ApprovalError（含"不存在/不在可改阶段/没有 decision"），由调用方转 400。
    """
    workspace = workspace or CollectWorkspace()
    goods = await db.get(Goods, goods_id)
    if goods is None:
        raise ApprovalError(f"商品不存在：{goods_id}")
    if goods.source != "1688":
        raise ApprovalError("只有 1688 采集件有「人工改写」通道（手动/CSV 件改的是它自己的记录）")
    if goods.external_goods_id or goods.pool_status == PoolStatus.PUSHED.value:
        raise ApprovalError("已在售：改标题/价格要走闲鱼商品编辑，不在这里")
    offer_dir = offer_dir_of(goods)
    if offer_dir is None:
        raise ApprovalError("该采集件没有 local_path，找不到工作区目录")

    batch_id = offer_dir.parent.parent.name
    product = workspace.read_product(batch_id, goods.source_offer_id)
    decision = product.get("decision")
    if not decision:
        raise ApprovalError("还没有 decision 段：先跑一次定价/文案，再在它之上人工改")

    if title is not None:
        decision["title"] = title.strip()
    if desc is not None:
        decision["desc"] = desc
    if price_cent is not None:
        decision["price_cent"] = int(price_cent)
        decision["pricing"] = "human"
    if title is not None or desc is not None:
        # 人工写过文案就不算"文案未产出"了（口径同 pricing=human）
        decision["copy_source"] = "human"
        decision["copy_error"] = None
    decision["risk_scan"] = run_risk_scan(product, goods_kind="physical")
    decision["edited_by"] = editor
    decision["edited_at"] = datetime.now().isoformat(timespec="seconds")
    # workspace.write_decision 要往 status.history 里追加一条 decided 记录，缺 decided_at 会 KeyError
    decision.setdefault("decided_at", decision["edited_at"])
    workspace.write_decision(batch_id, goods.source_offer_id, decision)

    goods.title = str(decision.get("title") or goods.title)[:128]
    goods.description = decision.get("desc") or goods.description
    if decision.get("price_cent") is not None:
        # manual 件的 decision 就是没价格，而 Goods.price_cent 非空——只在有值时镜像
        goods.price_cent = int(decision["price_cent"])
    if goods.pool_status == PoolStatus.SOURCED.value:
        goods.pool_status = PoolStatus.DECIDED.value
    await db.commit()
    logger.info(
        "人工改写 goods_id=%d offer_id=%s pricing=%s 风险=%s",
        goods.id, goods.source_offer_id, decision.get("pricing"), decision["risk_scan"]["passed"],
    )
    return {
        "goods_id": goods.id,
        "batch_id": batch_id,
        "offer_id": goods.source_offer_id,
        "pool_status": goods.pool_status,
        "pricing": decision.get("pricing"),
        "price_cent": decision.get("price_cent"),
        "risk_scan": decision["risk_scan"],
    }


__all__ = [
    "ApprovalError",
    "DecisionExists",
    "MAX_APPROVE_BATCH",
    "approve_offers",
    "decide_offer",
    "edit_offer",
    "read_workspace_product",
]
