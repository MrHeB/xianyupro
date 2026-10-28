"""M2 风险扫描测试（§7.4 六道闸门 + §7.5 虚拟品两分类）。

覆盖重点不是「能不能报错」，而是**不误杀**：本项目所有实物文案都要写「超级快充」
「一次性」「质量保证」，裸词匹配会把整批商品判成违规然后卡住放行，等于链路自杀。
"""

import pytest

from app.ai.risk_scan import NOT_COVERED, rewrite_terms, run_risk_scan


def _product(title="1米 Type-C 快充线 全新现货", desc=None, *, price_cent=1990,
             cost_cent=1000, images=None, candidates=None, delivery="包邮"):
    return {
        "collect": {
            "offer_id": "728001",
            "source_price_cent": cost_cent,
            "url": "https://detail.1688.com/offer/728001.html",
            "image_urls": images if images is not None else ["https://cbu01.alicdn.com/a.jpg"],
        },
        "decision": {
            "title": title,
            "desc": desc if desc is not None else (
                "成色与规格：全新，接口 Type-C，长度 1 米。\n"
                "适合场景：日常给手机补电，家里和办公室各放一根。\n"
                "发货与售后：以所选快递为准，问题可站内沟通。"
            ),
            "price_cent": price_cent,
            "delivery": delivery,
            "title_candidates": candidates or [title, title, title],
        },
        "media": {"local_images": []},
    }


def _codes(result):
    return {h["code"] for h in result["blockers"]}, {h["code"] for h in result["warnings"]}


# ---------- 基线：不该误报 ----------

def test_clean_physical_copy_passes():
    result = run_risk_scan(_product())
    assert result["passed"] is True, result["blockers"]
    assert result["blockers"] == []
    assert result["image_count_ok"] is True


@pytest.mark.parametrize("title,desc", [
    ("1米 Type-C 超级快充线 全新", "支持超级快充，华为小米都能用。"),
    ("最新款一次性雨衣 独立包装", "全新未使用，独立包装，出门带着方便。"),
    ("质量保证 type-c 快充线 1米", "全新现货，做工扎实，日常充电够用。"),
    ("中央空调清洗剂 家用", "全新未拆，家用制冷系统清洁用。"),
])
def test_polysemous_words_are_not_blocked(title, desc):
    """多义词降级为 warn 的四个实例：超级/一次性/保证/最新/中央空调——都不该拦住整批放行。"""
    blockers, _ = _codes(run_risk_scan(_product(title=title, desc=desc)))
    assert blockers == set(), blockers


@pytest.mark.parametrize("desc", [
    "全新菜刀一把，厨房用。",
    "儿童玩具水枪，夏天玩水。",
    "水果刀收纳架，厨房用。",
])
def test_kitchen_and_toy_blade_words_pass(desc):
    """违禁品只匹配管制形态，裸「刀/枪」不能一刀切。"""
    blockers, _ = _codes(run_risk_scan(_product(title="厨房小件 全新", desc=desc)))
    assert "prohibited_goods" not in blockers


@pytest.mark.parametrize("desc", [
    "仿真枪造型水弹玩具，全新。",
    "这把匕首是收藏摆件。",
    "出售管制刀具一套。",
])
def test_prohibited_goods_block(desc):
    blockers, _ = _codes(run_risk_scan(_product(title="收藏摆件 全新", desc=desc)))
    assert "prohibited_goods" in blockers


# ---------- 绝对化 / 虚假促销 ----------

def test_absolutism_blocks_and_offers_replacement():
    result = run_risk_scan(_product(title="Type-C 快充线 全网最低价 完美", desc="完美成色，第一选择，绝对好用。"))
    assert result["passed"] is False
    assert result["absolutism_words"]
    hit = next(h for h in result["blockers"] if h["term"] == "完美")
    assert hit["replacement"] == "成色极佳 / 无可见瑕疵"  # 照搬 forbidden-words 替换建议表


def test_baica_price_title_block_desc_warn():
    """红线区分面：白菜价在标题算违规、在描述只是提醒（forbidden-words.md 原文如此）。"""
    b, _ = _codes(run_risk_scan(_product(title="1米数据线 白菜价出")))
    assert "fake_promo" in b
    _, w = _codes(run_risk_scan(_product(desc="这个价格是白菜价了，成色全新，接口 Type-C。")))
    assert "fake_promo" in w


# ---------- 外联 / 站外 ----------

