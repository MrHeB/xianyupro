"""AI 自动回复引擎（阶段 4-3，验收：监听消息 → LLM 生成回复 → 发出）。

事件流（全部挂事件总线，与 WS 客户端解耦）：
MESSAGE_RECEIVED →
  ① 入库（conversation upsert + chat_message，pnm_id 幂等去重）；
  ② 过滤：自发消息 / 非文本 / 超龄旧消息不回；
  ③ 准入策略（reply_policy 完整判定序）；
  ④ 拼 prompt（全局提示词打底 + 该账号人设 + 商品上下文 + 近 N 条历史）→ LLM 生成；
  ⑤ WS 发出 → OUT 消息入库（is_auto_reply=1）+ 会话状态机更新。

注入点（全部可替换，便于单测）：session_factory / provider / sender / uid_lookup。
"""

import logging
import time
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.ai.reply_policy import PolicyContext, ReplyMode, evaluate_reply_policy
from app.core.config import settings
from app.messaging.decoder import is_synthetic_peer_name
from app.messaging.events import Event, EventType, event_bus
from app.models import ChatMessage, Conversation, Goods

logger = logging.getLogger(__name__)


class ReplyEngine:
    def __init__(
        self,
        session_factory,
        *,
        provider=None,
        sender=None,
        uid_lookup=None,
        bus=None,
    ):
        self._session_factory = session_factory
        self._provider = provider
        self._sender = sender
        self._uid_lookup = uid_lookup
        self.bus = bus or event_bus
        self._subscribed = False

    # ==================== 生命周期 ====================

    def start(self) -> None:
        if self._subscribed:
            return
        self._subscribed = True
        self.bus.subscribe(EventType.MESSAGE_RECEIVED, self.handle_message)
        logger.info("AI 回复引擎已启动（mode=%s）", settings.ai_reply_mode)

    # ==================== 主流程 ====================

    async def handle_message(self, event: Event) -> None:
        payload = event.payload or {}
        account_id = event.account_id
        own_uid = self._lookup_uid(account_id)
        sender_uid = str(payload.get("senderUserId") or "")

        # 自发消息（含手机端人工回复的回声）不入库不回复——
        # OUT 侧在发送时落库，人工回复的暂停联动见 TODO（流程方案 4.3）
        if own_uid and sender_uid == own_uid:
            return

        peer_id = sender_uid or str(payload.get("sId") or "")
        if not peer_id:
            return
        peer_name = str(payload.get("senderUserName") or "")
        if is_synthetic_peer_name(peer_name):
            peer_name = ""

        content_type = int(payload.get("contentType") or 0)
        text = str(payload.get("msgContent") or "")

        async with self._session_factory() as db:
            # pnm_id 幂等：WS 周期推送会重复，重复直接跳过
            pnm_id = str(payload.get("pnmId") or "")
            if pnm_id:
                dup = (
                    await db.execute(select(ChatMessage.id).where(ChatMessage.pnm_id == pnm_id))
                ).scalar_one_or_none()
                if dup is not None:
                    return

            conversation = await self._get_or_create_conversation(
                db, account_id, peer_id, peer_name or None
            )
            if not pnm_id:
                # 无 pnmId 的同步消息（每次 WS 重连全量回放）没有天然幂等键：
                # 有文本按 账号+会话+类型+内容 去重；空内容（系统卡片等）无文本指纹，
                # 退而用 会话+类型+消息时间 判定（回放时 messageTime 稳定不变）
                fp = [
                    ChatMessage.account_id == account_id,
                    ChatMessage.conversation_id == conversation.id,
                    ChatMessage.direction == "IN",
                    ChatMessage.content_type == content_type,
                ]
                if text:
                    fp.append(ChatMessage.content == text)
                else:
                    fp.append(ChatMessage.content == "")
                    fp.append(
                        ChatMessage.message_time
                        == (
                            int(payload.get("messageTime") or 0)
                            or int(time.time() * 1000)
                        )
                    )
                dup = (
                    await db.execute(select(ChatMessage.id).where(*fp).limit(1))
                ).scalar_one_or_none()
                if dup is not None:
                    return
                pnm_id = f"gen-{uuid.uuid4().hex}"
            db.add(
                ChatMessage(
                    account_id=account_id,
                    conversation_id=conversation.id,
                    pnm_id=pnm_id,
                    content_type=content_type,
                    direction="IN",
                    content=text,
                    message_time=int(payload.get("messageTime") or 0) or int(time.time() * 1000),
                    raw_payload=_compact_raw(payload),
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                # 平台重复推送并发穿过上面的 pnm_id 查重：撞唯一键的一方按幂等跳过，
                # 回复交给先提交成功的那个任务，不在此重试
                await db.rollback()
                return

        # 只对文本消息自动回复；超龄旧消息不回（防首次连接全量同步回一堆历史）
        if content_type != 1 or not text.strip():
            return

        # 运行时配置（DB 覆盖 .env，见 app/settings）：每次回复读一次，改配置免重启生效
        cfg = await self._load_cfg(account_id)
        if not self._is_fresh(payload, cfg):
            logger.debug("旧消息不回复 accountId=%d pnmId=%s", account_id, pnm_id)
            return

        decision = await self._evaluate(account_id, conversation.id, text, cfg)
        if not decision.should_reply:
            logger.info("不自动回复 accountId=%d reason=%s", account_id, decision.reason)
            return

        reply = await self._generate(account_id, conversation.id, peer_id, payload, cfg)
        if not reply:
            return

        sent = await self._send(account_id, peer_id, reply)
        if not sent:
            logger.warning("自动回复发送失败 accountId=%d peer=%s", account_id, peer_id)
            return

        await self._store_outbound(account_id, conversation.id, reply)
        logger.info("自动回复已发出 accountId=%d peer=%s len=%d", account_id, peer_id, len(reply))

    # ==================== 子步骤 ====================

    def _lookup_uid(self, account_id: int) -> str:
        if self._uid_lookup is not None:
            return self._uid_lookup(account_id)
        from app.auth.session_pool import session_pool

        session = session_pool.get(account_id)
        return session.external_uid if session else ""

    async def _load_cfg(self, account_id: int):
        """读运行时 LLM/回复策略配置（DB 覆盖 .env）+ 该账号人设。"""
        from app.settings import get_llm_config_for_account

        async with self._session_factory() as db:
            return await get_llm_config_for_account(db, account_id)

    def _is_fresh(self, payload: dict, cfg) -> bool:
        message_time = int(payload.get("messageTime") or 0)
        if message_time <= 0:
            return True  # 无时间戳按新消息处理
        return (time.time() * 1000 - message_time) < cfg.max_message_age_seconds * 1000

    async def _evaluate(self, account_id: int, conversation_id: int, text: str, cfg):
        async with self._session_factory() as db:
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.id == conversation_id)
                )
            ).scalar_one()
            daily_count = (
                await db.execute(
                    select(func.count(ChatMessage.id))
                    .join(Conversation, Conversation.id == ChatMessage.conversation_id)
                    .where(
                        Conversation.account_id == account_id,
                        ChatMessage.is_auto_reply == 1,
                        ChatMessage.created_at >= datetime.now().replace(
                            hour=0, minute=0, second=0, microsecond=0
                        ),
                    )
                )
            ).scalar_one()

        keywords = cfg.blocked_keyword_list
        ctx = PolicyContext(
            mode=ReplyMode(cfg.reply_mode),
            daily_reply_count=daily_count,
            max_daily_replies=cfg.max_daily_replies,
            last_manual_reply_at=conv.last_manual_reply_at,
            last_auto_reply_at=conv.last_auto_reply_at,
            conversation_paused=bool(conv.auto_reply_paused or conv.manual_disabled),
            blocked_keywords=keywords,
            message_text=text,
            work_hour_start=cfg.work_hour_start,
            work_hour_end=cfg.work_hour_end,
        )
        return evaluate_reply_policy(ctx, datetime.now())

    async def _generate(
        self, account_id: int, conversation_id: int, peer_id: str, payload: dict, cfg
    ) -> str:
        """拼 prompt 调 LLM。失败返回空串（降级为不回复）。"""
        provider = self._provider or self._default_provider(cfg)

        system = cfg.system_prompt
        goods_line = await self._goods_context(account_id, str(payload.get("xyGoodsId") or ""))
        if goods_line:
            system += f"\n\n当前咨询的商品：{goods_line}"

        history = await self._recent_history(conversation_id, cfg)
        messages = [{"role": "system", "content": system}, *history]
        if not history or history[-1]["role"] != "user":
            messages.append({"role": "user", "content": str(payload.get("msgContent") or "")})

        # 推理模型（deepseek-flash 等）的思维链同样计入 max_tokens，256 会把正文截断
        result = await provider.chat(messages, temperature=0.6, max_tokens=1024)
        if not result.ok:
            logger.warning("LLM 生成失败（降级不回复）：%s", result.error)
            return ""
        return result.text.strip()[:500]

    @staticmethod
    def _default_provider(cfg):
        from app.ai.provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            api_key=cfg.api_key, base_url=cfg.base_url, model=cfg.model
        )

    async def _recent_history(self, conversation_id: int, cfg) -> list[dict]:
        """近 N 条历史 → OpenAI messages（IN=user，OUT=assistant）。"""
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(ChatMessage.direction, ChatMessage.content)
                    .where(
                        ChatMessage.conversation_id == conversation_id,
                        ChatMessage.content_type == 1,
                    )
                    .order_by(ChatMessage.message_time.desc())
                    .limit(cfg.history_max_messages)
                )
            ).all()
        history = [
            {"role": "user" if d == "IN" else "assistant", "content": c or ""}
            for d, c in reversed(rows)
        ]
        return [m for m in history if m["content"].strip()]

    async def _goods_context(self, account_id: int, xy_goods_id: str) -> str:
        if not xy_goods_id:
            return ""
        async with self._session_factory() as db:
            goods = (
                await db.execute(
                    select(Goods).where(
                        Goods.account_id == account_id,
                        Goods.external_goods_id == xy_goods_id,
                    )
                )
            ).scalar_one_or_none()
        if goods is None:
            return ""
        price = goods.price_cent / 100 if goods.price_cent else None
        parts = [f"《{goods.title}》"]
        if price is not None:
            parts.append(f"价格 {price:.2f} 元")
        if goods.description:
            parts.append(f"描述：{goods.description[:200]}")
        return "，".join(parts)

    async def _send(self, account_id: int, peer_id: str, text: str) -> bool:
        if self._sender is not None:
            return await self._sender(account_id, peer_id, text)
        from app.messaging.manager import ws_manager

        client = ws_manager.get_client(account_id)
        if client is None:
            logger.warning("账号无 WS 连接，回复未发出 accountId=%d", account_id)
            return False
        return await client.send_text(peer_id, text)

    async def _store_outbound(self, account_id: int, conversation_id: int, text: str) -> None:
        async with self._session_factory() as db:
            db.add(
                ChatMessage(
                    account_id=account_id,
                    conversation_id=conversation_id,
                    pnm_id=f"out-{uuid.uuid4().hex}",
                    content_type=1,
                    direction="OUT",
                    content=text,
                    is_auto_reply=1,
                    message_time=int(time.time() * 1000),
                )
            )
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.id == conversation_id)
                )
            ).scalar_one()
            conv.last_auto_reply_at = datetime.now()
            await db.commit()

    async def _get_or_create_conversation(
        self, db, account_id: int, peer_id: str, peer_name: str | None
    ) -> Conversation:
        conv = (
            await db.execute(
                select(Conversation)
                .where(
                    Conversation.account_id == account_id,
                    Conversation.peer_id == peer_id,
                )
                .order_by(Conversation.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if conv is None:
            conv = Conversation(account_id=account_id, peer_id=peer_id, peer_name=peer_name)
            db.add(conv)
            await db.flush()
            logger.info("新会话建立 accountId=%d peer=%s", account_id, peer_id)
        elif peer_name and conv.peer_name != peer_name:
            conv.peer_name = peer_name
        return conv


def _compact_raw(payload: dict) -> dict:
    """raw_payload 只留结构化字段，去掉巨大的 rawPayload 递归体。"""
    return {k: v for k, v in payload.items() if k != "rawPayload" and not isinstance(v, dict)}


def create_reply_engine(session_factory, **kwargs) -> ReplyEngine:
    """工厂（lifespan 中创建并 start；sender 默认走 ws_manager）。"""
    return ReplyEngine(session_factory, **kwargs)
