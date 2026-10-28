"""1688 详情页解析器。

输入：详情页 HTML（`https://detail.1688.com/offer/{offer_id}.html`，必须带 1688 货源账号
Cookie——匿名只拿得到 2.3KB 的 x5sec 壳，见 executor.is_risk_shell）。
输出：`SourcedProduct`（品类 B 商品池 SOURCED 行所需全部字段）。

2026-09-19 用 5 份真实页面重写。整份商品 JSON 内嵌在
    window.context = (function(b,d){...})(window.contextPath, {"result":{"data":{...}}});
的第二个实参里，两种模板（`/default` 大市场 4 份、`/processing` 加工定制 1 份）皆如此；
旧实现认的 `__INIT_DATA__` / `runParams` / `"offer":{…},"supplier"` 五条路径**全部落空**，
于是解析结果一律 title='' / price 0 / 无图——采集链在那之前是空转的。

为什么不截出那个对象再 json.loads：对象里混了 `"skuWeight":{6261554607231:0.272,…}`
这种未加引号的数字键（JS 对象字面量），整体解析必失败。所以按字段定向提取。

价格单位：页面一律「元」字符串（`skuPriceScale` 形如 `13.00-23.00` 或 `￥2.5-￥4.0`），
统一转分。不再像旧实现那样"纯数字串当作已是分"——那样遇到 `"13"` 会算成 1 分。
"""

import json
import logging
import re
from typing import Optional

from app.collectors.base import SourcedProduct

logger = logging.getLogger(__name__)

DETAIL_URL_TEMPLATE = "https://detail.1688.com/offer/{offer_id}.html"

_SUBJECT_RE = re.compile(r'"subject"\s*:\s*"((?:[^"\\]|\\.)+)"')
# 阶梯价区间，取首段 = 起批价（成本价下界，定价三级链的第一级证据就靠它）
_PRICE_SCALE_RE = re.compile(r'"skuPriceScale"\s*:\s*"([^"]{1,60})"')
_PRICE_RE = re.compile(r'"price"\s*:\s*"?(\d+(?:\.\d{1,2})?)')
_NUMBER_RE = re.compile(r"\d+(?:\.\d{1,2})?")

_IMAGES_BLOCK_RE = re.compile(r'"images"\s*:\s*\[(.{0,20000}?)\]', re.S)
# 主图：对象里同时有相对 imageURI 和绝对 fullPathImageURI，取后者，省掉拼域名这一步
# （也是唯一稳定的那个键——images 数组里还嵌着别的数组，按块截会被第一个 `]` 截断）。
_MAIN_IMAGE_RE = re.compile(r'"fullPathImageURI"\s*:\s*"(https?://[^"]+)"')
_IMAGE_URI_RE = re.compile(r'"imageURI"\s*:\s*"([^"]+)"')
_CDN_BASE = "https://cbu01.alicdn.com/"
# 兜底：整页扫 1688 图片 CDN（cbu01/cbu02…），结构再变也不至于无图
_IMAGE_URL_RE = re.compile(
    r'https?://[a-z0-9.-]*cbu\d*\.alicdn\.com/[^\s"\'<>()]+\.(?:jpg|jpeg|png|webp)', re.I
)

# 类目：真实页面上只有 leafCategoryName（"保温杯"/"iPhone保护套"），没有 topCategoryName
_LEAF_CATEGORY_NAME_RE = re.compile(r'"leafCategoryName"\s*:\s*"((?:[^"\\]|\\.)+)"')
_LEAF_CATEGORY_RE = re.compile(r'"leafCategoryId"\s*:\s*"?(\d+)')
_COMPANY_RE = re.compile(r'"companyName"\s*:\s*"((?:[^"\\]|\\.)+)"')
_CAN_BOOK_RE = re.compile(r'"canBookCount"\s*:\s*"?(\d+)')
# 富文本详情不在这页 HTML 里，而在 itemcdn 上另拉一个文件；只记地址不抓（多一次请求）
_DETAIL_URL_RE = re.compile(r'"detailUrl"\s*:\s*"(https?://itemcdn\.[^"]+)"')
# 规格：`"prop":"颜色","value":[{"name":"…"},…]`
_SKU_PROP_RE = re.compile(
    r'"prop"\s*:\s*"((?:[^"\\]|\\.)+)"\s*,\s*"value"\s*:\s*\[(.{0,4000}?)\]', re.S
)
_PROP_VALUE_RE = re.compile(r'"name"\s*:\s*"((?:[^"\\]|\\.)+)"')

