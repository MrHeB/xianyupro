"""新增 account_reply_config：按账号 AI 回复人设（叠在全局 system_prompt 之后）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "account_reply_config",
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("persona_prompt", sa.Text(), comment="本账号人设（追加在全局之后）"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), comment="更新时间"),
    )


def downgrade() -> None:
    op.drop_table("account_reply_config")
