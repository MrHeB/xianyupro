"""统一会话池：多账号登录态的内存侧管理（阶段 4-1 移植，验收=多账号稳定登录、掉线自动重登）。

职责（参考 xianyu-pilot-pro 三轨保活）：
- 每账号持有一个会话对象（Cookie + MtopClient + WS 客户端）；
- Cookie 保活 30min（hasLogin.do）/ _m_h5_tk 1.5-2.5h / ws_token 10-14h 三轨刷新；
- 掉线自动重登；RGV587 不误标失效（只入滑块/人工流程）。

注意：进程内存版（单副本部署假设）。多副本部署时需把扫码会话/状态迁 Redis。
"""

import logging
from dataclasses import dataclass, field
from typing import Callable

from app.api_client import MtopClient

logger = logging.getLogger(__name__)


@dataclass
class AccountSession:
    """单账号运行时会话。"""

    account_id: int
    external_uid: str
    # 内存中持有明文 Cookie（DB 存的是 enc:v1 密文）
    cookie: str = ""
    mtop: MtopClient | None = None
    # ws_client 由 messaging 模块在登录后挂载
    ws_client: object | None = None
    extra: dict = field(default_factory=dict)

    @property
    def is_alive(self) -> bool:
        # 判据：unb + _m_h5_tk 三件套（protocol.md §2）
        return "unb=" in self.cookie and "_m_h5_tk=" in self.cookie and "cookie2=" in self.cookie


class SessionPool:
    """全进程唯一会话池。"""

    def __init__(self) -> None:
        self._sessions: dict[int, AccountSession] = {}

    def register(self, account_id: int, external_uid: str, cookie: str) -> AccountSession:
        """账号登录成功后注册（或刷新）会话。"""
        session = self._sessions.get(account_id)
        if session is None:
            session = AccountSession(account_id=account_id, external_uid=external_uid)
            self._sessions[account_id] = session
        session.external_uid = external_uid
        session.cookie = cookie
        session.mtop = MtopClient(
            cookie_provider=lambda: session.cookie,
            on_cookie_update=lambda c: setattr(session, "cookie", c),
        )
        return session

    def get(self, account_id: int) -> AccountSession | None:
        return self._sessions.get(account_id)

    async def remove(self, account_id: int) -> None:
        """运行时下线：移出会话池并关闭其 mtop 客户端（调用方需先停该账号 WS）。

        幂等——账号本就不在池里也不报错。停用/解绑走这里，重启后 load_accounts_into_pool
        按 status==1 过滤，已停用账号不会再被灌回来。
        """
        session = self._sessions.pop(account_id, None)
        if session is not None and session.mtop is not None:
            try:
                await session.mtop.aclose()
            except Exception:
                logger.warning("关闭账号 mtop 客户端失败 accountId=%d", account_id, exc_info=True)

    @property
    def alive_accounts(self) -> list[AccountSession]:
        return [s for s in self._sessions.values() if s.is_alive]

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            if session.mtop is not None:
                await session.mtop.aclose()
        self._sessions.clear()


# 全局单例（FastAPI lifespan 中初始化/销毁）
session_pool = SessionPool()
