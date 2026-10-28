"""FastAPI 应用入口。

- REST + WebSocket 对外接口（Swagger 文档 /docs，前端与 AI Agent 共用同一套 API）；
- lifespan：启动时恢复账号会话池 + 保活调度器（阶段 4-1）；
  为存活账号拉起 WS 消息监听 + AI 回复引擎（阶段 4-3）。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import api_router
from app.auth.refresher import CookieTokenRefresher
from app.core.db import AsyncSessionLocal, engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        async with AsyncSessionLocal() as db:
            from app.users.seed import ensure_seed_admin

            await ensure_seed_admin(db)
    except Exception:
        logger.exception("种子管理员创建失败（数据库未就绪？）")

    # 阶段 4-1 auth：恢复账号会话池 + 启动保活调度
    try:
        async with AsyncSessionLocal() as db:
            from app.auth import load_accounts_into_pool

            await load_accounts_into_pool(db)
    except Exception:
        logger.exception("启动恢复账号会话池失败（数据库未就绪？）")

    refresher = CookieTokenRefresher(AsyncSessionLocal)
    await refresher.start()

    # 阶段 4-3 messaging + ai：WS 监听（按账号）+ AI 回复引擎
    from app.ai.reply_engine import create_reply_engine
    from app.messaging.manager import ws_manager

    reply_engine = create_reply_engine(AsyncSessionLocal)
    reply_engine.start()
    try:
        ws_manager.start_all()
    except Exception:
        logger.exception("WS 监听启动失败（登录态/网络未就绪？稍后由扫码登录触发重试）")

    # 阶段 4-4 fulfillment：自动发货处理器（订阅 ORDER_PAID）
    from app.fulfillment import create_delivery_handler

    delivery_handler = create_delivery_handler(AsyncSessionLocal)
    delivery_handler.start()

    # 阶段 4-5 scheduler：DB 租约调度 worker（订单同步 + 发货兜底 heal）
    from app.scheduler.task_runner import build_default_runner, ensure_default_tasks

    task_runner = None
    task_loop = None
    try:
        await ensure_default_tasks(AsyncSessionLocal)
        task_runner = build_default_runner(AsyncSessionLocal)
        task_loop = asyncio.create_task(task_runner.run())
    except Exception:
        logger.exception("调度 worker 启动失败（数据库未就绪？）")

    yield

    if task_runner is not None:
        task_runner.stop()
    if task_loop is not None:
        # run() 两轮 tick 间 sleep 最长 60s，直接取消不等
        task_loop.cancel()
        try:
            await task_loop
        except asyncio.CancelledError:
            pass
    await refresher.stop()
    try:
        await ws_manager.stop_all()
    except Exception:
        logger.exception("WS 监听停线异常")
    from app.auth import qr_login_service, session_pool
    from app.auth.alibaba_login import alibaba_qr_login_service

    await qr_login_service.aclose()
    await alibaba_qr_login_service.aclose()
    await session_pool.shutdown()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="xianyu-core",
        description="闲鱼自动化系统后端（双品类：虚拟商品自动发货 + 1688 采集上架人工履约）",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    # WS 不挂 /api 前缀（/ws/messages）：与 vite/nginx 的 /ws/ 反代配置一致
    from app.api.routes import ws as ws_routes

    app.include_router(ws_routes.router)

    @app.get("/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
