"""闲鱼 IM WebSocket 客户端（protocol.md §7，蓝本：goofish-cli ws.py + pro ws_client.py）。

单账号一条长连接，职责：
- 连接（Cookie + Origin 握手头）→ /reg 注册（IM accessToken）→ ackDiff 短轮询；
- 心跳 /! 15s；600s 一次 loginuser.get 轻量保活；
- 收包**先 ACK 再处理**；/s/vulcan 等推送包 → 解码（decoder）→ 事件总线；
- 付款消息（payment 判定 + 节流）→ ORDER_PAID 事件（品类 A 发货触发源）；
- 断线指数退避重连（5→300s），重连后重新取 token 重新注册；
- send_text：发帧并等待 mid 配对回包（code=200 才算发出成功）。
"""

import asyncio
import json
import logging
from typing import Any

import websockets

from app.core.config import settings
from app.messaging import frames
from app.messaging.decoder import decode_push_payloads, parse_message
from app.messaging.events import Event, EventType, event_bus
from app.messaging.frames import WS_HOST
from app.messaging.payment import PaymentThrottle, is_payment_message
from app.messaging.token import im_token_manager

logger = logging.getLogger(__name__)

SYNC_LWPS = ("/s/para", "/s/sync", "/s/vulcan")
# 静默断开看门狗：超过该时长没收到任何帧就强制重连（心跳无回包，靠这个兜底）
RECV_WATCHDOG_SECONDS = 90.0


