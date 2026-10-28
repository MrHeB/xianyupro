"""调度任务表：DB 租约（lease）抢单，多 worker 天然互斥（参考 pro scheduled_task）。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ScheduledTask(TimestampMixin, Base):
    __tablename__ = "scheduled_task"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(64), index=True, comment="任务类型")
    cron_expression: Mapped[str] = mapped_column(String(64), comment="Cron 表达式")
    config_json: Mapped[dict | None] = mapped_column(JSON, comment="任务配置")
    enabled: Mapped[int] = mapped_column(Integer, default=1, comment="启用 0/1")
    # —— DB 租约三件套：worker 用 UPDATE ... WHERE lease 过期 抢占 ——
    lease_token: Mapped[str | None] = mapped_column(String(64), comment="租约令牌")
    lease_owner: Mapped[str | None] = mapped_column(String(64), comment="租约持有者")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, comment="租约到期")
    run_attempt_count: Mapped[int] = mapped_column(Integer, default=0, comment="执行次数")
    consecutive_failure_count: Mapped[int] = mapped_column(Integer, default=0, comment="连续失败")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, comment="上次执行")