@pytest.mark.parametrize("desc", [
    "细节可以加微信详聊哈。",
    "有意者扣扣我，企鹅号在线。",
    "走支付宝更便宜，直接转账就行。",
    "联系电话 13800001111，随时找我。",
    "钉钉上说要快，那边聊。",
])
def test_off_platform_words_block(desc):
    result = run_risk_scan(_product(desc=desc))
    assert result["passed"] is False
    assert result["external_links"]


def test_in_site_private_chat_is_warn_not_block():
    """「站内私聊」「支付宝担保」本身合法，硬拦会把正常文案全打死。"""
    blockers, warnings = _codes(run_risk_scan(
        _product(desc="成色全新，规格 1 米，细节可以站内私信我，付款走闲鱼担保的支付宝。")))
    assert "off_platform_contact" not in blockers
    assert "off_platform_contact_soft" in warnings


def test_number_without_phone_shape_does_not_block():
    """数字串只在「手机号形状」时报，普通型号/规格数字不该误杀。"""
    blockers, _ = _codes(run_risk_scan(
        _product(title="数据线 1米2米", desc="支持 66W 快充，型号 A1380，库存 1200 件，全新未拆。")))
    assert "contact_number" not in blockers


def test_phone_shaped_number_blocks_even_without_contact_word():
    """external-contact-keywords.md 扫描顺序第 3 步：号码提取独立于上下文。"""
    blockers, _ = _codes(run_risk_scan(
        _product(desc="全新 1 米快充线一批，具体数字 13800001111 库存充足。")))
    assert "contact_number" in blockers


def test_physical_external_link_blocks():
    result = run_risk_scan(_product(desc="成色全新，详细参数看这里 https://pan.quark.cn/s/abc123 自取。"))
    assert result["passed"] is False
    assert "off_platform_link" in _codes(result)[0]
    assert result["delivery_links"] == []


# ---------- 虚拟商品（§7.5 两分类） ----------

VIRTUAL_DESC_OK = ("交付形式：夸克网盘链接，拍下后自动发送，秒发不等待。\n"
                  "使用方式：在线打开链接即可，有效期 365 天内可重复下载。\n"
                  "边界：虚拟内容一经发送不支持无理由退换。")


def test_virtual_delivery_link_is_allowed_not_blocked():
    """§7.5：品类 A 的交付物本身就是网盘链接，一刀切会把虚拟品全判违规。"""
    result = run_risk_scan(
        _product(title="Python 入门教程 完整版", desc=VIRTUAL_DESC_OK + "\nhttps://pan.quark.cn/s/abc123",
                 delivery="无需邮寄"),
        goods_kind="virtual",
    )
    assert result["passed"] is True, result["blockers"]
    assert result["delivery_links"] == ["https://pan.quark.cn/s/abc123"]
    assert result["external_links"] == []


def test_virtual_unknown_host_link_blocks():
    """网盘域名白名单之外不自动放行：宁判 NEEDS_MANUAL_CHECK，不当引流放过。"""
    result = run_risk_scan(
        _product(title="Python 教程", desc=VIRTUAL_DESC_OK + "\nhttp://ka-some-shop.com/p/1",
                 delivery="无需邮寄"),
        goods_kind="virtual",
    )
    assert "off_platform_link" in _codes(result)[0]


def test_virtual_mailing_words_block_but_auto_delivery_ok():
    blockers, warnings = _codes(run_risk_scan(
        _product(title="教程一份", desc="拍下后快递发出，包装完好，运费包邮，尽快发货。",
                 delivery="无需邮寄"),
        goods_kind="virtual"))
    assert blockers & {"virtual_mailing_words", "delivery_kind_mismatch"} == {"virtual_mailing_words"}
    assert "virtual_mailing_soft" in warnings  # 裸「发货」只 warn
    # 「自动发货/秒发」是虚拟品正当表述，不该被裸「发货」规则打死
    clean = run_risk_scan(_product(title="教程一份", desc=VIRTUAL_DESC_OK, delivery="无需邮寄"),
                          goods_kind="virtual")
    assert "virtual_mailing_soft" not in {h["code"] for h in clean["warnings"]}


def test_virtual_requires_no_shipping_delivery():
    blockers, _ = _codes(run_risk_scan(
        _product(title="教程一份", desc=VIRTUAL_DESC_OK, delivery="包邮"), goods_kind="virtual"))
    assert "delivery_kind_mismatch" in blockers


def test_virtual_missing_disclosure_is_manual_check_not_veto():
    """缺交付形式/有效期/退换边界 → warn + NEEDS_MANUAL_CHECK，不直接否决（手册 §3 末 criteria 写法）。"""
    result = run_risk_scan(
        _product(title="Python 教程 完整版", desc="内容涵盖基础语法与实战案例，站内沟通即可。",
                 delivery="无需邮寄"),
        goods_kind="virtual",
    )
    assert result["passed"] is True
    manual = [h for h in result["warnings"] if h.get("needs_manual_check")]
    assert {h["code"] for h in manual} == {"missing_delivery_form", "missing_validity",
                                           "missing_refund_boundary"}


