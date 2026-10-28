"""运行时设置 KV 表（阶段 F3）：DB 覆盖 .env，免重启生效。

key 取名：llm_* 与 ai_*（与 config.py 对应字段一致）；
value 一律字符串，类型转换在 settings/service.py 里做。
"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AppSetting(TimestampMixin, Base):
    __tablename__ = "app_setting"
    __table_args__ = ({"comment": "运行时设置 KV（覆盖 .env）"},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, comment="配置键")
    value: Mapped[str | None] = mapped_column(Text, comment="配置值")