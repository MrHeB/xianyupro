"""系统用户表 + account.user_id 归属（阶段 F1：用户体系 + JWT）

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sys_user",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True, comment="登录名"),
        sa.Column("password_hash", sa.String(128), nullable=False, comment="bcrypt 密码哈希"),
        sa.Column("role", sa.String(16), nullable=False, server_default="user", comment="角色 admin/user"),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1", comment="1启用/0停用"),
        sa.Column("remark", sa.String(255), comment="备注"),
        sa.Column("deleted", sa.SmallInteger(), nullable=False, server_default="0", comment="软删 0/1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), comment="更新时间"),
    )
    op.add_column(
        "account",
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("sys_user.id", ondelete="SET NULL"),
            index=True, comment="归属系统用户",
        ),
    )


def downgrade() -> None:
    op.drop_column("account", "user_id")
    op.drop_table("sys_user")