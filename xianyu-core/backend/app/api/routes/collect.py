"""采集工作区接口（设计文档 §3.1 本地文件夹 → 前端可视化）。

只读为主 + 两个本地写（decide、schedule）+ 一个外部只读（search-1688）+ 一条编排链路
（run）：本路由**不产生任何闲鱼平台请求**（§4 铁律 2），`/run` 也只跑到 DECIDED 就停。
`search-1688` 与 `/run` 都会打 1688（真浏览器检索 + httpx 详情页，只读、不写 1688），
它们不属于"平台写出口"，但确实是对外请求，风控口径见需求文档 §6 风险 2。
`PUT /schedule` 只改自家 `scheduled_task` 行，不在这里检索也不采集。
数据源是 `data/collect/` 工作区（product.json 四段），Goods 只用于账号归属与状态机。
"""

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession
from app.collectors.alibaba.search_browser import SearchBlocked, search_offers
from app.collectors.alibaba.search_quota import search_quota
from app.collectors.alibaba.workspace import CollectWorkspace
from app.collectors.base import PoolStatus
from app.collectors.pipeline import CollectRunRequest, run_collect_pipeline
from app.collectors.pool_flow import decide_offer, read_workspace_product
from app.core.config import settings
from app.models import Account, AlibabaAccount, Goods, ScheduledTask, User
from app.scheduler.task_runner import COLLECT_TASK_TYPE, TASK_INTERVALS
from app.users.deps import get_current_user, require_admin
from app.users.scoping import ensure_account_scope, visible_account_ids

router = APIRouter(prefix="/collect", tags=["collect"])

# 目录名安全字符集：batch_id = 时间戳_关键词slug（make_batch_id 会保留中文），offer_id 为数字。
# \w 在 Python3 为 Unicode 语义（含中文），仍排除 / \ 与空字节；".." 另做显式拒绝。
_SAFE = re.compile(r"^[\w.\-]{1,80}$")
_IMAGE = re.compile(r"^[0-9]{1,3}\.jpg$")

workspace = CollectWorkspace()


def _check_id(value: str, label: str) -> str:
    if not _SAFE.match(value) or ".." in value:
        raise HTTPException(400, f"{label} 非法：{value!r}")
    return value


def _resolve(root: Path, *parts: str) -> Path:
    """拼接后确认仍在 workspace.root 内（防 ../ 穿越）。"""
    target = (root / Path(*parts)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(400, "路径越界") from exc
    return target


def _brief(product: dict) -> dict[str, Any]:
    """product.json → 列表页摘要（四段各取关键字段，不传全文）。"""
    collect = product.get("collect") or {}
    decision = product.get("decision") or {}
    media = product.get("media") or {}
    status = product.get("status") or {}
    history = status.get("history") or []
    scan = decision.get("risk_scan") or {}
    images = media.get("local_images") or []
    return {
        "stage": status.get("stage"),
        # 风控壳/解析空的件没有 Goods 行，面板只能靠这个字段回答"这件为什么没有"
        "blocked_reason": next((h.get("reason") for h in reversed(history) if h.get("reason")), None),
        "title_raw": collect.get("title_raw"),
        "source_price_cent": collect.get("source_price_cent"),
        "price_available": collect.get("price_available"),
        "url": collect.get("url"),
        "fetched_at": collect.get("fetched_at"),
        "has_decision": bool(decision),
        "pricing": decision.get("pricing"),
        "proposed_title": decision.get("title"),
        "proposed_price_cent": decision.get("price_cent"),
        "confidence": decision.get("confidence"),
        "markup_ratio_actual": decision.get("markup_ratio_actual"),
        "reasoning": decision.get("reasoning"),
        "copy_source": decision.get("copy_source"),
        "copy_error": decision.get("copy_error"),
        "risk_passed": scan.get("passed"),
        "risk_blockers": scan.get("blockers") or [],
        "risk_warnings": scan.get("warnings") or [],
        "not_covered": scan.get("not_covered") or [],
        "rewrite_rounds": decision.get("risk_rewrite_rounds"),
        "image_count": len(images),
        "image_count_ok": scan.get("image_count_ok", 1 <= len(images) <= 9),
        "local_images": images,
    }


async def _goods_by_offer(db: AsyncSession, offer_id: str) -> Goods | None:
    return (
        await db.execute(select(Goods).where(Goods.source_offer_id == offer_id))
    ).scalar_one_or_none()


# ---------------- 批次 ----------------


@router.get("/batches")
async def list_batches(
    limit: int = 50, user: User = Depends(get_current_user)
) -> list[dict]:
    """批次台账（batches.jsonl + batch.json 合成，倒序）。"""
    return workspace.list_batches(limit=limit)


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str, db: DbSession, user: User = Depends(get_current_user)) -> dict:
    """单批详情：元信息 + 每件商品的四段摘要（不可见账号的行剔除）。"""
    _check_id(batch_id, "batch_id")
    if not workspace.batch_dir(batch_id).is_dir():
        raise HTTPException(404, f"批次不存在：{batch_id}")
    ids = await visible_account_ids(db, user)
    meta = workspace.read_batch(batch_id)
    items = []
    for offer_id in workspace.list_offer_ids(batch_id):
        goods = await _goods_by_offer(db, offer_id)
        if goods is not None and ids is not None and goods.account_id not in ids:
            continue
        try:
            product = workspace.read_product(batch_id, offer_id)
        except (OSError, ValueError) as exc:
            items.append({"offer_id": offer_id, "error": f"product.json 读取失败：{exc}"})
            continue
        items.append({"offer_id": offer_id, "goods_id": goods.id if goods else None,
                      **_brief(product)})
    return {**meta, "items": items}


