"""账号 4 表（简化自 pro 的 5 表家族：合并 health/membership，按需再加）。

- account：账号主数据，external_uid = Cookie 里的 unb；
- account_auth：加密凭据（AES-GCM enc:v1，见 auth/cookie_crypto.py）；
- account_runtime：在线/WS/心跳等易变状态，与主数据分离避免频繁写主表；
- account_reply_config：按账号 AI 回复人设（全局打底 + 本表叠加）。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class Account(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 归属系统用户（一人多号）；NULL = 未归属（历史数据或系统级账号）
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL"), index=True, comment="归属用户"
    )
    # external_uid = unb（闲鱼用户 ID），外部 ID 一律字符串
    external_uid: Mapped[str] = mapped_column(String(64), unique=True, comment="unb")
    nickname: Mapped[str | None] = mapped_column(String(128), comment="昵称")
    # 0 停用 / 1 启用
    status: Mapped[int] = mapped_column(SmallInteger, default=1, comment="0停用/1启用")
    # 是否鱼小铺账号（发布接口两套：普通 vs backend.*，见 protocol.md §3）
    fish_shop_user: Mapped[int] = mapped_column(SmallInteger, default=0, comment="鱼小铺 0/1")
    remark: Mapped[str | None] = mapped_column(String(255), comment="备注")


class AccountAuth(TimestampMixin, Base):
    __tablename__ = "account_auth"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), primary_key=True
    )
    # enc:v1:{b64url(iv)}:{b64url(ct+tag)}，密钥 = SHA256(COOKIE_CRYPTO_SECRET)
    encrypted_cookie: Mapped[str] = mapped_column(Text, comment="AES-GCM 加密 Cookie")
    encrypted_token: Mapped[str | None] = mapped_column(Text, comment="加密 _m_h5_tk")
    ws_token: Mapped[str | None] = mapped_column(Text, comment="IM token（TTL 30min）")
    # 0 待校验 / 1 正常 / 2 过期（RGV587 不误标过期，只入滑块流程）
    cookie_status: Mapped[int] = mapped_column(SmallInteger, default=0, comment="Cookie 状态")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后登录时间")


class AccountRuntime(TimestampMixin, Base):
    __tablename__ = "account_runtime"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), primary_key=True
    )
    # offline / connecting / online
    ws_status: Mapped[str] = mapped_column(String(16), default="offline", comment="WS 状态")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后心跳")
    ws_latency_ms: Mapped[int | None] = mapped_column(comment="WS 延迟(ms)")


class AccountReplyConfig(TimestampMixin, Base):
    """按账号的 AI 回复人设（叠在全局 system_prompt 之后）。

    只留 persona_prompt 一列：全局配置负责合规约束（禁词/时长/风控口径），
    账号层只负责"这个号说话像谁"。空行 / 空文本 = 不叠加，行为与改动前一致。
    """

    __tablename__ = "account_reply_config"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), primary_key=True
    )
    persona_prompt: Mapped[str | None] = mapped_column(Text, comment="本账号人设（追加在全局之后）")
