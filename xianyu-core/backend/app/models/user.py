"""系统用户（管理后台登录，非闲鱼账号）。

- 闲鱼账号（Account）是"卖货的号"（会话池管理）；系统用户是"操作后台的人"。
- 一人可挂多个闲鱼账号：account.user_id 指向本表（见 account.py）。
- role：admin（全部账号可见/管用户/改系统配置）/ user（仅自己的数据）。
"""

from sqlalchemy import SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class User(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "sys_user"
    __table_args__ = ({"comment": "系统用户（管理后台登录）"},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, comment="登录名")
    # bcrypt hash（cost 12）
    password_hash: Mapped[str] = mapped_column(String(128), comment="bcrypt 密码哈希")
    # admin / user
    role: Mapped[str] = mapped_column(String(16), default="user", comment="角色 admin/user")
    # 1 启用 / 0 停用（停用后 token 立即失效）
    status: Mapped[int] = mapped_column(SmallInteger, default=1, comment="1启用/0停用")
    remark: Mapped[str | None] = mapped_column(String(255), comment="备注（会员等级预留位）")