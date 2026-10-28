"""1688 → 闲鱼 商品转换规则（直接照抄 pds-tool，盘点 §1.2，纯函数可单测）。

- 标题去噪：剔除 1688 营销词 + 【】[] 内容；
- 加价策略：售价 = 源价 × 倍率（默认 1.3），>10 元取整再 −0.1（19.9 式尾数定价）。
"""

import re

from app.collectors.base import SourcedProduct, TransformedProduct

# 1688 常见营销词（可按需扩充）
MARKETING_WORDS = (
    "厂家直销", "一手货源", "批发价", "一件代发", "包邮", "热销", "爆款",
    "低价", "促销", "特惠", "清仓", "厂家直供", "货源",
)

_BRACKET_RE = re.compile(r"[【\[][^】\]]*[】\]]")


def clean_title(title: str) -> str:
    """标题去噪：去营销词 + 去【】[] 括号内容，压缩空白。"""
    title = _BRACKET_RE.sub("", title)
    for word in MARKETING_WORDS:
        title = title.replace(word, "")
    title = re.sub(r"\s+", " ", title).strip(" -|/，,")
    return title


def markup_price(source_price_cent: int, rate: float = 1.3) -> int:
    """加价：售价 = 源价 × 倍率；>10 元取整再 −0.1（19.9 式尾数）。

    输入输出都是"分"。
    """
    priced_cent = round(source_price_cent * rate)
    yuan = priced_cent / 100
    if yuan > 10:
        # 取整到元再减 0.1，例如 25.3 → 25 → 24.9
        priced_cent = int(yuan) * 100 - 10
    return max(priced_cent, 1)


def transform_product(product: SourcedProduct, *, rate: float = 1.3) -> TransformedProduct:
    """SOURCED → MAPPED 转换入口。"""
    title = clean_title(product.title)
    return TransformedProduct(
        title=title,
        description=product.description or title,
        price_cent=markup_price(product.price_cent, rate),
        image_urls=product.image_urls,
    )
