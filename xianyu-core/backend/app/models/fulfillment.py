"""卡密库存与发货（品类 A 专用；品类 B 只推订单通知不进这套表）。"""

from sqlalchemy import JSON, ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CardGroup(TimestampMixin, Base):
    __tablename__ = "card_group"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, comment="卡密组名")
    goods_id: Mapped[int | None] = mapped_column(ForeignKey("goods.id"), comment="绑定商品")
    # 冗余计数，低库存告警用 alert_threshold
    total_count: Mapped[int] = mapped_column(default=0, comment="总数")
    used_count: Mapped[int] = mapped_column(default=0, comment="已用")
    alert_threshold: Mapped[int] = mapped_column(default=5, comment="告警阈值")


class CardItem(TimestampMixin, Base):
    __tablename__ = "card_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("card_group.id", ondelete="CASCADE"), index=True
    )
    card_key: Mapped[str] = mapped_column(String(255), comment="卡号")
    card_value: Mapped[str | None] = mapped_column(String(512), comment="卡值")
    # 0 未用 / 1 已认领 / 2 已用（认领失败回滚 0，孤儿回滚 heal）
    status: Mapped[int] = mapped_column(SmallInteger, default=0, comment="状态")
    used_order_id: Mapped[str | None] = mapped_column(String(120), index=True, comment="消耗订单")


class DeliveryRule(TimestampMixin, Base):
    """发货规则：商品级或账号级（goods_id 为空 = 账号级默认）。"""

    __tablename__ = "delivery_rule"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    goods_id: Mapped[int | None] = mapped_column(ForeignKey("goods.id"), comment="商品级规则")
    # kami / text / image / segments
    delivery_mode: Mapped[str] = mapped_column(String(16), comment="发货形态")
    trigger_on_pay: Mapped[int] = mapped_column(SmallInteger, default=1, comment="付款即发 0/1")
    max_delivery_per_day: Mapped[int] = mapped_column(default=200, comment="每日上限")


class DeliveryRecord(TimestampMixin, Base):
    """发货记录：每单最多一条成功（order_id 唯一约束兜底幂等）。"""

    __tablename__ = "delivery_record"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(120), unique=True, comment="订单号")
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    delivery_mode: Mapped[str] = mapped_column(String(16), comment="发货形态")
    # pending → message_sending → message_confirmed → platform_confirmed → success
    # 任何 unknown 态永不自动重发（防重发哲学，见盘点 §4.4）
    status: Mapped[str] = mapped_column(String(32), default="pending", comment="状态")
    card_item_id: Mapped[int | None] = mapped_column(ForeignKey("card_item.id"))
    receiver_info: Mapped[dict | None] = mapped_column(JSON, comment="收件信息快照")
    # failed/unknown 的原因（用户可读中文，不暴露 SQL/堆栈）；非终态链上也可记录中间原因
    fail_reason: Mapped[str | None] = mapped_column(String(255), comment="失败/跳过原因")
