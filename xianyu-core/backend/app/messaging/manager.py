"""多账号 WS 连接管理（阶段 4-3）。

职责：
- 启动时为会话池所有存活账号拉起 WS 监听；扫码登录成功后拉起新账号；
- COOKIE_EXPIRED 事件 → 停线（等人工重登后由会话池恢复再拉起）；
- 进程内存版（单副本部署假设，同 auth 会话池）。
"""

import asyncio
import logging

from app.auth.session_pool import AccountSession, session_pool
from app.messaging.events import Event, EventType, event_bus
from app.messaging.ws_client import XianyuWebSocketClient

logger = logging.getLogger(__name__)


class WsManager:
    def __init__(self, bus=event_bus):
        self.bus = bus
        self._tasks: dict[int, asyncio.Task] = {}
        self._clients: dict[int, XianyuWebSocketClient] = {}
        self._subscribed = False

    # ---------- 生命周期 ----------

    def start_all(self) -> None:
        """为会话池当前所有存活账号拉起 WS 监听（lifespan 启动时调用）。"""
        for session in session_pool.alive_accounts:
            self.start_account(session)
        self._ensure_subscribed()

    def start_account(self, session: AccountSession) -> None:
        """拉起单账号 WS（幂等：已在跑则跳过）。"""
        if session.account_id in self._tasks:
            return
        if session.mtop is None:
            logger.warning("账号无 mtop 客户端，跳过 WS 启动 accountId=%d", session.account_id)
            return
        client = XianyuWebSocketClient(
            account_id=session.account_id,
            external_uid=session.external_uid,
            cookie=session.cookie,
            mtop=session.mtop,
            bus=self.bus,
        )
        self._clients[session.account_id] = client
        self._tasks[session.account_id] = asyncio.create_task(
            self._run_account(session.account_id, client), name=f"ws-{session.account_id}"
        )
        logger.info("WS 监听已启动 accountId=%d", session.account_id)
        self._ensure_subscribed()

    async def _run_account(self, account_id: int, client: XianyuWebSocketClient) -> None:
        try:
            await client.run()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("WS 任务异常退出 accountId=%d", account_id)
        finally:
            self._tasks.pop(account_id, None)
            self._clients.pop(account_id, None)

    async def stop_account(self, account_id: int) -> None:
        client = self._clients.get(account_id)
        if client:
            await client.stop()
        task = self._tasks.get(account_id)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def stop_all(self) -> None:
        for account_id in list(self._tasks):
            await self.stop_account(account_id)

    def get_client(self, account_id: int) -> XianyuWebSocketClient | None:
        return self._clients.get(account_id)

    # ---------- 事件联动 ----------

    def _ensure_subscribed(self) -> None:
        if self._subscribed:
            return
        self._subscribed = True
        self.bus.subscribe(EventType.COOKIE_EXPIRED, self._on_cookie_expired)

    async def _on_cookie_expired(self, event: Event) -> None:
        logger.info("登录态失效，停线 WS accountId=%d", event.account_id)
        await self.stop_account(event.account_id)


# 全局单例（lifespan 中随应用起停）
ws_manager = WsManager()
