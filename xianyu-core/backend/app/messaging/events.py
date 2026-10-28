"""进程内事件总线：消息监听 → AI 回复 / 自动发货 都从这里扇出。

设计（借鉴 xianyu-pilot outbox 可靠扇出思想，骨架阶段先做内存版）：
- 阶段 4-3：WS 新消息 → MESSAGE_RECEIVED 事件 → AI 回复引擎订阅；
- 阶段 4-4：付款消息 → ORDER_PAID 事件 → 发货处理器订阅；
- TODO(阶段4+)：生产化时改为「同一事务写 outbox 双分支 + worker lease 消费」，
  保证消息不丢（进程崩溃时内存队列即丢）。
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    MESSAGE_RECEIVED = "message_received"   # 新消息（含文本/图片）
    ORDER_PAID = "order_paid"               # 买家已付款（触发品类 A 自动发货）
    ORDER_CREATED = "order_created"          # 新订单（品类 B 仅推送人工通知）
    COOKIE_EXPIRED = "cookie_expired"        # 登录态失效（触发重登）


@dataclass
class Event:
    type: EventType
    account_id: int
    payload: dict = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=datetime.now)


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    """进程内发布/订阅。多副本部署时需换 Redis Stream。"""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        for handler in self._handlers.get(event.type, []):
            # 每个处理器独立任务，互不阻塞；异常只记日志不断总线
            asyncio.create_task(self._safe_run(handler, event))

    async def _safe_run(self, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception("事件处理器异常 type=%s account=%s", event.type, event.account_id)


# 全局单例
event_bus = EventBus()