def test_required_disclosure_does_not_self_trigger():
    """§7.5 强制要求的「不支持无理由退换」披露，不该被「误导承诺」规则自己判违规。"""
    result = run_risk_scan(_product(title="教程一份", desc=VIRTUAL_DESC_OK, delivery="无需邮寄"),
                           goods_kind="virtual")
    assert result["passed"] is True
    assert "misleading_promise" not in {h["code"] for h in result["warnings"]}


@pytest.mark.parametrize("desc", [
    "全网热播剧盗版资源，网盘直接发。",
    "影视会员账号出租，可代登。",
    "游戏外挂辅助，稳定不掉线。",
    "比特币矿机收益代充。",
    "学历代办，办证刻章都行。",
])
def test_virtual_forbidden_category_vetoes(desc):
    """一票否决品类：实物卖盗版资源同样违规，故两种 goods_kind 都拦。"""
    for kind in ("physical", "virtual"):
        blockers, _ = _codes(run_risk_scan(_product(desc=desc), goods_kind=kind))
        assert "virtual_forbidden_category" in blockers, (desc, kind)


# ---------- 货源话术 / 虚构经历（§7.4 / §6） ----------

@pytest.mark.parametrize("desc", [
    "支持一件代发，厂家直销，批发价供货。",
    "一手货源，源头工厂，欢迎代理加盟招代理。",
])
def test_source_leak_blocks(desc):
    blockers, _ = _codes(run_risk_scan(_product(desc=desc)))
    assert "source_leak" in blockers


@pytest.mark.parametrize("desc", [
    "亲测好用，我用过一段时间，搬家出闲置。",
])
def test_invented_experience_blocks(desc):
    blockers, _ = _codes(run_risk_scan(_product(desc=desc)))
    assert "invented_experience" in blockers


# ---------- 结构与价格（红线 3 / 4 / 5） ----------

def test_title_length_and_desc_length():
    assert "title_too_long" in _codes(run_risk_scan(_product(title="字" * 31)))[0]
    assert "title_missing" in _codes(run_risk_scan(_product(title="   ")))[0]
    assert "title_no_spec" in _codes(run_risk_scan(_product(title="全新数据线现货")))[1]
    assert "desc_short" in _codes(run_risk_scan(_product(desc="全新数据线。")))[1]


@pytest.mark.parametrize("count,expect", [
    (0, "image_missing"),
    (2, "image_too_few"),
    (10, "image_count_over"),
])
def test_image_count_gates(count, expect):
    urls = [f"https://cbu01.alicdn.com/{i}.jpg" for i in range(count)]
    blockers, warnings = _codes(run_risk_scan(_product(images=urls)))
    assert expect in (blockers | warnings)
    assert run_risk_scan(_product(images=urls))["image_count_ok"] is (0 < count <= 9)


def test_price_below_cost_blocks():
    blockers, _ = _codes(run_risk_scan(_product(price_cent=800, cost_cent=1000)))
    assert "price_below_cost" in blockers
    blockers, warnings = _codes(run_risk_scan(_product(price_cent=1100, cost_cent=1000)))
    assert "price_below_cost" not in blockers
    assert "price_margin_thin" in warnings


# ---------- 候选标题：未选中的降为 warn ----------

def test_unselected_candidate_hit_is_warn():
    result = run_risk_scan(_product(candidates=[
        "1米 Type-C 快充线 全新", "完美品质数据线", "绝对正品数据线"]))
    assert result["passed"] is True
    assert any("unused_candidate" in h["code"] for h in result["warnings"])


# ---------- 循环控制：只重跑「文案能救」的违规 ----------

def test_rewrite_terms_skips_structural_blockers():
    result = run_risk_scan(_product(title="全网最低价数据线", images=[]))
    terms = rewrite_terms(result)
    assert "全网最低" in terms  # 文案可改写
    assert not any(t == "0" for t in terms)  # 缺图不靠改写解决


def test_scan_reports_what_it_cannot_cover():
    """诚实自报：类目偏差、降价改标等要 DB/OCR 的项不静默放过。"""
    result = run_risk_scan(_product())
    assert NOT_COVERED and all(result["not_covered"])
    assert any("category_recommend" in line for line in result["not_covered"])


def test_result_is_json_serialisable():
    import json

    json.dumps(run_risk_scan(_product()), ensure_ascii=False)  # product.json 落盘用
