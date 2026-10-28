"""采集插件基类：sourcing → transform → push 三段式数据流（借鉴 pds-tool，盘点 §1.2）。

商品池状态机（设计文档 §3.2）：
    SOURCED → DECIDED（LLM 定价/文案/风险扫描完成 = 待确认，定时链路到此为止）
            → MAPPED（人工放行）→ PUSHED / FAILED
`auto_listing` 只扫 MAPPED，所以未经人工放行的商品永远不会自己发出去（§8.1 防线）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class PoolStatus(str, Enum):
    SOURCED = "SOURCED"    # 已采集入库
    DECIDED = "DECIDED"    # 已定价/文案/扫描，待人工确认（不可直接上架）
    MAPPED = "MAPPED"      # 人工放行，待上架
    PUSHED = "PUSHED"      # 已上架
    FAILED = "FAILED"


@dataclass
class SourcedProduct:
    """采集到的原始商品（品类 B 专用，全部走人工履约线）。"""

    source: str                    # 1688 / csv
    source_offer_id: str           # 外部 ID 一律字符串
    title: str
    description: str | None = None
    price_cent: int = 0            # 源价（分）
    image_urls: list[str] | None = None
    raw: dict | None = None        # 原始 JSON


@dataclass
class TransformedProduct:
    """转换后待上架商品。"""

    title: str
    description: str
    price_cent: int
    category_hint: str | None = None
    image_urls: list[str] | None = None


class BaseCollector(ABC):
    """采集器接口：只负责 sourcing，产出入商品池（pool_status=SOURCED）。"""

    name: str = "base"

    @abstractmethod
    async def collect(self, keyword: str, limit: int = 20) -> list[SourcedProduct]:
        """按关键词采集，返回原始商品列表。"""


class BaseTransformer(ABC):
    """转换器接口：SOURCED → MAPPED（标题去噪 / 加价 / 类目映射）。"""

    @abstractmethod
    def transform(self, product: SourcedProduct) -> TransformedProduct:
        ...


# 风控/登录壳特征：详情页与搜索页共用同一套拦截壳（实测见 scripts/spike_1688_search_auth.py）
RISK_SHELL_MARKERS = ("_____tmd_____", "x5secdata", "rgv587_flag")


def is_risk_shell(html: str) -> bool:
    """抓到的到底是商品页/结果页，还是 x5sec 风控壳。

    放在 base 而不是 executor：executor（详情页）与 search_browser（列表页）都要判，
    而后者若从前者 import 会和 `alibaba/__init__` 形成循环引用。
    """
    return bool(html) and any(marker in html for marker in RISK_SHELL_MARKERS)
