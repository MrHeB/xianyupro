"""IM accessToken 获取与缓存（protocol.md §7.3，蓝本：goofish-cli token.py）。

- WS /reg 用的 token 来自 mtop `mtop.taobao.idlemessage.pc.login.token`，
  不同于 HTTP 签名用的 _m_h5_tk，二者不可混用；
- 该接口风控敏感（高频调用会 RGV587），所以内存缓存 30min（真实过期约 2h）。
"""

import logging
import time

from app.api_client.client import MtopClient
from app.api_client.errors import XianyuApiError
from app.core.config import settings
from app.messaging.frames import IM_APP_KEY

logger = logging.getLogger(__name__)

TOKEN_API = "mtop.taobao.idlemessage.pc.login.token"
LOGIN_USER_API = "mtop.taobao.idlemessage.pc.loginuser.get"


class ImTokenManager:
    """按账号缓存 accessToken（内存版；多副本部署时迁 Redis）。"""

    def __init__(self, ttl_seconds: float | None = None):
        self._ttl = ttl_seconds or settings.im_token_ttl_seconds
        # (account_id, device_id) -> (token, expire_at)
        self._cache: dict[tuple[int, str], tuple[str, float]] = {}

    def invalidate(self, account_id: int, device_id: str) -> None:
        self._cache.pop((account_id, device_id), None)

    async def get_token(
        self, mtop: MtopClient, account_id: int, device_id: str, *, force_refresh: bool = False
    ) -> str:
        """取 WS accessToken：缓存优先，过期/强制刷新时调 mtop。失败抛 XianyuApiError。"""
        key = (account_id, device_id)
        now = time.time()
        if not force_refresh:
            cached = self._cache.get(key)
            if cached and now < cached[1]:
                return cached[0]

        data = await mtop.call(TOKEN_API, "1.0", {"appKey": IM_APP_KEY, "deviceId": device_id})
        token = (data or {}).get("accessToken") or ""
        if not token:
            raise XianyuApiError("token", f"accessToken 获取失败: {data}")
        self._cache[key] = (token, now + self._ttl)
        logger.info("IM accessToken 获取成功 accountId=%d（缓存 %ds）", account_id, self._ttl)
        return token

    async def refresh_login(self, mtop: MtopClient) -> None:
        """WS 长连常驻时的轻量保活（600s 一次）。异常只记日志。"""
        try:
            await mtop.call(LOGIN_USER_API, "1.0", {})
        except Exception:
            logger.warning("loginuser.get 保活失败（下轮重试）")


# 全局单例
im_token_manager = ImTokenManager()