class XianyuWebSocketClient:
    """单账号 WS 长连接。由 WsManager 持有，run() 阻塞至账号下线。"""

    def __init__(self, account_id: int, external_uid: str, cookie: str, mtop, bus=event_bus):
        self.account_id = account_id
        self.external_uid = external_uid
        self._cookie_provider = (lambda: cookie) if isinstance(cookie, str) else cookie
        self._mtop = mtop  # MtopClient（IM token 获取用）
        self.bus = bus
        self.device_id = frames.build_device_id(external_uid)
        self._payment_throttle = PaymentThrottle()
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}  # mid -> Future（发送回包配对）
        self._pending_ack_diff_mid: str | None = None
        self._stopping = False

    # ==================== 主循环 ====================

    async def run(self) -> None:
        """常驻：连接 → 注册 → 收包循环；断线指数退避重连（5→300s）。"""
        backoff = settings.ws_reconnect_min_seconds
        while not self._stopping:
            try:
                await self._run_once()
                if self._stopping:
                    break
                logger.info("WS 正常退出 accountId=%d，3s 后重连", self.account_id)
                await asyncio.sleep(3)
                backoff = settings.ws_reconnect_min_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopping:
                    break
                logger.warning(
                    "WS 断连 accountId=%d：%s；%.0fs 后重连", self.account_id, exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, settings.ws_reconnect_max_seconds)

    async def _run_once(self) -> None:
        token = await im_token_manager.get_token(self._mtop, self.account_id, self.device_id)
        cookie = self._cookie_provider()

        self._ws = await websockets.connect(
            settings.ws_url,
            additional_headers={
                "Cookie": cookie,
                "Host": WS_HOST,
                "User-Agent": frames.UA_WEB,
                "Origin": "https://www.goofish.com",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            ping_interval=None,  # 禁用 WS ping，走 LWP /! 心跳
            max_size=4 * 1024 * 1024,
        )
        logger.info("WS 已连接 accountId=%d", self.account_id)

        await self._send(frames.build_reg_frame(token, self.device_id))
        await self._send_ack_diff()

        heartbeat = asyncio.create_task(self._heartbeat_loop())
        refresh = asyncio.create_task(self._refresh_loop())
        try:
            await self._recv_loop()
        finally:
            heartbeat.cancel()
            refresh.cancel()
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()
            self._pending.clear()
            self._pending_ack_diff_mid = None
            self._ws = None
            await self._safe_close()

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        watchdog = asyncio.get_event_loop().time() + RECV_WATCHDOG_SECONDS
        while True:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=RECV_WATCHDOG_SECONDS)
            except TimeoutError:
                logger.warning("WS 静默超时（%ds 无帧），强制重连 accountId=%d",
                               RECV_WATCHDOG_SECONDS, self.account_id)
                raise
            watchdog = asyncio.get_event_loop().time()
            try:
                frame = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(frame, dict):
                continue

            # 先 ACK 再处理（protocol.md §7.4）
            try:
                await self._send(frames.build_ack_frame(frame))
            except Exception:
                logger.debug("ACK 发送失败（连接可能已断）accountId=%d", self.account_id)

            await self._handle_frame(frame)

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        lwp = frame.get("lwp")

        # 推送包：解码全部消息并扇出，然后发下一轮 ackDiff（短轮询）
        if lwp in SYNC_LWPS:
            try:
                await self._process_push(frame)
            except Exception:
                logger.exception("推送包处理异常 accountId=%d", self.account_id)
            await self._send_ack_diff()
            return

        # ackDiff 空响应（无 lwp，仅 code+headers，mid 配对）→ 发下一轮
        mid = (frame.get("headers") or {}).get("mid")
        if mid and not lwp:
            fut = self._pending.pop(mid, None)
            if fut and not fut.done():
                fut.set_result(frame)
            if mid == self._pending_ack_diff_mid:
                self._pending_ack_diff_mid = None
                await self._send_ack_diff()

    # ==================== 推送包处理 ====================

    async def _process_push(self, frame: dict[str, Any]) -> None:
        body = frame.get("body") or {}
        sync_push = body.get("syncPushPackage") or {}
        data_list = sync_push.get("data") or []
        for item in data_list:
            if not isinstance(item, dict):
                continue
            for decoded in decode_push_payloads(item.get("data", "")):
                parsed = parse_message(decoded)
                if not parsed:
                    continue
                await self._emit_message(parsed, raw=decoded)

    async def _emit_message(self, parsed: dict, raw: dict) -> None:
        payload = {**parsed, "rawPayload": raw}
        await self.bus.publish(
            Event(type=EventType.MESSAGE_RECEIVED, account_id=self.account_id, payload=payload)
        )
        # 付款事件：品类 A 自动发货触发源（节流 + DB 幂等双兜底）
        judgement = is_payment_message(parsed)
        if judgement.is_payment:
            pnm_id = str(parsed.get("pnmId") or "")
            if self._payment_throttle.should_emit(self.account_id, pnm_id):
                await self.bus.publish(
                    Event(type=EventType.ORDER_PAID, account_id=self.account_id, payload=payload)
                )
            else:
                logger.info("付款推送节流命中 accountId=%d pnmId=%s", self.account_id, pnm_id)

    # ==================== 发送 ====================

    async def send_text(self, peer_id: str, text: str) -> bool:
        """给对端发文本。peer_id 是对端用户 ID（cid 用对端 ID 即可，单聊 cid=toid 场景已验证）。

        actualReceivers 必须包含发送者自己（protocol.md §7.5）。
        返回是否收到 code=200 回包。
        """
        return await self._send_and_wait(
            frames.build_send_text_frame(self.external_uid, peer_id, peer_id, text)
        )

    async def send_image(self, peer_id: str, url: str) -> bool:
        return await self._send_and_wait(
            frames.build_send_image_frame(self.external_uid, peer_id, peer_id, url)
        )

    async def _send_and_wait(self, frame: dict[str, Any], timeout: float = 15.0) -> bool:
        if self._ws is None:
            logger.warning("WS 未连接，发送被拒绝 accountId=%d", self.account_id)
            return False
        mid = frame["headers"]["mid"]
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._send(frame)
            resp = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            logger.warning("发送回包超时 mid=%s accountId=%d", mid, self.account_id)
            return False
        finally:
            self._pending.pop(mid, None)

        result = frames.parse_send_response(resp)
        if result.get("code") != 200:
            logger.warning(
                "发送失败 code=%s error=%s accountId=%d",
                result.get("code"), result.get("error"), self.account_id,
            )
            return False
        return True

    # ==================== 心跳与保活 ====================

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.ws_heartbeat_interval)
            try:
                await self._send(frames.build_heartbeat_frame())
            except Exception as exc:
                logger.debug("心跳发送失败（连接可能已断）：%s", exc)
                return

    async def _refresh_loop(self) -> None:
        """600s 一次 loginuser.get 轻量保活（蓝本 goofish-cli run_forever）。"""
        while True:
            await asyncio.sleep(settings.ws_refresh_interval)
            await im_token_manager.refresh_login(self._mtop)

    # ==================== 底层收发 ====================

    async def _send(self, frame: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("WS 未连接")
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def _send_ack_diff(self) -> None:
        frame = frames.build_ack_diff()
        self._pending_ack_diff_mid = frame["headers"]["mid"]
        await self._send(frame)

    async def _safe_close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def stop(self) -> None:
        """请求停线（WsManager 调用）：让 run() 主循环退出。"""
        self._stopping = True
        await self._safe_close()
