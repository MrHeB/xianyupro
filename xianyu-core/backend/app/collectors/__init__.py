"""collectors：采集插件（1688 非登录 / CSV 导入），品类 B 入口（阶段 4-5 移植）。"""

from app.collectors.base import (
    BaseCollector,
    BaseTransformer,
    PoolStatus,
    SourcedProduct,
    TransformedProduct,
)

__all__ = [
    "BaseCollector",
    "BaseTransformer",
    "PoolStatus",
    "SourcedProduct",
    "TransformedProduct",
]