_MAX_IMAGES = 8  # 闲鱼首图上限，多采无用


def _unescape(raw: str) -> str:
    r"""页面里嵌的是 JS 字符串字面量，`\uXXXX` / `\"` 要还原成中文原文。"""
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def _yuan_to_cent(text: str) -> int:
    return int(round(float(text) * 100))


def _dedupe(urls: list[str]) -> list[str]:
    """按首次出现去重（同一张图会以主图/详情图重复挂多次）。"""
    return list(dict.fromkeys(urls))


def extract_title(html: str) -> str:
    m = _SUBJECT_RE.search(html)
    return _unescape(m.group(1)).strip() if m else ""


def extract_price_cent(html: str) -> int:
    """起批价（分）。拿不到 skuPriceScale 时退回页面里第一个 price。"""
    m = _PRICE_SCALE_RE.search(html)
    if m:
        n = _NUMBER_RE.search(m.group(1))
        if n:
            return _yuan_to_cent(n.group())
    m = _PRICE_RE.search(html)
    return _yuan_to_cent(m.group(1)) if m else 0


def extract_image_urls(html: str) -> Optional[list[str]]:
    """主图列表（≤8）。

    从主商品标题往后取——页面上还有推荐位/详情图里的其它 fullPathImageURI，
    不锚定就会把店铺 banner 图当主图（实测踩过：首图变成一张 2015 年的 cms/upload PNG）。
    """
    anchor = html.find('"subject"')
    urls = _MAIN_IMAGE_RE.findall(html, max(anchor, 0)) or _MAIN_IMAGE_RE.findall(html)
    if not urls:
        block = _IMAGES_BLOCK_RE.search(html)
        if block:
            urls = [
                u if u.startswith("http") else _CDN_BASE + u.lstrip("/")
                for u in _IMAGE_URI_RE.findall(block.group(1))
            ]
    if not urls:
        urls = _IMAGE_URL_RE.findall(html)
    return _dedupe(urls)[:_MAX_IMAGES] or None


def extract_specs(html: str) -> Optional[dict[str, str]]:
    """销售属性（型号/颜色…）→ 首个可选值，给文案阶段当规格素材。"""
    specs: dict[str, str] = {}
    for m in _SKU_PROP_RE.finditer(html):
        name = _unescape(m.group(1)).strip()
        vm = _PROP_VALUE_RE.search(m.group(2))
        if name and name not in specs and vm:
            specs[name] = _unescape(vm.group(1)).strip()
        if len(specs) >= 10:
            break
    return specs or None


def _company(html: str) -> Optional[str]:
    """商家名。页面上会出现多个（含推荐位里别人的），所以优先取主商品标题之后的第一个。"""
    anchor = html.find('"subject"')
    matches = list(_COMPANY_RE.finditer(html, max(anchor, 0)))
    if not matches:
        matches = list(_COMPANY_RE.finditer(html))
    return _unescape(matches[0].group(1)).strip() if matches else None


def parse_offer(html: str, offer_id: str) -> SourcedProduct:
    """解析 1688 详情页 HTML → SourcedProduct。

    字段拿不到就留空，绝不抛异常：上层 executor 以"有没有标题"判采集成败，
    并把整页存成 blocked 件供人工核对（解析失效要看得见，不能被异常吞掉）。
    """
    stock = _CAN_BOOK_RE.search(html)
    leaf = _LEAF_CATEGORY_RE.search(html)
    category = _LEAF_CATEGORY_NAME_RE.search(html)
    detail = _DETAIL_URL_RE.search(html)

    return SourcedProduct(
        source="1688",
        source_offer_id=offer_id,
        title=extract_title(html),
        # 富文本详情不在本页（在 itemcdn 上），留 None，地址进 raw 供后续按需取
        description=None,
        price_cent=extract_price_cent(html),
        image_urls=extract_image_urls(html),
        raw={
            "specs": extract_specs(html),
            "stock": int(stock.group(1)) if stock else None,
            "category": _unescape(category.group(1)) if category else None,
            "leaf_category_id": leaf.group(1) if leaf else None,
            "supplier": _company(html),
            "detail_url": detail.group(1) if detail else None,
            "url": DETAIL_URL_TEMPLATE.format(offer_id=offer_id),
        },
    )
