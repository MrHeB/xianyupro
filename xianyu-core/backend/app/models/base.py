"""模型公共列 mixin。"""

from datetime import datetime

from sqlalchemy import DateTime, SmallInteger, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """全项目 ORM 基类。"""


class TimestampMixin:
    """时间戳统一用 Python 本地 naive 时钟（default/onupdate 客户端生成）。

    业务代码（heal 超时判定、每日发货上限等）都用 datetime.now() 比较；
    若用 DB 的 func.now()，SQLite（UTC）与本地时钟差 8h 会误判超时
    （盘点 §4.5 同类坑）。server_default 仅作裸 SQL 插入的兜底。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, server_default=func.now(), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        server_default=func.now(),
        comment="更新时间",
    )


class SoftDeleteMixin:
    deleted: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0", comment="软删 0/1"
    )