# ---------------- 单件 ----------------


@router.get("/offers/{batch_id}/{offer_id}")
async def get_offer(
    batch_id: str, offer_id: str, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """单件全文（product.json 四段原样 + Goods 行摘要），供人工逐段核对。"""
    _check_id(batch_id, "batch_id")
    _check_id(offer_id, "offer_id")
    path = _resolve(workspace.root, batch_id, "offers", offer_id, "product.json")
    if not path.is_file():
        raise HTTPException(404, f"未找到 {batch_id}/{offer_id} 的 product.json")
    try:
        product = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"product.json 损坏：{exc}") from exc
    goods = await _goods_by_offer(db, offer_id)
    if goods is not None:
        await ensure_account_scope(db, user, goods.account_id)
    return {
        "batch_id": batch_id,
        "offer_id": offer_id,
        "product": product,
        "brief": _brief(product or {}),
        "goods": None if goods is None else {
            "id": goods.id,
            "account_id": goods.account_id,
            "title": goods.title,
            "price_cent": goods.price_cent,
            "source_price_cent": goods.source_price_cent,
            "pool_status": goods.pool_status,
            "status": goods.status,
            "external_goods_id": goods.external_goods_id,
        },
    }


class DecideIn(BaseModel):
    """定价/文案触发入参（§5 工具 4）：preference 是用户偏好文本，force 覆盖已有 decision。"""

    preference: str | None = Field(default=None, max_length=200)
    force: bool = False


@router.post("/offers/{batch_id}/{offer_id}/decide")
async def offer_decide(
    batch_id: str,
    offer_id: str,
    body: DecideIn,
    db: DbSession,
    user: User = Depends(get_current_user),
) -> dict:
    """跑 §8.4 节点 2–4（LLM 定价 → 文案 → 风险扫描 → ≤2 轮重写），SOURCED → DECIDED。

    只写本地工作区与自家 DB 的 pool_status，不触达闲鱼。
    """
    _check_id(batch_id, "batch_id")
    _check_id(offer_id, "offer_id")
    goods = await _goods_by_offer(db, offer_id)
    if goods is None:
        raise HTTPException(404, f"该 offer_id 未入库：{offer_id}")
    await ensure_account_scope(db, user, goods.account_id)
    from app.ai.decision import DecisionExists

    try:
        return await decide_offer(
            db, batch_id=batch_id, offer_id=offer_id,
            preference=body.preference, force=body.force, workspace=workspace,
        )
    except DecisionExists as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/offers/{batch_id}/{offer_id}/images/{name}")
async def offer_image(batch_id: str, offer_id: str, name: str) -> FileResponse:
    """本地图片原件（人工核对用）。仅允许 images/ 下的 *.jpg。"""
    _check_id(batch_id, "batch_id")
    _check_id(offer_id, "offer_id")
    if not _IMAGE.match(name):
        raise HTTPException(400, f"图片名非法：{name!r}")
    path = _resolve(workspace.root, batch_id, "offers", offer_id, "images", name)
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path)


# ---------------- 待确认清单 ----------------


