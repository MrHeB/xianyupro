"""流程面板步骤台账（设计文档 §8.4）。

定时任务、前端按钮、MCP 会话三个入口驱动同一条流水线：handler 执行业务函数
前后各写一次台账，面板只读台账、不参与业务逻辑——"三入口不分叉"的关键约束。
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ListingRunStep(TimestampMixin, Base):
    __tablename__ = "listing_run_step"
    __table_args__ = (
        Index("ix_listing_run_step_run_seq", "run_id", "step_seq", unique=True),
        {"comment": "采集→上架流水线步骤台账（流程面板数据源）"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True, comment="批次/运行 ID")
    # 入池前只有 offer_id，入池后才有 goods_id，两者均可空
    goods_id: Mapped[int | None] = mapped_column(
        ForeignKey("goods.id", ondelete="SET NULL"), index=True, comment="商品ID"
    )
    offer_id: Mapped[str | None] = mapped_column(
        String(64), index=True, comment="1688 offer_id"
    )
    step_seq: Mapped[int] = mapped_column(comment="步骤序号（从 1 起）")
    # collect / pricing / copy / risk_scan / confirm / import / delivery_config / publish
    node: Mapped[str] = mapped_column(String(32), comment="步骤节点")
    # pending / running / ok / warn / failed / skipped
    status: Mapped[str] = mapped_column(String(16), default="pending", comment="步骤状态")
    started_at: Mapped[datetime | None] = mapped_column(comment="开始时间")
    finished_at: Mapped[datetime | None] = mapped_column(comment="结束时间")
    artifact_ref: Mapped[str | None] = mapped_column(
        String(255), comment="关键产物指针（路径/ID）"
    )
    # 错误分类对齐 protocol.md §1.3（ErrorKind 值）
    error_kind: Mapped[str | None] = mapped_column(String(32), comment="错误分类")
    error_msg: Mapped[str | None] = mapped_column(Text, comment="可读失败原因")
