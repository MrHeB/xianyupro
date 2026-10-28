"""调度中心：DB 租约抢单的常驻 worker（阶段 4-5 接线，参考 pro scheduled_task 模式）。

不引入 Celery——单进程 asyncio 常驻循环 + scheduled_task 表 lease 三件套，
多 worker 天然互斥（UPDATE ... WHERE lease 过期，抢到才执行），Redis 只作辅助。

已注册任务：
- order_sync（600s）：遍历会话池存活账号，sold.get 拉单补齐 TradeOrder；
- delivery_recovery（600s）：heal_stuck_deliveries 兜底扫描（中间态超时转 unknown）。
- auto_listing（600s）：轮询商品池 MAPPED 状态商品，自动上架（写操作限流 1 次/分钟）。
- collect_1688（1800s）：按 scheduled_task.config_json 里的计划跑到「待确认清单」，
  与 POST /api/collect/run 共用同一个 pipeline，止于 DECIDED（需求文档 S3 / N5）。

任务间隔用 TASK_INTERVALS 常量表；cron_expression 字段保留给以后接 croniter
（避免为一个间隔解析引入新依赖）。cookie 三轨保活不在本表——由 auth.refresher
常驻循环独立负责。
"""

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import or_, select, update

from app.models import ScheduledTask

logger = logging.getLogger(__name__)

LEASE_SECONDS = 300  # 租约时长：任务最长执行时间
DEFAULT_POLL_INTERVAL = 60.0

# 定时采集的任务类型键（routes/collect.py 的 config_json 读写与这里必须同一个字符串）
COLLECT_TASK_TYPE = "collect_1688"

# 任务类型 → 执行间隔（秒）；due 判定 = last_run_at 为空或超过间隔
TASK_INTERVALS: dict[str, int] = {
    "order_sync": 600,
    "delivery_recovery": 600,
    "auto_listing": 600,
    # 主设计 §13-Q4 建议一期 30–60 分钟：定时一次 = N 件详情页 + 每件 2 次 LLM 调用，
    # 频次宁低勿高。检索配额（N7，每小时每货源账号 collect_search_hourly_quota 次）
    # 是另一道闸，见 collect_1688_task。
    COLLECT_TASK_TYPE: 1800,
}

# handler 签名：async (session_factory, task) -> None；task 是本任务的 scheduled_task 行
# （读 config_json 用）。失败抛异常由 runner 记连续失败。
TaskHandler = Callable[..., Awaitable[None]]


