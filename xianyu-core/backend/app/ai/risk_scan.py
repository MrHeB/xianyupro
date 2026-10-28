"""发布前风险扫描（设计文档 §7.4 / §7.5，M2 后半）：`run_risk_scan` 工具本体。

词表与红线来源 = `reference/goofish-cli/skills/goofish-risk-guard`（git `7d6a162c`，只读）：
  - `references/forbidden-words.md`           → 绝对化 / 医疗 / 政治 / 擦边 / 违禁品 / 虚假数据 + 替换建议表
  - `references/external-contact-keywords.md` → 外联词、号码正则、灰色区组合判定
  - `references/publish-red-lines.md`         → 行为红线（图片 / 描述 / 价格 / 类目）
自研补充只有三处，且都出自本项目文档：§7.5 虚拟品一票否决品类、§7.5 禁邮寄话术、
§7.4+§6 的货源话术与虚构使用经历。

三条实现约束：
1. **纯规则、零 LLM、零 IO**（风控手册 §1.7 规则优先；面板第 4 步必须可离线复现）；
2. **只报不改**：结果交调用方去改文案，对齐 risk-guard skill 自身定位（"不直接替用户改文案"）；
3. **覆盖不到项显式进 `not_covered` 自报**，不静默通过——降级要带标记。

命中处置分级（§7.5）：站外链分两类，不一刀切。
已知网盘交付域名 + 虚拟商品 → `delivery_links`（放行）；其余一律 `off_platform_link`（拦截）。
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

GoodsKind = Literal["physical", "virtual"]

RULES_SOURCE = "reference/goofish-cli@7d6a162c · skills/goofish-risk-guard"

BLOCK = "block"
WARN = "warn"

# §8.4 节点 4 / §6 步骤④：改写只能救「文案里出现了不该出现的词」。
# 下列 code 与措辞无关（缺图 / 超长 / 价格 / 交付方式），重跑文案是白费 LLM 调用。
NOT_COPY_FIXABLE = frozenset({
    "title_missing", "title_too_long", "title_no_spec",
    "desc_missing", "desc_short",
    "image_missing", "image_count_over", "image_too_few",
    "price_below_cost", "price_margin_thin", "price_too_low",
    "delivery_kind_mismatch",
})

# 未覆盖项（红线要求但本扫描器无数据源）：留给 M4 批量放行端点与发布四步。
NOT_COVERED = [
    "category_mismatch：需发布第 2 步 `category_recommend` 返回的 catId，红线 2 明说「不要靠猜」",
    "reprice_then_relabel（红线 1 降价改标）：需在售历史 GoodsEditSnapshot，由 M4 批量放行时比对",
    "first_image_1x1 / 水印 / 图中二维码（红线 3）：需读图与 OCR",
    "duplicate_listing / daily_publish_cap（红线 6 / 红线 9）：需账号维度历史",
    "resurrect_delisted（红线 7 复活下架商品）：需下架记录",
]

# 闲鱼单商品 9 图上限（红线 3）；<3 是软性建议（红线 3 软性 / SKILL 闸门 1 warnings）
MAX_IMAGES = 9
MIN_IMAGES_SUGGEST = 3
TITLE_MAX = 30          # §7.4 标题 ≤ 30 字
DESC_MIN = 30           # 红线 4 描述 < 30 字

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s\u4e00-\u9fff'\"<>()（）]+", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_QQ_CTX_RE = re.compile(r"[qｑQ]\s?[qｑQ]?\s?(?<!\d)[1-9]\d{4,11}(?!\d)")
_WECHAT_ID_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][-_A-Za-z0-9]{5,19}(?![A-Za-z0-9])")

# 虚拟商品交付物的正当落点：网盘 / 云盘系（§7.5「品类 A 的交付物本身就是网盘链接，属正当需求」）。
# 名单外的 http 链接即便挂在虚拟商品下也拦，理由：发卡/引流域各样，宁判 NEEDS_MANUAL_CHECK。
DELIVERY_HOSTS = (
    "pan.quark.cn", "quark.cn",
    "pan.baidu.com", "yun.baidu.com",
    "alipan.com", "aliyundrive.com",
    "123pan.com", "123684.com", "123865.com",
    "lanzou.com", "lanzoux.com", "lanzous.com", "lanzovn.com",
    "pan.xunlei.com", "myqcloud.com", "weiyun.com",
    "cloud.189.cn", "yun.139.com", "ctfile.com",
    "drive.uc.cn",
)


def _p(*words: str) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)


# ---------------- 替换建议（照搬 forbidden-words.md「替换建议表」） ----------------

REPLACEMENTS = {
    "全网最低": "同类市价偏低 / 价格友好",
    "完美": "成色极佳 / 无可见瑕疵",
    "顶级": "高配 / 优质",
    "百分百": "购自 XX，有小票/发票",
    "100%": "购自 XX，有小票/发票",
    "独家": "较少见 / 小众",
    "第一": "推荐选择 / 热门款",
    "最后一件": "现货一件",
}


@dataclass(frozen=True)
class Rule:
    code: str
    severity: str
    pattern: re.Pattern[str]
    message: str
    fields: tuple[str, ...] = ("title", "desc")
    kinds: tuple[GoodsKind, ...] = ("physical", "virtual")


# ---------------- 词表（分类沿用 forbidden-words.md 六类 + 二类外联） ----------------

ABSOLUTISM = Rule(
    code="absolutism",
    severity=BLOCK,
    message="绝对化用语（广告法红线，必删）",
    pattern=_p(
        "最佳", "最好", "最优", "最优秀", "最大", "最小", "最高", "最低", "最便宜", "最贵",
        "最便利", "最先进", "最流行", "最时尚", "最潮", "最牛", "最火",
        "顶级", "顶尖", "顶配", "极品", "极致", "绝佳", "完美", "无敌", "王牌", "王者",
        "独家", "独一无二",
        "第一", "唯一", "No.1", "Top1", "全网第一", "销量第一", "排名第一", "行业第一",
        "全网最低", "全国最低", "全球最低", "全网首发", "全网最火", "全网最便宜",
        "国家级", "央视推荐", "中央电视台", "人民日报推荐", "行业领袖",
        "绝对", "永不", "决不", "包治", "根治", "百分百", "100%",
        "亏本", "亏本甩卖", "亏本清仓", "跳楼价", "永久", "永远",
    ),
)
# 多义词：降级为 warn 而不是 block，否则会误杀正常实物文案——
# 「超级快充」是数据线卖点、「质量保证」是常规表述、「一次性手套」是品名、「最新款」不是极限词。
ABSOLUTISM_SOFT = Rule(
    code="absolutism_soft",
    severity=WARN,
    message="疑似绝对化/夸大表述，建议换成可验证的客观描述",
    pattern=re.compile(r"最(?![新旧后终])|超级|彻底|保证|一次性", re.IGNORECASE),
)
FAKE_PROMO = Rule(
    code="fake_promo",
    severity=BLOCK,
    message="虚假促销词（红线 1：降价改标题会被判虚假促销）",
    pattern=_p("白菜价"),
    fields=("title",),
)
FAKE_PROMO_DESC = Rule(
    code="fake_promo",
    severity=WARN,
    message="描述里的促销价词，注意语境",
    pattern=_p("白菜价"),
    fields=("desc",),
)
MEDICAL = Rule(
    code="medical_claim",
    severity=BLOCK,
    message="医疗/保健功效承诺（无资质即违规）",
    pattern=_p("治愈", "根治", "疗效", "特效", "痊愈", "医治", "药到病除", "立竿见影",
               "神效", "奇效", "无副作用"),
)
MEDICAL_SOFT = Rule(
    code="medical_soft",
    severity=WARN,
    message="涉医疗词，确认是否为品名本身（如「治疗仪」）",
    pattern=_p("治疗", "纯天然"),
)
POLITICAL = Rule(
    code="political",
    severity=BLOCK,
    message="政治敏感词（闲鱼零容忍）",
    pattern=re.compile(
        "国务院|全国人大|人大代表|总理|主席|领导人|国家领导人|中央(?![空调扶净暖])",
        re.IGNORECASE,
    ),
)
PROHIBITED = Rule(
    code="prohibited_goods",
    severity=BLOCK,
    message="违禁/管制品（发布即下架）",
    # 只做管制形态，不匹配裸「枪/刀」：水枪、菜刀、水果刀在闲鱼可售。
    pattern=_p("手枪", "步枪", "猎枪", "气枪", "仿真枪", "钢珠枪", "电击枪", "弩箭", "弓弩",
               "匕首", "管制刀具", "管制", "弹簧刀", "三棱刀", "砍刀", "开山刀",
               "毒品", "冰毒", "易制爆", "易燃易爆", "烟花爆竹"),
)
VULGAR = Rule(
    code="vulgar",
    severity=BLOCK,
    message="擦边/涉未成年敏感词",
    pattern=_p("原味", "体液", "幼女", "萝莉", "lo娘"),
)
VULGAR_SOFT = Rule(
    code="vulgar_soft",
    severity=WARN,
    message="非服饰类目下慎用（部分情境敏感）",
    pattern=_p("JK", "少女"),
)
FALSE_DATA = Rule(
    code="false_data",
    severity=BLOCK,
    message="虚假数据（闲鱼不显示销量/库存，声称即误导）",
    pattern=re.compile(
        r"销量爆款|月销\s*\d+\s*[wW万千]|\d+\s*万\+|库存无限|永久供货|长期供货|全网销量",
        re.IGNORECASE,
    ),
)

# —— 二类：外联 / 诱导线下（封号红线）——
OFF_PLATFORM_CONTACT = Rule(
    code="off_platform_contact",
    severity=BLOCK,
    message="站外联系方式（封号红线：闲鱼担保交易是平台底线）",
    pattern=_p(
        "微信", "威信", "葳信", "维信", "围信", "薇信", "媺信", "溦信", "微X", "维❤", "微❤",
        "v信", "V信", "VX信", "加v", "加VX", "加V", "加微", "扣扣", "企鹅号", "Q号", "Q我",
        "钉钉", "飞书", "探探", "Telegram", "小飞机", "WhatsApp",
        "手机号", "座机", "移动号", "联通号", "留电话", "电话联系", "打我电话",
        "加我好友", "加一下我", "留号", "留联系", "联系方式", "发号",
        "走支付宝", "走微信", "线下交易", "线下付", "当面交易", "私下交易",
        "直接打款", "转账", "先付定金",
    ),
)
OFF_PLATFORM_CONTACT_SOFT = Rule(
    code="off_platform_contact_soft",
    severity=WARN,
    message="站内私聊/私信本身合法，但配合站外方式即触线；确认无引流意图",
    pattern=_p("私聊", "私信", "二维码", "扫码", "支付宝"),
)
OFF_PLATFORM_ABBREV = Rule(
    code="off_platform_contact",
    severity=BLOCK,
    message="站外联系方式缩写",
    pattern=re.compile(r"(?<![A-Za-z0-9])(?:vx|tg|qq|weixin|wechat)(?![A-Za-z0-9])", re.IGNORECASE),
)

SOURCE_LEAK = Rule(
    code="source_leak",
    severity=BLOCK,
    message="货源话术（§7.4：暴露 1688 供货身份，闲鱼判「职业卖家/批发」）",
    pattern=_p("一件代发", "代发", "厂家直销", "厂家直发", "工厂直销", "源头工厂",
               "一手货源", "货源", "批发", "批发价", "供应链", "OEM", "招代理"),
)
INVENTED_EXPERIENCE = Rule(
    code="invented_experience",
    severity=BLOCK,
    message="虚构个人闲置经历（§6 硬性约束 2：无凭据叙述禁止生成）",
    pattern=_p("亲测", "我用过", "自用", "本人实测", "亲自使用", "用过一段时间",
               "搬家出", "分手出", "出国急出", "闲置一年", "闲置多年"),
)
MISLEADING_PROMISE = Rule(
    code="misleading_promise",
    severity=WARN,
    message="误导性承诺（无凭证时不得写）",
    # 「无理由退换」要排除 §7.5 强制要求的否定式披露，否则自相矛盾。
    pattern=re.compile(
        "包真|假一赔十|假一赔三|正品保证|官方授权|官方售后|支持验货"
        "|(?<!不支持)(?<!不适用)无理由退换",
        re.IGNORECASE,
    ),
)

# —— §7.5 虚拟商品专用 ——
VIRTUAL_FORBIDDEN = Rule(
    code="virtual_forbidden_category",
    severity=BLOCK,
    message="虚拟品一票否决品类（§7.5）",
    pattern=_p("盗版", "枪版", "翻录", "全集资源", "网盘资源", "影视会员", "会员合租",
               "账号出租", "共享账号", "代登", "上号", "外挂", "辅助器", "破解版", "修改器",
               "代刷", "刷粉", "刷单", "虚拟货币", "比特币", "USDT", "代充", "黑卡",
               "办证", "假证", "刻章", "代考", "代写论文", "包过", "题库答案", "网课转卖",
               "付费课程转卖"),
)
VIRTUAL_MAILING = Rule(
    code="virtual_mailing_words",
    severity=BLOCK,
    message="虚拟商品禁邮寄话术（§7.5，统一用「拍下后自动发送 / 秒发」）",
    pattern=_p("快递", "物流", "邮寄", "包装", "包邮", "运费", "上门取件", "收货地址"),
    kinds=("virtual",),
)
VIRTUAL_MAILING_SOFT = Rule(
    code="virtual_mailing_soft",
    severity=WARN,
    message="虚拟品出现「发货」，确认是「自动发货」语义而非邮寄承诺",
    pattern=re.compile(r"(?<![自动秒在线即时])发货"),
    kinds=("virtual",),
    fields=("desc",),
)

# 交付形式 / 有效期 / 退换边界 三项缺失只标 NEEDS_MANUAL_CHECK 而不否决
# —— 搬 ai-goofish-monitor 的 criteria 写法（风控手册 §3 末、手册 §1.4）。
_VIRTUAL_DISCLOSURE = {
    "missing_delivery_form": ("卡密", "兑换码", "激活码", "网盘", "云盘", "链接", "文本", "电子", "自动发送", "秒发"),
    "missing_validity": ("有效期", "到期", "天内", "年有效", "使用期限", "长期有效"),
    "missing_refund_boundary": ("不支持无理由", "不退不换", "虚拟商品不退换", "一经发送", "售出不退"),
}


# ---------------- 命中收集 ----------------

class _Hits:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str, str]] = set()

    def add(self, code: str, severity: str, field: str, term: str, message: str, **extra: Any) -> None:
        key = (code, field, term.lower())
        if key in self._seen:
            return
        self._seen.add(key)
        hit: dict[str, Any] = {
            "code": code,
            "severity": severity,
            "field": field,
            "term": term,
            "message": message,
        }
        replacement = REPLACEMENTS.get(term)
        if replacement:
            hit["replacement"] = replacement
        hit.update(extra)
        self.items.append(hit)

    def of(self, severity: str) -> list[dict[str, Any]]:
        return [h for h in self.items if h["severity"] == severity]


def _scan_words(text: str, field: str, kind: GoodsKind, hits: _Hits) -> None:
    if not text:
        return
    for rule in (
        ABSOLUTISM, ABSOLUTISM_SOFT, FAKE_PROMO, FAKE_PROMO_DESC, MEDICAL, MEDICAL_SOFT,
        POLITICAL, PROHIBITED, VULGAR, VULGAR_SOFT, FALSE_DATA,
        OFF_PLATFORM_CONTACT, OFF_PLATFORM_CONTACT_SOFT, OFF_PLATFORM_ABBREV,
        SOURCE_LEAK, INVENTED_EXPERIENCE, MISLEADING_PROMISE,
        VIRTUAL_FORBIDDEN, VIRTUAL_MAILING, VIRTUAL_MAILING_SOFT,
    ):
        if kind not in rule.kinds or field not in rule.fields:
            continue
        for m in rule.pattern.finditer(text):
            term = m.group(0)
            if term.strip():
                hits.add(rule.code, rule.severity, field, term, rule.message)


def _classify_links(text: str, field: str, kind: GoodsKind, hits: _Hits,
                    delivery_links: list[str]) -> None:
    """§7.5 两分类：网盘交付链接放行、其余按引导脱离平台拦。"""
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip("，。,.;；")
        host = re.sub(r"^https?://|^www\.", "", url, flags=re.IGNORECASE).split("/")[0].lower()
        if kind == "virtual" and any(host == h or host.endswith("." + h) for h in DELIVERY_HOSTS):
            delivery_links.append(url)
            continue
        hits.add("off_platform_link", BLOCK, field, url,
                 "虚拟品已知网盘交付域名之外的站外链接（§7.5：放行的是交付物，不是引流）")


def _scan_contacts_numeric(text: str, field: str, hits: _Hits) -> None:
    for m in _PHONE_RE.finditer(text):
        hits.add("contact_number", BLOCK, field, m.group(0), "文案含手机号（外联红线硬数字特征）")
    if _QQ_CTX_RE.search(text):
        hits.add("contact_number", BLOCK, field, "Q+数字串", "文案含 QQ 号样式数字串")
    # 灰色区组合判定（external-contact-keywords.md 扫描顺序第 4 步）：
    # 字母数字下划线串只有在同段文案已出现联系方式语境时才升级为 block。
    if OFF_PLATFORM_CONTACT.pattern.search(text) or OFF_PLATFORM_ABBREV.pattern.search(text):
        stripped = re.sub(_URL_RE, " ", text)
        for m in _WECHAT_ID_RE.finditer(stripped):
            cand = m.group(0)
            if re.search(r"\d", cand) and re.search(r"[A-Za-z]", cand):
                hits.add("contact_number", BLOCK, field, cand, "联系方式语境下的微信 ID 样式串")


# ---------------- 主入口 ----------------

def run_risk_scan(product: dict, *, goods_kind: GoodsKind = "physical") -> dict:
    """扫 `product.json` 的 decision 段（§5 工具 5；§8.4 面板节点 4 的产物）。

    只读，不写盘；写回 `decision.risk_scan` 由 `decision.price_and_copy` 负责。
    人工改文案后必须重扫（M4 批量放行端点再扫一次）。
    """
    decision = product.get("decision") or {}
    collect = product.get("collect") or {}
    media = product.get("media") or {}

    title = str(decision.get("title") or "")
    desc = str(decision.get("desc") or "")
    fields = {"title": title, "desc": desc}

    hits = _Hits()
    delivery_links: list[str] = []
    for field, text in fields.items():
        _scan_words(text, field, goods_kind, hits)
        _classify_links(text, field, goods_kind, hits, delivery_links)
        _scan_contacts_numeric(text, field, hits)

    # 候选标题（未选用的两版）：命中降级为 warn，避免人工在面板换用时漏过，也不触发无谓改写。
    candidates = decision.get("title_candidates") or []
    for i, cand in enumerate(candidates[1:], start=2):
        cand_hits = _Hits()
        _scan_words(str(cand or ""), "title", goods_kind, cand_hits)
        for h in cand_hits.items:
            hits.add(f"{h['code']}_unused_candidate", WARN, f"title_candidates[{i}]", h["term"],
                     f"未选用的候选标题命中：{h['message']}")

    # —— 结构与合规硬指标 ——
    if not title.strip():
        hits.add("title_missing", BLOCK, "title", "", "标题为空，无法发布")
    elif len(title) > TITLE_MAX:
        hits.add("title_too_long", BLOCK, "title", title, f"标题 {len(title)} 字 > {TITLE_MAX}")
    elif not re.search(r"\d", title):
        hits.add("title_no_spec", WARN, "title", title, "标题缺核心规格（数字/参数），搜索权重低")

    if len(desc.strip()) < DESC_MIN:
        hits.add("desc_missing" if not desc.strip() else "desc_short",
                 BLOCK if not desc.strip() else WARN, "desc", desc[:30],
                 f"描述 < {DESC_MIN} 字（红线 4：搜索权重低、不进推荐池）")

    images = media.get("local_images") or collect.get("image_urls") or []
    image_count = len(images)
    if image_count == 0:
        hits.add("image_missing", BLOCK, "media", "0", "无图无法发布")
    elif image_count > MAX_IMAGES:
        hits.add("image_count_over", BLOCK, "media", str(image_count),
                 f"图片 {image_count} 张 > 闲鱼上限 {MAX_IMAGES}")
    elif image_count < MIN_IMAGES_SUGGEST:
        hits.add("image_too_few", WARN, "media", str(image_count),
                 f"图片 {image_count} 张 < {MIN_IMAGES_SUGGEST}（红线 3 软性建议）")

    # —— 价格偏离（红线 5）——
    cost = int(collect.get("source_price_cent") or 0)
    price = decision.get("price_cent")
    if price is not None:
        price = int(price)
        if price <= 100:
            hits.add("price_too_low", BLOCK, "price_cent", str(price),
                     "价格 ≤ 1 元，系统怀疑钓鱼（红线 5）")
        if cost > 0 and price < cost:
            hits.add("price_below_cost", BLOCK, "price_cent", str(price),
                     f"售价 {price} 分低于成本 {cost} 分，疑似录入错误（红线 5）")
        elif cost > 0 and price < round(cost * 1.15):
            hits.add("price_margin_thin", WARN, "price_cent", str(price),
                     f"售价低于成本×1.15，运费+售后准备吃不掉（§7.3 第 3 条）")

    # —— 交付方式与品类一致性（红线 2 类目偏差里唯一可静态判的一段）——
    delivery = str(decision.get("delivery") or "")
    if goods_kind == "virtual" and delivery != "无需邮寄":
        hits.add("delivery_kind_mismatch", BLOCK, "delivery", delivery,
                 "虚拟商品必须「无需邮寄」（发布侧 virtual 分支依此置位）")
    if goods_kind == "physical" and delivery == "无需邮寄":
        hits.add("delivery_kind_mismatch", WARN, "delivery", delivery,
                 "实物商品选「无需邮寄」，确认是否自提")

    # —— §7.5 虚拟品三项必写：缺失只标 NEEDS_MANUAL_CHECK，不否决 ——
    if goods_kind == "virtual":
        for code, markers in _VIRTUAL_DISCLOSURE.items():
            if not any(m in desc for m in markers):
                hits.add(code, WARN, "desc", "",
                         "虚拟商品文案未写清该项，需人工补（NEEDS_MANUAL_CHECK 而非否决）",
                         needs_manual_check=True)

    blockers = hits.of(BLOCK)
    absolutism = sorted({h["term"] for h in hits.items
                         if h["code"] in ("absolutism", "absolutism_soft", "fake_promo")})
    external = sorted({h["term"] for h in blockers
                       if h["code"] in ("off_platform_link", "off_platform_contact",
                                        "contact_number")})

    return {
        "passed": not blockers,
        "goods_kind": goods_kind,
        "rules_source": RULES_SOURCE,
        "scanned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "blockers": blockers,
        "warnings": hits.of(WARN),
        # §3.2 契约里的三个摘要键，面板与 product.json 对齐用
        "absolutism_words": absolutism,
        "external_links": external,
        "image_count_ok": 0 < image_count <= MAX_IMAGES,
        "delivery_links": sorted(set(delivery_links)),
        "not_covered": NOT_COVERED,
    }


def rewrite_terms(scan_result: dict) -> list[str]:
    """本轮该交给文案重写的违规词（§8.4 节点 4 → 回第 3 步）。

    返回空表示「再改文案也救不了」，调用方应停止改写轮次。
    """
    terms = {
        h["term"] for h in scan_result.get("blockers") or []
        if h["code"] not in NOT_COPY_FIXABLE and h["term"].strip()
    }
    return sorted(terms)
