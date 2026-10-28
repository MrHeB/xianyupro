"""M0 前置修复（设计文档 §9-1/§8.4）：

- goods 加 source_offer_id（唯一，1688 采集去重 + 发布幂等键）与 local_path（本地工作区目录）；
- 新表 listing_run_step：流程面板步骤台账（三入口共用流水线的观测数据源）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "goods",
        sa.Column("source_offer_id", sa.String(64), nullable=True, comment="1688 offer_id（品类B 来源去重）"),
    )
    op.create_index("uq_goods_source_offer_id", "goods", ["source_offer_id"], unique=True)
    op.add_column(
        "goods",
        sa.Column("local_path", sa.String(255), nullable=True, comment="本地工作区目录"),
    )

    op.create_table(
        "listing_run_step",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False, comment="批次/运行 ID"),
        sa.Column("goods_id", sa.Integer(), sa.ForeignKey("goods.id", ondelete="SET NULL"), comment="商品ID"),
        sa.Column("offer_id", sa.String(64), comment="1688 offer_id"),
        sa.Column("step_seq", sa.Integer(), nullable=False, comment="步骤序号（从 1 起）"),
        sa.Column("node", sa.String(32), nullable=False, comment="步骤节点"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", comment="步骤状态"),
        sa.Column("started_at", sa.DateTime(), comment="开始时间"),
        sa.Column("finished_at", sa.DateTime(), comment="结束时间"),
        sa.Column("artifact_ref", sa.String(255), comment="关键产物指针"),
        sa.Column("error_kind", sa.String(32), comment="错误分类"),
        sa.Column("error_msg", sa.Text(), comment="可读失败原因"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), comment="更新时间"),
    )
    op.create_index("ix_listing_run_step_run_id", "listing_run_step", ["run_id"])
    op.create_index("ix_listing_run_step_goods_id", "listing_run_step", ["goods_id"])
    op.create_index("ix_listing_run_step_offer_id", "listing_run_step", ["offer_id"])
    op.create_index(
        "ix_listing_run_step_run_seq", "listing_run_step", ["run_id", "step_seq"], unique=True
    )


def downgrade() -> None:
    op.drop_table("listing_run_step")
    op.drop_index("uq_goods_source_offer_id", table_name="goods")
    op.drop_column("goods", "local_path")
    op.drop_column("goods", "source_offer_id")