class TaskRunner:
    """调度 worker：到期任务 → 原子抢占租约 → 执行 → 记账。"""

    def __init__(self, session_factory, *, poll_interval: float = DEFAULT_POLL_INTERVAL):
        self.session_factory = session_factory
        self.poll_interval = poll_interval
        self.owner = f"worker-{uuid.uuid4().hex[:8]}"
        self._handlers: dict[str, TaskHandler] = {}
        self._running = False

    def register(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("调度 tick 异常")
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    async def _tick(self) -> None:
        now = datetime.now()
        async with self.session_factory() as db:
            tasks = (
                await db.execute(select(ScheduledTask).where(ScheduledTask.enabled == 1))
            ).scalars().all()

        for task in tasks:
            handler = self._handlers.get(task.task_type)
            if handler is None:
                continue
            if not _is_due(task, now):
                continue
            if not await self._claim(task.id, now):
                continue  # 已被别的 worker 抢走（或租约未过期）
            await self._execute(task.id, task.task_type, handler, task)

    async def _claim(self, task_id: int, now: datetime) -> bool:
        """原子抢占租约：UPDATE 影响行数=1 才算抢到（多 worker 互斥的根基）。"""
        async with self.session_factory() as db:
            result = await db.execute(
                update(ScheduledTask)
                .where(
                    ScheduledTask.id == task_id,
                    ScheduledTask.enabled == 1,
                    or_(
                        ScheduledTask.lease_expires_at.is_(None),
                        ScheduledTask.lease_expires_at < now,
                    ),
                )
                .values(
                    lease_token=uuid.uuid4().hex,
                    lease_owner=self.owner,
                    lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                    last_run_at=now,
                    run_attempt_count=ScheduledTask.run_attempt_count + 1,
                )
            )
            await db.commit()
            return result.rowcount == 1

    async def _execute(
        self, task_id: int, task_type: str, handler: TaskHandler, task: ScheduledTask
    ) -> None:
        """执行任务并记账：成功清零连续失败，失败 +1（lease 过期后下轮重抢）。"""
        try:
            await handler(self.session_factory, task)
        except Exception:
            logger.exception("调度任务失败 task_id=%d type=%s", task_id, task_type)
            async with self.session_factory() as db:
                await db.execute(
                    update(ScheduledTask)
                    .where(ScheduledTask.id == task_id)
                    .values(
                        consecutive_failure_count=ScheduledTask.consecutive_failure_count + 1
                    )
                )
                await db.commit()
        else:
            async with self.session_factory() as db:
                await db.execute(
                    update(ScheduledTask)
                    .where(ScheduledTask.id == task_id)
                    .values(consecutive_failure_count=0)
                )
                await db.commit()


def _is_due(task: ScheduledTask, now: datetime) -> bool:
    """到期判定：last_run_at 为空（从未跑过）或距今超过任务类型间隔。"""
    interval = TASK_INTERVALS.get(task.task_type)
    if interval is None:
        return False
    if task.last_run_at is None:
        return True
    return (now - task.last_run_at).total_seconds() >= interval


# ---------- 任务 handler：order_sync / delivery_recovery / auto_listing / collect_1688 ----------


async def order_sync_task(session_factory, task: ScheduledTask) -> None:
    """遍历会话池存活账号拉单；账号间 2-5s 随机抖动（protocol.md §1.4）。

    全部账号失败才抛（feed 调度失败计数）；单账号失败记日志继续。
    """
    from app.auth import session_pool
    from app.orders.sync import sync_account_orders

    sessions = list(session_pool.alive_accounts)
    if not sessions:
        logger.info("订单同步跳过：无存活账号")
        return
    failures = 0
    for i, session in enumerate(sessions):
        if i:
            await asyncio.sleep(random.uniform(2.0, 5.0))
        try:
            async with session_factory() as db:
                await sync_account_orders(db, session)
        except Exception:
            failures += 1
            logger.exception("订单同步失败 account_id=%s", session.account_id)
    if failures == len(sessions):
        raise RuntimeError(f"订单同步全部账号失败（{failures}/{len(sessions)}）")


async def delivery_recovery_task(session_factory, task: ScheduledTask) -> None:
    """发货兜底：中间态超时转 unknown（防重发哲学，不回滚卡密）。"""
    from app.fulfillment.delivery import heal_stuck_deliveries

    healed = await heal_stuck_deliveries(session_factory)
    if healed:
        logger.info("发货兜底：%d 条中间态超时记录转 unknown（人工核实）", healed)


async def auto_listing_task(session_factory, task: ScheduledTask) -> None:
    """自动上架：轮询商品池 MAPPED 状态商品，逐个上架（写操作限流 1 次/分钟，账号间抖动）。

    策略：
    - 按账号分组，同一账号内串行上架（写操作限流）
    - 每轮最多处理每账号 5 条（避免单轮阻塞太久）
    - 失败记日志不抛，下轮重试；成功自动进入下一条
    """
    import asyncio
    import random
    from collections import defaultdict

    from sqlalchemy import select

    from app.api_client.errors import ErrorKind, XianyuApiError
    from app.auth.session_pool import session_pool
    from app.core.failure_guard import WriteGuardPaused, risk_silence
    from app.listing.publisher import publish_goods
    from app.models import Goods

    # 1. 查所有 pool_status=MAPPED 且有图片的商品（无图无法上架）
    async with session_factory() as db:
        goods_rows = (
            await db.execute(
                select(Goods).where(
                    Goods.pool_status == "MAPPED",
                    Goods.deleted == 0,
                    Goods.image_urls != None,
                    Goods.image_urls != [],
                )
            )
        ).scalars().all()

    if not goods_rows:
        logger.debug("自动上架：无待上架商品（MAPPED 且有图）")
        return

    # 2. 按账号分组
    by_account: dict[int, list[Goods]] = defaultdict(list)
    for g in goods_rows:
        by_account[g.account_id].append(g)

    # 3. 遍历账号上架（账号间 2-5s 抖动）
    account_ids = list(by_account.keys())
    for i, account_id in enumerate(account_ids):
        if i:
            await asyncio.sleep(random.uniform(2.0, 5.0))

        session = session_pool.get(account_id)
        if session is None or not session.is_alive:
            logger.warning("自动上架跳过：账号 %d 未登录或会话不可用", account_id)
            continue

        # 同账号内串行（写操作限流 1 次/分钟在 api_client 内已限制，这里再限单轮最多 5 条）
        account_goods = by_account[account_id][:5]
        for goods in account_goods:
            try:
                logger.info("自动上架尝试 goods_id=%d title=%s", goods.id, goods.title)
                async with session_factory() as db:
                    # 需要在新事务里重新加载 goods，避免 detached instance
                    fresh_goods = await db.get(Goods, goods.id)
                    if fresh_goods is None:
                        logger.warning("自动上架跳过：商品 %d 已不存在", goods.id)
                        continue
                    if fresh_goods.pool_status != "MAPPED":
                        logger.info("自动上架跳过：商品 %d 状态已变更为 %s", goods.id, fresh_goods.pool_status)
                        continue
                    await publish_goods(db, fresh_goods, session)
                logger.info("自动上架成功 goods_id=%d external_id=%s", goods.id, fresh_goods.external_goods_id)
            except WriteGuardPaused:
                logger.warning("自动上架跳过 goods_id=%d：publish 域熔断中（R-2）", goods.id)
            except XianyuApiError as exc:
                if exc.kind == ErrorKind.LOCAL_THROTTLE:
                    # 自己令牌桶没排到号（1 次/分钟），不是闲鱼拒单，不必铺栈
                    logger.warning("自动上架排队中 goods_id=%d：%s", goods.id, exc)
                else:
                    logger.exception("自动上架失败 goods_id=%d，下轮重试", goods.id)
                if exc.kind == ErrorKind.RISK_CONTROL:
                    await risk_silence()  # R-6：命中风控先随机静默，防下轮循环立刻打回
            except Exception:
                logger.exception("自动上架失败 goods_id=%d，下轮重试", goods.id)
            # 写操作限流已在 MtopClient 内部（1 次/分钟），这里再加人性化抖动
            await asyncio.sleep(random.uniform(10.0, 30.0))


async def collect_1688_task(session_factory, task: ScheduledTask) -> None:
    """定时采集（需求文档 S3 / 主设计 §8.2）：到点自动把一条采集计划跑到「待确认清单」。

    与 `POST /api/collect/run` 调**同一个** `run_collect_pipeline`（N5），产出、逐件记账、
    闸门因此完全一致；同样**不放行、不上架**——链路终点是 DECIDED。这条防线是状态机给的：
    `auto_listing` 只扫 MAPPED，而没经人工放行的采集件最多到 DECIDED（§8.1）。

    与手动入口的两点差别：
    ① 参数来自 `scheduled_task.config_json`。配置缺失/非法只记日志不抛——"还没配"和
       "配了但跑炸了"混在一个计数里，人就分不清该不该去看了；但只要开跑了却没产出清单
       （无可用货源账号 / 检索被拦 / 配额用尽 / 话术没被理解）一律抛错，让
       `consecutive_failure_count` 成为"最近几次到点什么都没备料"的唯一信号。
       Cookie 数小时即失效，这条链路会常态化地卡在 `no_source_account` 上——那是预期，
       不是 bug，正是要让这个计数涨起来的地方。
    ② 带检索配额（N7）：手动入口的闸门是人点的那一下，无人值守没有，只能代码给。

    单副本部署下不必担心跑过租约（LEASE_SECONDS=300）：间隔 1800s ≫ 租约，下一轮到点前
    这一轮早跑完了。真上多副本时要按任务类型给租约时长，那是既定的多副本前置。
    """
    from app.collectors.alibaba.search_quota import search_quota
    from app.collectors.pipeline import CollectRunRequest, run_collect_pipeline

    raw = task.config_json
    if not raw:
        logger.info("定时采集未配置计划，跳过 task_id=%d", task.id)
        return
    try:
        cfg = CollectRunRequest(**raw)
    except (ValidationError, TypeError) as exc:
        logger.error("定时采集配置非法，跳过 task_id=%d: %s", task.id, str(exc)[:200])
        return
    if not (cfg.instruction or cfg.keyword or cfg.offer_ids):
        # PUT /schedule 已挡这一条；走到这里说明行是绕开接口手改的
        logger.warning("定时采集计划没有货源（instruction/keyword/offer_ids 全空），跳过 task_id=%d", task.id)
        return

    async with session_factory() as db:
        result = await run_collect_pipeline(
            db,
            **cfg.pipeline_kwargs(),
            trigger="scheduled",
            search_quota=search_quota,
        )

    status = result.get("status")
    if status != "ok":
        raise RuntimeError(
            f"定时采集未产出清单：{status} "
            f"{result.get('message') or result.get('needs_clarification') or ''}".strip()
        )

    # 逐件记账（N6）：无人值守时这行日志是唯一能看到"哪几件没成"的地方
    c = result["collected"]
    batch_id = result["batch_id"]
    logger.info(
        "定时采集完成 batch=%s 检索=%s 采集 added=%d skipped=%d failed=%d blocked=%d "
        "未尝试=%d%s；决策 %d 件（可放行 %d / 需人工 %d）耗时 %dms",
        batch_id,
        "/".join(r.get("keyword") or "-" for r in result["searched"]["rounds"]) or "-",
        c["added"], c["skipped"], c["failed"], c["blocked"], c["not_attempted"],
        " 已中止" if c["aborted"] else "",
        len(result["decided"]), len(result["reviewable"]), len(result["needs_human"]),
        result["elapsed_ms"],
    )
    for item in result["decided"]:
        if item.get("error"):
            logger.warning("定时采集单件失败 batch=%s offer=%s: %s",
                           batch_id, item["offer_id"], item["error"])
    if not result["reviewable"]:
        logger.warning("定时采集 batch=%s 没有可放行件（全被风险扫描或 manual 拦下，去看清单）", batch_id)


def build_default_runner(session_factory) -> TaskRunner:
    """构造注册了默认任务的 runner（main.py lifespan 用）。"""
    runner = TaskRunner(session_factory)
    runner.register("order_sync", order_sync_task)
    runner.register("delivery_recovery", delivery_recovery_task)
    runner.register("auto_listing", auto_listing_task)
    runner.register(COLLECT_TASK_TYPE, collect_1688_task)
    return runner


async def ensure_default_tasks(session_factory) -> None:
    """启动时缺则插入默认任务行（create_all 只建表不插数据）。幂等。"""
    async with session_factory() as db:
        existing = (
            await db.execute(select(ScheduledTask.task_type))
        ).scalars().all()
        for task_type, interval in TASK_INTERVALS.items():
            if task_type in existing:
                continue
            db.add(
                ScheduledTask(
                    task_type=task_type,
                    # 占位：间隔实际由 TASK_INTERVALS 决定，接 croniter 后才生效
                    cron_expression=f"*/{max(1, interval // 60)} * * * *",
                    # 定时采集默认关：它会无人值守地开浏览器打 1688，得由人配好计划再打开。
                    # 其余三个是自家平台的读/写兜底，开着没有对外部站点的副作用。
                    enabled=0 if task_type == COLLECT_TASK_TYPE else 1,
                )
            )
            logger.info("补种默认调度任务 type=%s", task_type)
        await db.commit()
