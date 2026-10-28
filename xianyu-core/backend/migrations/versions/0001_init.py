"""initial schema（protocol.md §8 建表蓝本）

Revision ID: 0001
Revises:
Create Date: 2026-09-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ts_cols() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), comment="更新时间"),
    ]


def upgrade() -> None:
    # ==== 账号 ====
    op.create_table(
        "account",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("external_uid", sa.String(64), nullable=False, unique=True, comment="unb"),
        sa.Column("nickname", sa.String(128), comment="昵称"),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1", comment="0停用/1启用"),
        sa.Column("fish_shop_user", sa.SmallInteger(), nullable=False, server_default="0", comment="鱼小铺 0/1"),
        sa.Column("remark", sa.String(255), comment="备注"),
        sa.Column("deleted", sa.SmallInteger(), nullable=False, server_default="0", comment="软删 0/1"),
        *_ts_cols(),
    )
    op.create_table(
        "account_auth",
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("account.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("encrypted_cookie", sa.Text(), nullable=False, comment="AES-GCM 加密 Cookie"),
        sa.Column("encrypted_token", sa.Text(), comment="加密 _m_h5_tk"),
        sa.Column("ws_token", sa.Text(), comment="IM token（TTL 30min）"),
        sa.Column("cookie_status", sa.SmallInteger(), nullable=False, server_default="0", comment="Cookie 状态"),
        sa.Column("last_login_at", sa.DateTime(), comment="最后登录时间"),
        *_ts_cols(),
    )
    op.create_table(
        "account_runtime",
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("account.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ws_status", sa.String(16), nullable=False, server_default="offline", comment="WS 状态"),
        sa.Column("last_heartbeat_at", sa.DateTime(), comment="最后心跳"),
        sa.Column("ws_latency_ms", sa.Integer(), comment="WS 延迟(ms)"),
        *_ts_cols(),
    )

    # ==== 商品 ====
    op.create_table(
        "goods",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("external_goods_id", sa.String(64), unique=True, comment="闲鱼商品ID"),
        sa.Column("category", sa.String(1), nullable=False, comment="品类 A/B"),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual", comment="来源"),
        sa.Column("title", sa.String(128), nullable=False, comment="标题"),
        sa.Column("description", sa.Text(), comment="描述"),
        sa.Column("price_cent", sa.BigInteger(), nullable=False, comment="售价（分）"),
        sa.Column("source_price_cent", sa.BigInteger(), comment="源价（分，品类B）"),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="0", comment="0下架/1在售/2已售"),
        sa.Column("pool_status", sa.String(16), comment="商品池状态 SOURCED/MAPPED/PUSHED/FAILED"),
        sa.Column("image_urls", sa.JSON(), comment="图片 URL 列表"),
        sa.Column("raw_payload", sa.JSON(), comment="原始快照 JSON"),
        sa.Column("auto_reply_enabled", sa.SmallInteger(), comment="NULL继承/0关/1开"),
        sa.Column("auto_relist_enabled", sa.SmallInteger(), nullable=False, server_default="0", comment="售罄重上 0/1"),
        sa.Column("deleted", sa.SmallInteger(), nullable=False, server_default="0", comment="软删 0/1"),
        *_ts_cols(),
    )
    op.create_table(
        "goods_edit_snapshot",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("goods_id", sa.Integer(),
                  sa.ForeignKey("goods.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("source", sa.String(16), nullable=False, comment="快照来源"),
        sa.Column("snapshot_json", sa.JSON(), nullable=False, comment="全量发布快照"),
        *_ts_cols(),
    )

    # ==== 订单 ====
    op.create_table(
        "trade_order",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("order_id", sa.String(120), nullable=False, unique=True, comment="订单号"),
        sa.Column("external_order_id", sa.String(120), index=True, comment="外部订单号"),
        sa.Column("goods_id", sa.Integer(), sa.ForeignKey("goods.id"), index=True),
        sa.Column("category", sa.String(1), nullable=False, comment="品类 A/B"),
        sa.Column("buyer_id", sa.String(64), comment="买家ID"),
        sa.Column("buyer_name", sa.String(128), comment="买家昵称"),
        sa.Column("total_amount_cent", sa.BigInteger(), nullable=False, comment="订单金额（分）"),
        sa.Column("order_status", sa.SmallInteger(), nullable=False, server_default="0", comment="订单状态"),
        sa.Column("paid_at", sa.DateTime(), comment="支付时间"),
        sa.Column("deleted", sa.SmallInteger(), nullable=False, server_default="0", comment="软删 0/1"),
        *_ts_cols(),
    )

    # ==== 消息与会话 ====
    op.create_table(
        "conversation",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("peer_id", sa.String(64), nullable=False, index=True, comment="对端用户ID"),
        sa.Column("peer_name", sa.String(128), comment="对端昵称"),
        sa.Column("goods_id", sa.Integer(), sa.ForeignKey("goods.id"), comment="关联商品"),
        sa.Column("auto_reply_paused", sa.SmallInteger(), nullable=False, server_default="0", comment="人工暂停 0/1"),
        sa.Column("manual_disabled", sa.SmallInteger(), nullable=False, server_default="0", comment="人工禁用 0/1"),
        sa.Column("last_manual_reply_at", sa.DateTime(), comment="最后人工回复"),
        sa.Column("last_auto_reply_at", sa.DateTime(), comment="最后自动回复"),
        *_ts_cols(),
    )
    op.create_table(
        "chat_message",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("conversation_id", sa.Integer(),
                  sa.ForeignKey("conversation.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("pnm_id", sa.String(128), nullable=False, unique=True, comment="平台消息ID"),
        sa.Column("message_uid", sa.String(128), index=True, comment="消息UID"),
        sa.Column("content_type", sa.SmallInteger(), nullable=False, comment="内容类型"),
        sa.Column("direction", sa.String(4), nullable=False, comment="方向 IN/OUT"),
        sa.Column("content", sa.Text(), comment="消息内容"),
        sa.Column("is_auto_reply", sa.SmallInteger(), nullable=False, server_default="0", comment="AI回复 0/1"),
        sa.Column("message_time", sa.BigInteger(), nullable=False, comment="消息时间(ms)"),
        sa.Column("raw_payload", sa.JSON(), comment="原始报文"),
        *_ts_cols(),
    )

    # ==== 卡密与发货 ====
    op.create_table(
        "card_group",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True, comment="卡密组名"),
        sa.Column("goods_id", sa.Integer(), sa.ForeignKey("goods.id"), comment="绑定商品"),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0", comment="总数"),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0", comment="已用"),
        sa.Column("alert_threshold", sa.Integer(), nullable=False, server_default="5", comment="告警阈值"),
        *_ts_cols(),
    )
    op.create_table(
        "card_item",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("group_id", sa.Integer(),
                  sa.ForeignKey("card_group.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("card_key", sa.String(255), nullable=False, comment="卡号"),
        sa.Column("card_value", sa.String(512), comment="卡值"),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="0", comment="0未用/1认领/2已用"),
        sa.Column("used_order_id", sa.String(120), index=True, comment="消耗订单"),
        *_ts_cols(),
    )
    op.create_table(
        "delivery_rule",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("goods_id", sa.Integer(), sa.ForeignKey("goods.id"), comment="商品级规则"),
        sa.Column("delivery_mode", sa.String(16), nullable=False, comment="发货形态"),
        sa.Column("trigger_on_pay", sa.SmallInteger(), nullable=False, server_default="1", comment="付款即发 0/1"),
        sa.Column("max_delivery_per_day", sa.Integer(), nullable=False, server_default="200", comment="每日上限"),
        *_ts_cols(),
    )
    op.create_table(
        "delivery_record",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("order_id", sa.String(120), nullable=False, unique=True, comment="订单号"),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("account.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("delivery_mode", sa.String(16), nullable=False, comment="发货形态"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending", comment="状态"),
        sa.Column("card_item_id", sa.Integer(), sa.ForeignKey("card_item.id")),
        sa.Column("receiver_info", sa.JSON(), comment="收件信息快照"),
        *_ts_cols(),
    )

    # ==== 调度 ====
    op.create_table(
        "scheduled_task",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("task_type", sa.String(64), nullable=False, index=True, comment="任务类型"),
        sa.Column("cron_expression", sa.String(64), nullable=False, comment="Cron 表达式"),
        sa.Column("config_json", sa.JSON(), comment="任务配置"),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1", comment="启用 0/1"),
        sa.Column("lease_token", sa.String(64), comment="租约令牌"),
        sa.Column("lease_owner", sa.String(64), comment="租约持有者"),
        sa.Column("lease_expires_at", sa.DateTime(), comment="租约到期"),
        sa.Column("run_attempt_count", sa.Integer(), nullable=False, server_default="0", comment="执行次数"),
        sa.Column("consecutive_failure_count", sa.Integer(), nullable=False, server_default="0", comment="连续失败"),
        sa.Column("last_run_at", sa.DateTime(), comment="上次执行"),
        *_ts_cols(),
    )


def downgrade() -> None:
    for table in (
        "scheduled_task",
        "delivery_record",
        "delivery_rule",
        "card_item",
        "card_group",
        "chat_message",
        "conversation",
        "trade_order",
        "goods_edit_snapshot",
        "goods",
        "account_runtime",
        "account_auth",
        "account",
    ):
        op.drop_table(table)
