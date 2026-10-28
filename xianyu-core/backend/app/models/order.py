"""订单模型（品类 A/B 共用，category 字段分流履约策略）。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class TradeOrder(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "trade_order"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    # 双 VARCHAR 防大整数溢出被静默写 NULL（pro V1.35 的血泪教训）
    order_id: Mapped[str] = mapped_column(String(120), unique=True, comment="订单号")
    external_order_id: Mapped[str | None] = mapped_column(
        String(120), index=True, comment="外部订单号"
    )
    goods_id: Mapped[int | None] = mapped_column(ForeignKey("goods.id"), index=True)
    # 'A' 自动发货 / 'B' 仅推送通知人工履约
    category: Mapped[str] = mapped_column(String(1), comment="品类 A/B")
    buyer_id: Mapped[str | None] = mapped_column(String(64), comment="买家ID")
    buyer_name: Mapped[str | None] = mapped_column(String(128), comment="买家昵称")
    total_amount_cent: Mapped[int] = mapped_column(BigInteger, comment="订单金额（分）")
    # 0 待付款 / 1 已付款 / 2 待发货 / 3 已发货 / 4 已完成 / 5 已关闭
    order_status: Mapped[int] = mapped_column(SmallInteger, default=0, comment="订单状态")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, comment="支付时间")
