"""1688 详情页 parser 的真实回归测试。

fixture 是 2026-09-19 用货源账号 Cookie 抓下来的**真实页面**（已脱去账号痕迹），
覆盖两种模板：
    detail_623375582529.html   /default      大市场（保温杯）
    detail_1053709411360.html  /processing   加工定制 jgdz（手机数据线）

为什么要真实样本：旧 parser 认 `__INIT_DATA__` / `runParams` 三条路径，在没有 fixture、
只 monkeypatch 掉 `parse_offer` 的单测下一直是"绿"的，而线上采 5 件字段全空（R2 验收
「字段完整率≥90%」实测 0%）。这里断言的是页面上**确实存在**的那几个值。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.collectors.alibaba import parser as parser_mod
from app.collectors.alibaba.executor import AlibabaExecutor
from app.collectors.alibaba.parser import extract_specs, extract_title, parse_offer
from app.collectors.alibaba.workspace import CollectWorkspace
from app.models import Base, Goods

FIXTURES = Path(__file__).parent / "fixtures" / "alibaba"

# 真实页面上人工核对过的期望值（改页面结构导致这里红，就是 parser 该修了）
CASES = {
    "623375582529": {
        "title": "内外304不锈钢保温杯 异形可乐瓶户外运动水杯车载不锈钢小口瓶",
        "price_cent": 1540,
        "image_count": 8,
        "category": "保温杯",
        "leaf_category_id": "1043766",
        "supplier": "永康市迎庆杯业有限公司",
        "stock": 1857,
        "specs": {"颜色": "不锈钢本色", "容量": "550ml"},
    },
    "1053709411360": {
        "title": "5A大电流双头Type-C快充数据线 兼容全系列手机通用可传输闪充线",
        "price_cent": 250,
        "image_count": 5,
        "category": "手机数据线",
        "leaf_category_id": "1034008",
        "supplier": "东莞市益连接科技有限公司",
        "stock": 10,
        "specs": {"型号": "type-c对type-c数据线 3.5粗1米", "颜色": "白色"},
    },
}


def read_fixture(offer_id: str) -> str:
    return (FIXTURES / f"detail_{offer_id}.html").read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _FakeImageHttp:
    """图片下载走工作区，需要 http.get；测试不真连 CDN。"""

    async def get(self, url, **kwargs):
        return SimpleNamespace(content=b"", status_code=200, raise_for_status=lambda: None)

    async def aclose(self):
        pass


@pytest.mark.parametrize("offer_id", sorted(CASES))
def test_parse_offer_on_real_page(offer_id):
    exp = CASES[offer_id]
    sp = parse_offer(read_fixture(offer_id), offer_id)

    assert sp.source == "1688"
    assert sp.source_offer_id == offer_id
    assert sp.title == exp["title"]
    assert sp.price_cent == exp["price_cent"]
    # 富文本详情在 itemcdn 上另拉，本页不抓（多一次请求），只记地址
    assert sp.description is None
    assert len(sp.image_urls) == exp["image_count"]
    assert sp.raw["category"] == exp["category"]
    assert sp.raw["leaf_category_id"] == exp["leaf_category_id"]
    assert sp.raw["supplier"] == exp["supplier"]
    assert sp.raw["stock"] == exp["stock"]
    assert sp.raw["specs"] == exp["specs"]
    assert sp.raw["detail_url"].startswith("https://itemcdn.tmall.com/")
    assert sp.raw["url"] == f"https://detail.1688.com/offer/{offer_id}.html"


@pytest.mark.parametrize("offer_id", sorted(CASES))
def test_main_images_are_the_offer_own_pictures(offer_id):
    """首图必须是商品主图。曾经取到店铺 banner（2015 年的 cms/upload PNG）——推荐位/
    banner 的 URL 结构不同，这条断言就是那道闸。"""
    urls = parse_offer(read_fixture(offer_id), offer_id).image_urls
    assert urls == list(dict.fromkeys(urls))  # 无重复
    for u in urls:
        assert u.startswith("https://cbu")
        assert "/img/ibank/" in u
        assert "cms/upload" not in u


def test_price_is_yuan_not_cent():
    """页面写 2.5 元 = 250 分。旧实现把纯数字串当已经是分，2.5 会变成 2 分成本、
    加价后以 3 分上架——不报错，只是每单亏钱。"""
    assert extract_title('{"subject":"测试标题"}') == "测试标题"
    html = '{"skuPriceScale":"2.50-4.00","price":"9.90"}'
    assert parser_mod.extract_price_cent(html) == 250


def test_specs_missing_returns_none():
    assert extract_specs("<html>没有销售属性</html>") is None


def test_garbage_page_yields_empty_fields():
    """风控壳 / 改版页：字段一律空，不能抛异常。executor 以「有没有标题」判 failed
    并把整页存成 blocked 件，所以这里必须是空值而不是异常。"""
    shell = read_fixture("623375582529")[:2355]
    sp = parse_offer("<html><body>punish</body></html>", "000")
    assert sp.title == ""
    assert sp.price_cent == 0
    assert sp.image_urls is None
    assert parse_offer(shell, "000").title == ""


@pytest.mark.asyncio
async def test_executor_collects_real_page_without_stubbing_parser(session_factory, monkeypatch, tmp_path):
    """采集链路跑真 parser（只把网络掐掉）：入库字段来自真实页面，且停在 SOURCED。

    这条是 M1 的收口断言——上一轮 executor 单测把 parse_offer 整个 patch 掉了，
    所以 parser 早就字段全空而无人察觉。
    """
    monkeypatch.setattr(
        AlibabaExecutor, "_fetch_detail", lambda self, oid: _resolved(read_fixture(oid))
    )
    workspace = CollectWorkspace(tmp_path)
    executor = AlibabaExecutor(
        http=_FakeImageHttp(), request_delay_range=(0, 0), workspace=workspace
    )
    async with session_factory() as db:
        result = await executor.collect_by_offer_ids(db, 1, ["623375582529"])
    await executor.aclose()

    assert result["added"] == 1 and result["failed"] == 0
    async with session_factory() as db:
        goods = (await db.execute(select(Goods))).scalars().one()
    assert goods.pool_status == "SOURCED"
    assert goods.title == CASES["623375582529"]["title"]  # clean_title 无营销词可去，原样
    assert goods.source_price_cent == 1540
    assert goods.price_cent == 1990  # 15.4 元 × 1.3 = 20.02 → 取整 −0.1
    assert goods.raw_payload["supplier"] == "永康市迎庆杯业有限公司"
    assert (workspace.offer_dir(result["batch_id"], "623375582529") / "product.json").is_file()


async def _resolved(html):
    return html
