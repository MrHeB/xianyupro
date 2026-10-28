"""1688 货源账号（采集侧登录态）。

与 `account`（闲鱼发布账号）刻意分表：1688 与闲鱼同属淘宝会员体系，同一个淘宝账号
在两边 `unb` 相同，若复用 account 表 + platform 字段，按 external_uid upsert 会把
1688 的 Cookie 覆盖到闲鱼账号上，闲鱼登录态当场作废。

不拆 auth 子表：本表不被任何业务表 FK 引用，拆表只多一次 join。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class AlibabaAccount(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "alibaba_account"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 归属系统用户（一人可有多个货源账号）；NULL = 系统级
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL"), index=True, comment="归属用户"
    )
    # 1688/淘宝会员 ID，取自 Cookie 的 unb
    external_uid: Mapped[str] = mapped_column(String(64), unique=True, comment="unb")
    nickname: Mapped[str | None] = mapped_column(String(128), comment="昵称")
    # 0 停用 / 1 启用
    status: Mapped[int] = mapped_column(SmallInteger, default=1, comment="0停用/1启用")
    # enc:v1:{b64url(iv)}:{b64url(ct+tag)}，与闲鱼 Cookie 同一套 AES-GCM
    encrypted_cookie: Mapped[str] = mapped_column(Text, comment="AES-GCM 加密 Cookie")
    encrypted_token: Mapped[str | None] = mapped_column(Text, comment="加密 _m_h5_tk")
    # 0 待校验 / 1 正常 / 2 过期
    cookie_status: Mapped[int] = mapped_column(SmallInteger, default=0, comment="Cookie 状态")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后登录时间")
    remark: Mapped[str | None] = mapped_column(String(255), comment="备注")
