"""SQLAlchemy 数据模型（蓝本：docs/protocol.md §8）。

硬性约定（§8.7）：
1. 外部 ID 一律 VARCHAR，禁止 BIGINT（防静默 NULL 导致重复发货）；
2. 软删 deleted TINYINT NOT NULL DEFAULT 0；
3. 金额统一"分"（BIGINT *_cent）；
4. 幂等键：pnm_id / order_id / event_key 全库唯一约束兜底。
"""

from app.models.account import Account, AccountAuth, AccountReplyConfig, AccountRuntime
from app.models.alibaba_account import AlibabaAccount
from app.models.base import Base
from app.models.fulfillment import (
    CardGroup,
    CardItem,
    DeliveryRecord,
    DeliveryRule,
)
from app.models.goods import Goods, GoodsEditSnapshot
from app.models.listing_run import ListingRunStep
from app.models.message import ChatMessage, Conversation
from app.models.order import TradeOrder
from app.models.setting import AppSetting
from app.models.task import ScheduledTask
from app.models.user import User

__all__ = [
    "Base",
    "Account",
    "AccountAuth",
    "AccountRuntime",
    "AlibabaAccount",
    "User",
    "Goods",
    "GoodsEditSnapshot",
    "ListingRunStep",
    "TradeOrder",
    "ChatMessage",
    "Conversation",
    "CardGroup",
    "CardItem",
    "DeliveryRecord",
    "DeliveryRule",
    "ScheduledTask",
    "AppSetting",
]
