"""运行时设置 KV 表（阶段 F3：DB 覆盖 .env，免重启生效）

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_setting",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("key", sa.String(128), nullable=False, unique=True, comment="配置键"),
        sa.Column("value", sa.Text(), comment="配置值"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), comment="更新时间"),
    )


def downgrade() -> None:
    op.drop_table("app_setting")