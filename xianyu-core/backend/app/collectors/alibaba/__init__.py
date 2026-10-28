"""1688 采集：httpx 直连详情页解析 + 货源账号 Cookie（匿名会被 x5sec 拦，见 executor）。"""

from app.collectors.alibaba.transform import clean_title, markup_price, transform_product
from app.collectors.alibaba.executor import AlibabaExecutor, import_1688_by_offer_ids

__all__ = [
    "clean_title",
    "markup_price",
    "transform_product",
    "AlibabaExecutor",
    "import_1688_by_offer_ids",
]
