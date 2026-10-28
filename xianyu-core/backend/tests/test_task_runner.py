"""调度 worker 测试：DB 租约抢占 + 到期判定 + 执行记账。

覆盖：ensure_default_tasks 补种幂等（含"定时采集默认关"）、租约互斥（他 worker 持有未过期
租约抢不到）、到期判定（last_run_at 为空/超期跑、间隔内不跑）、handler 收得到自己的任务行
（config_json 是定时采集的参数来源）、执行成功清零连续失败、失败 +1（lease 过期后下轮重抢）、
delivery_recovery 接线 heal。collect_1688 handler 本体见 test_collect_schedule_s3.py。
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, ScheduledTask
from app.scheduler.task_runner import (
    COLLECT_TASK_TYPE,
    TASK_INTERVALS,
    TaskRunner,
    _is_due,
    delivery_recovery_task,
    ensure_default_tasks,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _get_task(session_factory, task_type: str) -> ScheduledTask | None:
    async with session_factory() as db:
        return (
            await db.execute(
                select(ScheduledTask).where(ScheduledTask.task_type == task_type)
            )
        ).scalar_one_or_none()


@pytest.mark.asyncio
async def test_ensure_default_tasks_seeds_and_idempotent(session_factory):
    """补种默认任务行；重复调用不重复插。"""
    await ensure_default_tasks(session_factory)
    await ensure_default_tasks(session_factory)
    async with session_factory() as db:
        types = (await db.execute(select(ScheduledTask.task_type))).scalars().all()
    assert sorted(types) == sorted(TASK_INTERVALS)
    task = await _get_task(session_factory, "order_sync")
    assert task is not None and task.enabled == 1
    # 定时采集是唯一"默认关"的：它无人值守地开浏览器打外部站点，得先有人配计划
    collect = await _get_task(session_factory, COLLECT_TASK_TYPE)
    assert collect is not None and collect.enabled == 0 and collect.config_json is None


@pytest.mark.asyncio
async def test_tick_passes_task_row_to_handler(session_factory):
    """handler 拿得到自己的任务行——config_json 是定时采集的唯一参数来源。"""
    await ensure_default_tasks(session_factory)
    seen: list = []

    async def handler(sf, task):
        seen.append(task)

    runner = TaskRunner(session_factory)
    runner.register("order_sync", handler)
    await runner._tick()

    assert len(seen) == 1 and seen[0].task_type == "order_sync"
    assert seen[0].config_json is None


def test_is_due_rules():
    now = datetime.now()
    never_run = ScheduledTask(task_type="order_sync", cron_expression="*/10 * * * *")
    assert _is_due(never_run, now) is True

    just_ran = ScheduledTask(
        task_type="order_sync", cron_expression="*/10 * * * *", last_run_at=now
    )
    assert _is_due(just_ran, now) is False
    assert _is_due(just_ran, now + timedelta(seconds=TASK_INTERVALS["order_sync"])) is True

    unknown_type = ScheduledTask(task_type="nope", cron_expression="*")
    assert _is_due(unknown_type, now) is False


@pytest.mark.asyncio
async def test_tick_runs_due_task_and_updates_bookkeeping(session_factory):
    """到期任务执行 handler，last_run_at 落库、连续失败清零。"""
    await ensure_default_tasks(session_factory)
    ran: list[str] = []

    async def handler(sf, task):
        ran.append("delivery_recovery")

    async with session_factory() as db:
        await db.execute(
            update(ScheduledTask)
            .where(ScheduledTask.task_type == "delivery_recovery")
            .values(consecutive_failure_count=3, last_run_at=None)
        )
        await db.commit()

    runner = TaskRunner(session_factory)
    runner.register("delivery_recovery", handler)
    await runner._tick()

    assert ran == ["delivery_recovery"]
    task = await _get_task(session_factory, "delivery_recovery")
    assert task.last_run_at is not None
    assert task.consecutive_failure_count == 0
    assert task.lease_owner == runner.owner
    assert task.run_attempt_count == 1

    # 间隔内不重跑
    await runner._tick()
    assert ran == ["delivery_recovery"]
    assert (await _get_task(session_factory, "delivery_recovery")).run_attempt_count == 1


@pytest.mark.asyncio
async def test_tick_lease_mutex_blocks_other_worker(session_factory):
    """租约互斥：他 worker 持有未过期租约（last_run_at 已久远、due）也抢不到。"""
    await ensure_default_tasks(session_factory)
    now = datetime.now()
    async with session_factory() as db:
        # last_run_at 很久以前 → due；lease_expires_at 在未来 → 被别的 worker 持有
        await db.execute(
            update(ScheduledTask)
            .where(ScheduledTask.task_type == "order_sync")
            .values(
                last_run_at=now - timedelta(hours=1),
                lease_expires_at=now + timedelta(seconds=120),
                lease_owner="worker-other",
            )
        )
        await db.commit()

    ran: list[str] = []

    async def handler(sf, task):
        ran.append("order_sync")

    runner = TaskRunner(session_factory)
    runner.register("order_sync", handler)
    await runner._tick()
    assert ran == []  # 抢不到不执行

    # 租约过期后能抢到
    async with session_factory() as db:
        await db.execute(
            update(ScheduledTask)
            .where(ScheduledTask.task_type == "order_sync")
            .values(lease_expires_at=now - timedelta(seconds=1))
        )
        await db.commit()
    await runner._tick()
    assert ran == ["order_sync"]


@pytest.mark.asyncio
async def test_tick_failure_increments_consecutive_count(session_factory):
    """handler 失败 → consecutive_failure_count +1；下轮（仍 due 由 lease 过期后）重试。"""
    await ensure_default_tasks(session_factory)

    async def boom(sf, task):
        raise RuntimeError("任务炸了")

    runner = TaskRunner(session_factory)
    runner.register("order_sync", boom)
    await runner._tick()

    task = await _get_task(session_factory, "order_sync")
    assert task.consecutive_failure_count == 1
    assert task.last_run_at is not None  # 抢占时即记账

    # lease 过期 + last_run_at 已超间隔后可重试
    async with session_factory() as db:
        await db.execute(
            update(ScheduledTask)
            .where(ScheduledTask.task_type == "order_sync")
            .values(
                lease_expires_at=datetime.now() - timedelta(seconds=1),
                last_run_at=datetime.now() - timedelta(seconds=TASK_INTERVALS["order_sync"]),
            )
        )
        await db.commit()

    async def ok(sf, task):
        pass

    runner2 = TaskRunner(session_factory)
    runner2.register("order_sync", ok)
    await runner2._tick()
    task = await _get_task(session_factory, "order_sync")
    assert task.consecutive_failure_count == 0


@pytest.mark.asyncio
async def test_delivery_recovery_task_wires_heal(session_factory, monkeypatch):
    """delivery_recovery handler 调 heal_stuck_deliveries。"""
    called: list = []

    async def fake_heal(sf, *, stuck_minutes=30):
        called.append((sf is session_factory, stuck_minutes))
        return 2

    # handler 内是函数内 import，patch 源头模块即可生效
    monkeypatch.setattr("app.fulfillment.delivery.heal_stuck_deliveries", fake_heal)
    await delivery_recovery_task(session_factory, None)
    assert called == [(True, 30)]