@router.get("/pending")
async def list_pending(
    db: DbSession,
    user: User = Depends(get_current_user),
    pool_status: str = "DECIDED",
    account_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """待确认清单（§8.1）：默认已跑到 DECIDED 的采集件，附定价模式与风险扫描结论。

    pool_status=SOURCED 可查「采了但还没跑定价」的积压件。MAPPED/PUSHED 不在此列。
    """
    if pool_status not in (PoolStatus.SOURCED.value, PoolStatus.DECIDED.value):
        raise HTTPException(400, f"不支持的状态筛选：{pool_status}")
    ids = await visible_account_ids(db, user)
    stmt = (
        select(Goods)
        .where(Goods.source == "1688", Goods.pool_status == pool_status)
        .order_by(Goods.id.desc())
        .limit(min(limit, 200))
    )
    if ids is not None:
        stmt = stmt.where(Goods.account_id.in_(ids))
        if account_id is not None and account_id not in ids:
            return []
    if account_id is not None:
        stmt = stmt.where(Goods.account_id == account_id)
    rows = (await db.execute(stmt)).scalars().all()
    out = []
    for g in rows:
        product = read_workspace_product(g, workspace)
        item = {
            "goods_id": g.id,
            "account_id": g.account_id,
            "offer_id": g.source_offer_id,
            "pool_status": g.pool_status,
            "goods_title": g.title,
            "goods_price_cent": g.price_cent,
            "source_price_cent": g.source_price_cent,
            "error": "缺 product.json（未落工作区或目录被清）",
        }
        if product:
            batch_id = Path(g.local_path or "").parent.parent.name or None
            item.update({"batch_id": batch_id, **_brief(product), "error": None})
        out.append(item)
    return out


# ---------------- 关键词检索（只出候选，不入库） ----------------


class Search1688In(BaseModel):
    """1688 关键词检索入参。`alibaba_account_id` 缺省时自动挑一个登录态可用的货源账号。"""

    keyword: str = Field(min_length=1, max_length=60)
    limit: int | None = Field(default=None, ge=1, le=50)
    alibaba_account_id: int | None = None
    max_pages: int = Field(default=1, ge=1, le=3)


@router.post("/search-1688")
async def search_1688(
    body: Search1688In, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """关键词 → offer_id 候选列表（一次性浏览器拦 1688 搜索页接口）。

    刻意与采集分开：先让人看清候选再决定采哪几件，而不是"搜到就全采"。
    本端点不写 Goods、不落工作区、不触达闲鱼。
    """
    from app.auth.alibaba_account_service import alibaba_cookie_text, pick_usable_alibaba_account

    if body.alibaba_account_id is not None:
        src = await db.get(AlibabaAccount, body.alibaba_account_id)
        if src is None or src.deleted:
            raise HTTPException(404, f"1688 货源账号不存在：{body.alibaba_account_id}")
        if user.role != "admin" and src.user_id != user.id:
            raise HTTPException(403, "无权使用该 1688 货源账号")
        if src.cookie_status != 1 or src.status != 1:
            raise HTTPException(409, "该货源账号登录态不可用，先到「1688 货源账号」重新扫码")
    else:
        owner = None if user.role == "admin" else user.id
        src = await pick_usable_alibaba_account(db, user_id=owner)
        if src is None:
            raise HTTPException(409, "没有登录态可用的 1688 货源账号，先扫码再检索（匿名搜索页只会拿到风控壳）")

    cookie = await alibaba_cookie_text(db, src.id)
    limit = body.limit or settings.collect_search_max_items
    try:
        found = await search_offers(
            body.keyword, cookie_text=cookie, limit=limit, max_pages=body.max_pages
        )
    except SearchBlocked as exc:
        # 502：不是调用方的错，是上游给了个不是结果页的东西
        hint = {
            "risk_shell": "被 1688 风控拦截，先降低检索频率再试",
            "login_wall": "货源账号登录态已失效，请重新扫码",
            "no_result": "页面已打开但没解析出商品 ID（关键词无结果或列表页改版）",
        }.get(exc.reason, str(exc))
        raise HTTPException(502, f"检索失败（{exc.reason}）：{hint}") from exc
    return {
        "keyword": body.keyword,
        "offer_ids": found.offer_ids,
        "count": len(found.offer_ids),
        "source": found.source,
        "pages": found.pages,
        "alibaba_account_id": src.id,
    }


# ---------------- 自然语言采集编排（需求文档 §3 五节点） ----------------


RunPipelineIn = CollectRunRequest  # 参数契约与定时任务的 config_json 同一个形状（N5）


@router.post("/run")
async def run_collect(
    body: RunPipelineIn, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """采集编排：意图解析 → 检索定位 → 详情页采集 → 逐件定价文案扫描 → 待确认清单。

    与 `search-1688` + `collect-1688` 的区别是"一次说清"：这条链路把原本写在 Skill
    提示词里的八步搬进了 `collectors/pipeline.py`，循环与轮数上限是代码写死的，模型
    只在节点内出数据（D-2026-09-19-15）。

    **链路终点是 DECIDED**：不 approve、不 publish，本模块连那两个符号都不 import。
    放行仍要人在前端勾选后走 `POST /api/goods/batch-approve`。

    `status` 是业务结论而非 HTTP 错误码（needs_clarification / no_source_account /
    search_blocked 都是"正常跑出了个没结果"，前端要按状态分别渲染追问，别当 5xx 处理）；
    只有入参非法、账号不存在、越权才是 4xx。

    注意这是同步长请求：一次浏览器检索 + N 件详情页（每件 1–3s 随机延迟）+ 每件 2 次
    LLM 调用，`max_items` 默认 20，别在前端把它调大。
    """
    if not (body.instruction or body.keyword or body.offer_ids):
        raise HTTPException(400, "instruction / keyword / offer_ids 至少给一个")
    await ensure_account_scope(db, user, body.account_id)
    if await db.get(Account, body.account_id) is None:
        raise HTTPException(404, f"账号不存在：{body.account_id}")
    if body.alibaba_account_id is not None:
        src = await db.get(AlibabaAccount, body.alibaba_account_id)
        if src is None or src.deleted:
            raise HTTPException(404, f"1688 货源账号不存在：{body.alibaba_account_id}")
        if user.role != "admin" and src.user_id != user.id:
            raise HTTPException(403, "无权使用该 1688 货源账号")

    # 参数整体转发，不逐字段挑：漏传一个字段就是"端点跑得过、定时跑不过"的那种分叉
    return await run_collect_pipeline(
        db,
        **body.pipeline_kwargs(),
        trigger="nl" if body.instruction else "manual",
        workspace=workspace,
    )


# ---------------- 定时采集计划（需求文档 S3 / 主设计 §8.2） ----------------


class ScheduleIn(BaseModel):
    """写进 `scheduled_task.config_json` 的采集计划；到期由 handler 原样喂给同一个 pipeline。"""

    enabled: bool = True
    config: CollectRunRequest


def _schedule_view(row: ScheduledTask) -> dict:
    cfg = row.config_json or {}
    aid = cfg.get("alibaba_account_id")
    return {
        "task_id": row.id,
        "task_type": row.task_type,
        "enabled": bool(row.enabled),
        "interval_seconds": TASK_INTERVALS.get(row.task_type),
        "config": row.config_json,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "run_attempt_count": row.run_attempt_count,
        # 无人值守时这几个数是唯一可观测面：>0 表示"最近几次到点没产出清单"
        "consecutive_failure_count": row.consecutive_failure_count,
        # 没写死货源账号时运行时才 pick 一个，配额对象就不确定，宁可不报也不猜
        "quota": None if aid is None else {
            "alibaba_account_id": aid,
            "per_hour": search_quota.limit,
            "remaining": search_quota.remaining(aid),
        },
    }


@router.get("/schedule")
async def get_schedule(
    db: DbSession, user: User = Depends(get_current_user)
) -> list[dict]:
    """定时采集任务现状（一期只有一行；`enabled=0` 或无 config 时到点什么都不做）。"""
    rows = (
        await db.execute(
            select(ScheduledTask)
            .where(ScheduledTask.task_type == COLLECT_TASK_TYPE)
            .order_by(ScheduledTask.id)
        )
    ).scalars().all()
    return [_schedule_view(r) for r in rows]


@router.put("/schedule")
async def put_schedule(
    body: ScheduleIn, db: DbSession, user: User = Depends(require_admin)
) -> dict:
    """设置定时采集计划（仅管理员：`scheduled_task` 是全局行，没有归属列，见多用户需求 §阶段）。

    `enabled=true` 后**最多一个轮询周期（60s）就会跑第一轮**，不是等满 30 分钟——补种的行
    `last_run_at` 为空，到期判定视为"该跑了"。所以这个开关本身就是"现在验一次"的按钮。

    校验放在这里而不是 handler 里——改配置的人在场，报错有人看得懂；到点再炸只有一条日志。
    执行侧仍走 `run_collect_pipeline`（N5）：这里只是决定"到点跑不跑、拿什么跑"。
    """
    cfg = body.config
    if not (cfg.instruction or cfg.keyword or cfg.offer_ids):
        raise HTTPException(400, "instruction / keyword / offer_ids 至少给一个")
    if await db.get(Account, cfg.account_id) is None:
        raise HTTPException(404, f"账号不存在：{cfg.account_id}")
    if cfg.alibaba_account_id is not None:
        src = await db.get(AlibabaAccount, cfg.alibaba_account_id)
        if src is None or src.deleted:
            raise HTTPException(404, f"1688 货源账号不存在：{cfg.alibaba_account_id}")

    row = (
        await db.execute(
            select(ScheduledTask)
            .where(ScheduledTask.task_type == COLLECT_TASK_TYPE)
            .order_by(ScheduledTask.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        interval = TASK_INTERVALS[COLLECT_TASK_TYPE]
        row = ScheduledTask(
            task_type=COLLECT_TASK_TYPE,
            # 占位：到期判定走 TASK_INTERVALS，接 croniter 后这栏才说话
            cron_expression=f"*/{max(1, interval // 60)} * * * *",
        )
        db.add(row)
    row.enabled = 1 if body.enabled else 0
    row.config_json = cfg.model_dump()
    await db.commit()
    await db.refresh(row)
    return _schedule_view(row)
