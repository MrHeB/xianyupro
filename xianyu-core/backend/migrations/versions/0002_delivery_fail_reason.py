"""delivery_record 补 fail_reason 列（阶段 4-4：失败/跳过原因对运维必需）

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-14
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "delivery_record",
        sa.Column("fail_reason", sa.String(255), comment="失败/跳过原因"),
    )


def downgrade() -> None:
    op.drop_column("delivery_record", "fail_reason")
