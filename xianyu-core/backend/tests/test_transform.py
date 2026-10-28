"""1688 → 闲鱼转换规则测试（规则来自 pds-tool，盘点 §1.2）。"""

from app.collectors.alibaba.transform import clean_title, markup_price, transform_product
from app.collectors.base import SourcedProduct


def test_clean_title_removes_marketing_words():
    assert clean_title("厂家直销优质数据线 一手货源") == "优质数据线"


def test_clean_title_removes_brackets():
    assert clean_title("数据线【2024新款】快充") == "数据线快充"
    assert clean_title("数据线[包邮]快充") == "数据线快充"


def test_clean_title_collapses_whitespace():
    assert clean_title("  数据线   快充  ") == "数据线 快充"


def test_markup_price_under_10yuan():
    # 5 元 × 1.3 = 6.5 元，≤10 不做尾数处理
    assert markup_price(500) == 650


def test_markup_price_over_10yuan_199_style():
    # 20 元 × 1.3 = 26 元 → 取整 26 → 25.9（-0.1 尾数定价）
    assert markup_price(2000) == 2590


def test_markup_price_minimum_floor():
    assert markup_price(10, rate=1.0) == 10


def test_transform_product_pipeline():
    product = SourcedProduct(
        source="1688",
        source_offer_id="728001",
        title="【现货】厂家直销手机壳 一件代发包邮",
        price_cent=1000,
    )
    result = transform_product(product)
    assert result.title == "手机壳"
    assert result.price_cent == 1290  # 10 元 × 1.3 = 13 元（≤10 不尾数化？13>10 → 12.9）
