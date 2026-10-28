"""路由聚合。前端与 AI Agent 共用同一套 API（Swagger：/docs）。

ws 路由不挂 /api 前缀（暴露在 /ws/messages）：
vite / nginx 均按 /ws/ 反代并带升级头，与 REST 的 /api/ 分开配置。
"""

from fastapi import APIRouter

from app.api.routes import (
    accounts,
    alibaba,
    auth,
    collect,
    delivery,
    goods,
    messages,
    orders,
    settings,
    stats,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(accounts.router)
api_router.include_router(alibaba.router)
api_router.include_router(stats.router)
api_router.include_router(goods.router)
api_router.include_router(collect.router)
api_router.include_router(messages.router)
api_router.include_router(delivery.router)
api_router.include_router(orders.router)
api_router.include_router(settings.router)
