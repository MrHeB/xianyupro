"""商品模型。

category 划分两线（流程方案 §7 决策 5/6）：
- A 虚拟商品：手动上架，卡密自动发货；
- B 1688 采集：自动上架，人工履约。
"""

from sqlalchemy import JSON, BigInteger, ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class Goods(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "goods"
    __table_args__ = ({"comment": "商品（含品类 A 手动上架 / 品类 B 采集上架）"},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 归属账号（多账号：每账号独立商品）
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    # 闲鱼商品 ID，外部 ID 一律字符串
    external_goods_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, comment="闲鱼商品ID（未上架时为空）"
    )
    # 1688 offer_id：采集去重 + 发布幂等唯一键（风控手册 §1.6 / 设计文档 §9-1）
    source_offer_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, comment="1688 offer_id（品类B 来源去重）"
    )
    # 本地工作区目录（data/collect/<batch>/offers/<offer_id>/，M1 启用）
    local_path: Mapped[str | None] = mapped_column(String(255), comment="本地工作区目录")
    # 'A' 虚拟（手动上架+卡密自动发货）/ 'B' 1688 采集（人工履约）
    category: Mapped[str] = mapped_column(String(1), comment="品类 A/B")
    # manual / 1688 / csv
    source: Mapped[str] = mapped_column(String(16), default="manual", comment="来源")
    title: Mapped[str] = mapped_column(String(128), comment="标题")
    description: Mapped[str | None] = mapped_column(Text, comment="描述")
    # 金额统一"分"
    price_cent: Mapped[int] = mapped_column(BigInteger, comment="售价（分）")
    source_price_cent: Mapped[int | None] = mapped_column(BigInteger, comment="源价（分，品类B）")
    # 0 下架 / 1 在售 / 2 已售（同步自闲鱼状态映射）
    status: Mapped[int] = mapped_column(SmallInteger, default=0, comment="0下架/1在售/2已售")
    # 商品池状态机（品类 B 采集流程）：SOURCED→MAPPED→PUSHED→FAILED
    pool_status: Mapped[str | None] = mapped_column(String(16), comment="商品池状态")
    image_urls: Mapped[list | None] = mapped_column(JSON, comment="图片 URL 列表")
    raw_payload: Mapped[dict | None] = mapped_column(JSON, comment="原始快照 JSON")
    # None=继承账号全局 / 0=强制关 / 1=强制开
    auto_reply_enabled: Mapped[int | None] = mapped_column(
        SmallInteger, comment="AI自动回复 NULL继承/0关/1开"
    )
    auto_relist_enabled: Mapped[int] = mapped_column(SmallInteger, default=0, comment="售罄重上 0/1")


class GoodsEditSnapshot(TimestampMixin, Base):
    """发布快照：售罄链式重发、编辑回显都靠全量快照回放。"""

    __tablename__ = "goods_edit_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    goods_id: Mapped[int] = mapped_column(
        ForeignKey("goods.id", ondelete="CASCADE"), index=True
    )
    # publish / edit / detail_api / relist
    source: Mapped[str] = mapped_column(String(16), comment="快照来源")
    snapshot_json: Mapped[dict] = mapped_column(JSON, comment="全量发布快照")
