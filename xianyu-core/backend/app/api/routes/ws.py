"""WebSocket 接口：订阅事件总线，把新消息实时推给前端。"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.messaging.events import Event, EventType, event_bus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])

FRONTEND_EVENTS = (
    EventType.MESSAGE_RECEIVED,
    EventType.ORDER_PAID,
    EventType.ORDER_CREATED,  # 品类 B 人工履约通知（阶段 4-4）
    EventType.COOKIE_EXPIRED,
)


def _to_frontend(event: Event) -> dict:
    payload = {k: v for k, v in (event.payload or {}).items() if k != "rawPayload"}
    return {
        "type": event.type.value,
        "account_id": event.account_id,
        "occurred_at": event.occurred_at.isoformat(),
        "payload": payload,
    }


@router.websocket("/ws/messages")
async def ws_messages(ws: WebSocket) -> None:
    """订阅事件总线 → 推送前端（MESSAGE_RECEIVED / ORDER_PAID / COOKIE_EXPIRED）。

    payload 里剥掉 rawPayload（可能很大），前端只拿规范化字段。
    """
    await ws.accept()

    def _subscribe() -> list:
        handlers = []

        async def _forward(event: Event) -> None:
            try:
                await ws.send_json(_to_frontend(event))
            except Exception:
                pass  # 连接已断；由心跳/写失败自然回收

        for event_type in FRONTEND_EVENTS:
            event_bus.subscribe(event_type, _forward)
            handlers.append((event_type, _forward))
        return handlers

    # 订阅必须与推送互斥：先注册 handler，再起心跳；断开时无法反注册（骨架版总线无
    # unsubscribe），handler 内部的 send 失败会被吞掉，随前端重连累积的空 handler
    # 只做空转——TODO(生产化)：EventBus 补 unsubscribe。
    _subscribe()
    try:
        while True:
            await ws.send_json({"type": "heartbeat"})
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("前端 WS 连接异常关闭")
