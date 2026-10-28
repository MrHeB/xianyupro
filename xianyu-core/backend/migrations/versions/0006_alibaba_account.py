"""新增 1688 货源账号表 alibaba_account（1688 采集侧登录态）。

为什么独立表而不是给 account 加 platform 字段：1688 与闲鱼同属淘宝会员体系，
同一个淘宝账号两边 unb 相同，复用 account 会让按 external_uid 的 upsert 把
1688 Cookie 覆盖到闲鱼账号上，闲鱼登录态作废。

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alibaba_account",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("sys_user.id", ondelete="SET NULL"),
            comment="归属用户",
        ),
        sa.Column("external_uid", sa.String(64), nullable=False, comment="unb"),
        sa.Column("nickname", sa.String(128), comment="昵称"),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1", comment="0停用/1启用"),
        sa.Column("encrypted_cookie", sa.Text(), nullable=False, comment="AES-GCM 加密 Cookie"),
        sa.Column("encrypted_token", sa.Text(), comment="加密 _m_h5_tk"),
        sa.Column(
            "cookie_status", sa.SmallInteger(), nullable=False, server_default="0", comment="Cookie 状态"
        ),
        sa.Column("last_login_at", sa.DateTime(), comment="最后登录时间"),
        sa.Column("remark", sa.String(255), comment="备注"),
        sa.Column("deleted", sa.SmallInteger(), nullable=False, server_default="0", comment="软删 0/1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), comment="更新时间"),
    )
    op.create_index("uq_alibaba_account_external_uid", "alibaba_account", ["external_uid"], unique=True)
    op.create_index("ix_alibaba_account_user_id", "alibaba_account", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_alibaba_account_user_id", table_name="alibaba_account")
    op.drop_index("uq_alibaba_account_external_uid", table_name="alibaba_account")
    op.drop_table("alibaba_account")
