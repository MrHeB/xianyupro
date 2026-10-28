"""消息与会话模型（含会话级自动回复暂停状态机）。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversation"
    __table_args__ = (
        # 一账号一买家一会话
        {"comment": "会话（含自动回复暂停状态机）"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    peer_id: Mapped[str] = mapped_column(String(64), index=True, comment="对端用户ID")
    peer_name: Mapped[str | None] = mapped_column(String(128), comment="对端昵称")
    goods_id: Mapped[int | None] = mapped_column(ForeignKey("goods.id"), comment="关联商品")
    # —— 会话级自动回复状态机（跨重启持久化）——
    auto_reply_paused: Mapped[int] = mapped_column(
        SmallInteger, default=0, comment="人工暂停自动回复 0/1"
    )
    manual_disabled: Mapped[int] = mapped_column(
        SmallInteger, default=0, comment="人工禁用（发\"开启自动回复\"恢复）0/1"
    )
    last_manual_reply_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后人工回复")
    last_auto_reply_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后自动回复")


class ChatMessage(TimestampMixin, Base):
    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversation.id", ondelete="CASCADE"), index=True
    )
    # pnm_id 幂等去重（WS 周期推送会重复）
    pnm_id: Mapped[str] = mapped_column(String(128), unique=True, comment="平台消息ID")
    message_uid: Mapped[str | None] = mapped_column(String(128), index=True, comment="消息UID")
    # 1 文本 / 2 图片 / 25 已拍 / 26 付款 / 28 发货 / 32 已读（见 protocol.md §7）
    content_type: Mapped[int] = mapped_column(SmallInteger, comment="内容类型")
    # IN / OUT
    direction: Mapped[str] = mapped_column(String(4), comment="方向 IN/OUT")
    content: Mapped[str | None] = mapped_column(Text, comment="消息内容")
    is_auto_reply: Mapped[int] = mapped_column(
        SmallInteger, default=0, comment="是否 AI 自动回复 0/1"
    )
    # 毫秒时间戳（闲鱼侧全是 ms）
    message_time: Mapped[int] = mapped_column(BigInteger, comment="消息时间(ms)")
    raw_payload: Mapped[dict | None] = mapped_column(JSON, comment="原始报文")
