"""S1 关键词检索测试（app/collectors/alibaba/search_browser.py + executor.collect_by_keyword）。

两层各守一类回归：
- 纯函数层（GBK 编码 / ID 抽取 / 失败分类）：正则或编码改错就红；
- 控流层：假浏览器只替换 playwright 这层**网络边界**，`search_offers` 本体的分支
  （mtop 优先、DOM 回退、limit 截断、blocked 判定）全部真跑。

上次 parser 坏了三个月没人发现，就是因为单测把被测函数整个 patch 掉了。

真实接口样本已进 fixtures（2026-09-20 由 `COLLECT_DEBUG_DUMP_PATH` 开关 + 一次真号检索抓到，
脱敏后另存），所以下面既有"按结构手工构造"的用例（能精确控制分桶/去重/截断的边界），
也有"接口到底还长这样"的回归用例（见 `# 真实 mtop 响应样本` 一节）。

样本文件只证明**抓取那天**接口长这样；它防的是"改版没人发现"，不防"改版后仍能出货"。
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.collectors.alibaba import search_browser as sb
from app.collectors.alibaba.executor import AlibabaExecutor
from app.collectors.alibaba.workspace import CollectWorkspace
from app.collectors.base import RISK_SHELL_MARKERS
from app.models import Base, Goods

FIXTURES = Path(__file__).parent / "fixtures" / "alibaba"

# 真实页面上人工核对过的期望值（详情页 fixture，用于验检索→采集的交接）
DETAIL_OFFER = "623375582529"


def read_detail(offer_id: str) -> str:
    return (FIXTURES / f"detail_{offer_id}.html").read_text(encoding="utf-8")


def _resolved(v):
    async def _c():
        return v
    return _c()


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _FakeImageHttp:
    async def get(self, url, **kwargs):
        return SimpleNamespace(content=b"", status_code=200, raise_for_status=lambda: None)

    async def aclose(self):
        pass


# ------------------------------------------------------------------ 纯函数层


def test_search_url_encodes_keywords_as_gbk():
    """1688 搜索页只认 GBK 百分号编码；写成 UTF-8 会检索成乱码词、静默出别的货。"""
    url = sb.build_search_url("手机数据线", page=2)
    assert quote("手机数据线".encode("gbk", "ignore")) in url
    assert quote("手机数据线") not in url
    assert "beginPage=2" in url


def test_harvest_dedupes_across_patterns():
    """同一个 ID 会被多条正则各命中一次（DOM 链接 + JSON 字段），去重但不能丢。

    顺序按"模式优先"而非文档顺序：先 offer/\\d+\\.html，再 offerId，再 objectId。
    """
    payload = (
        '{"offerId":"623375582529"} offer/1053709411360.html '
        '"objectId":"623375582529" "offerId":700100200300'
    )
    assert sb.harvest_offer_ids(payload) == [
        "1053709411360", "623375582529", "700100200300",
    ]


def test_harvest_ignores_ids_out_of_digit_range():
    """6–20 位才算 offer_id；订单号/时间戳混进来会污染下游解析。

    第二例是回归点：早期实现没有 (?\\!\\d)，21 位数字串会被截前 20 位当成一个"真 ID"。
    """
    assert sb.harvest_offer_ids("offer/12345.html") == []
    assert sb.harvest_offer_ids('"offerId":"123456789012345678901"') == []
    assert sb.harvest_offer_ids('"objectId":12345') == []
    assert sb.harvest_offer_ids("") == []
    assert sb.harvest_offer_ids(None) == []


@pytest.mark.parametrize(
    "body, expected",
    [
        (f"<html>{RISK_SHELL_MARKERS[0]} x5secdata</html>", "risk_shell"),
        ("<html>请先登录 扫码登录</html>", "login_wall"),
        ("<html>没有找到相关货源</html>", "no_result"),
    ],
)
def test_classify_failure_distinguishes_causes(body, expected):
    """三种失败的处置动作完全相反（降频 / 重扫码 / 换词），不能合成一个"没结果"。"""
    assert sb.classify_failure(body) == expected


def test_search_blocked_carries_reason():
    exc = sb.SearchBlocked("login_wall", "detail")
    assert exc.reason == "login_wall"
    assert "login_wall" in str(exc)


# ------------------------------------------- 真实 mtop 响应样本（2026-09-20 真号抓取）

# 关键词「无线鼠标」，登录态货源账号，1 页，走的就是产品化那条路（默认 limit=20）。
# 抓法：前端「1688 采集」页填关键词 + 勾"只检索，不入库"，后端临时开
# `COLLECT_DEBUG_DUMP_PATH` 把响应体落盘，再用 `scripts/make_1688_search_fixture.py` 脱敏 +
# items 60→6 裁剪（脚本会跑一遍 `harvest_offer_ids` 自检并打印本清单，接口改版后可重跑）。
SEARCH_MTOP_FIXTURE = "search_mtop_wirelessrecommend.jsonp"

# 本次真号响应里前 6 张货源卡片的 ID（原样顺序）。**注意**：这是"接口给了哪 6 件"的断言，
# 不是"页面上第 1 张卡片是这件"的断言——后者要人眼看，抓样本那天没逐张核过。
REAL_MTOP_IDS = [
    "622358416103",
    "671104868065",
    "1034773230798",
    "618204139100",
    "891954076355",
    "1070272755093",
]


def read_search_mtop() -> str:
    return (FIXTURES / SEARCH_MTOP_FIXTURE).read_text(encoding="utf-8")


def parse_search_mtop() -> dict:
    """剥掉 JSONP 回调，返回真 JSON。页面侧不需要剥（正则直接在文本上扫），这里只为断言结构。"""
    m = re.match(r"^\s*(\w+)\((.*)\)$", read_search_mtop().strip(), re.S)
    assert m, "响应不再是 JSONP 包裹"
    return json.loads(m.group(2))


# 脱敏时整值置灰的键（身份 / 追踪凭证）。留键去值：删键会让样本失去结构保真度。
DENY_KEYS = frozenset(
    {"loginId", "memberId", "sessionId", "eurl", "impressionEurl", "serverTrackId", "pvid", "requestId", "tpp_trace"}
)


def test_real_mtop_body_yields_the_exact_id_set():
    """整条链路的立足点：真实响应体喂给 `harvest_offer_ids` 出的就是接口那一次给的那几家货。

    断言的是**有序全等**而不是"包含"——多出来的是抓错响应，少了的是正则退化，
    两者都必须红。详情页 parser 曾经坏了三个月没人发现，就是因为单测里没有真样本。
    """
    assert sb.harvest_offer_ids(read_search_mtop()) == REAL_MTOP_IDS


def test_real_mtop_ids_come_only_from_the_offer_id_field():
    """真实响应里 `offer/xxx.html` 链接和 `objectId` 字段一个都没有。

    为什么要单独立一条：这三条正则的**优先级顺序**在测试里被当成"DOM 链接优先"的
    设计来维护，但真接口只走第二条。哪天有人为了"修去重顺序"动了 OFFER_RES，
    这条会告诉他改动对真实链路是空转的；反过来哪天它开始命中 offer/ 链接，
    说明接口改版成带跳转链接了，分桶口径要重看。
    """
    body = read_search_mtop()
    assert sb.OFFER_RES[0].findall(body) == []      # offer/\d+\.html
    assert sb.OFFER_RES[2].findall(body) == []      # "objectId"
    assert len(sb.OFFER_RES[1].findall(body)) == len(REAL_MTOP_IDS) * 2  # data + trackInfo.jumpArgs 各一次


def test_real_mtop_response_shape_has_not_changed():
    """接口改版的第一现场：外层 JSONP 回调名、api 名、OFFER 容器路径。

    `_TARGET_HINT` 判的是 URL 里含 "relationrecommend"，这里判的是响应体里的 api 字段——
    两个都断言，才不会"改名后静默退化成 dom 源"。
    """
    raw = read_search_mtop()
    assert re.match(r"^\s*mtopjsonp\w+\(", raw), "外层不再是 mtopjsonp* 回调"
    payload = parse_search_mtop()

    assert payload["api"] == "mtop.relationrecommend.wirelessrecommend.recommend"
    assert payload["ret"] == ["SUCCESS::调用成功"]
    offer = payload["data"]["data"]["OFFER"]
    assert [i["data"]["offerId"] for i in offer["items"]] == REAL_MTOP_IDS
    # found/hasMore 保留真页原值（60 条裁剪成 6 条后这两个数**不该**跟着变，否则就是假样本）。
    # 两个都是字符串——真接口把布尔也回成 "true"，别"顺手改成" JSON 原生类型。
    assert offer["found"] == "2000" and offer["hasMore"] == "true"
    assert offer["keywords"] == "无线鼠标"


def test_search_fixture_carries_no_account_or_tracking_identity():
    """样本是给 CI 跑的，不是给人看的：身份/追踪字段的值必须整颗置灰。"""

    seen: dict[str, set] = {}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in DENY_KEYS:
                    seen.setdefault(k, set()).add(v)
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(parse_search_mtop())
    assert seen, "样本里一个身份字段都没命中，说明脱敏脚本或接口结构变了"
    assert {v for vs in seen.values() for v in vs} == {"__REDACTED__"}


# ----------------------------------------------------- 控流层（假 playwright）


class _FakeResponse:
    def __init__(self, url, body, content_type="application/json"):
        self.url = url
        self.headers = {"content-type": content_type}
        self._body = body

    async def text(self):
        return self._body


class _FakePage:
    def __init__(self, spec):
        self._spec = spec
        self._handler = None
        self.goto_urls = []
        self.scrolled = 0
        self._first_goto = True

    def set_default_timeout(self, ms):
        pass

    def on(self, event, handler):
        self._handler = handler

    async def goto(self, url, **kwargs):
        self.goto_urls.append(url)
        if self._spec.get("goto_error"):
            raise self._spec["goto_error"]
        if self._first_goto:
            self._first_goto = False
            for resp in self._spec.get("responses", []):
                await self._handler(resp)

    async def wait_for_timeout(self, ms):
        pass

    async def evaluate(self, script):
        self.scrolled += 1

    async def content(self):
        return self._spec.get("html", "")


class _FakeContext:
    def __init__(self, spec):
        self._spec = spec
        self.cookie_batches = []
        self.page = _FakePage(spec)

    async def add_cookies(self, items):
        self.cookie_batches.append(items)

    async def new_page(self):
        return self.page


class _FakeBrowser:
    def __init__(self, spec):
        self._spec = spec
        self.contexts = []
        self.closed = False

    async def new_context(self, **kwargs):
        ctx = _FakeContext(self._spec)
        self.contexts.append(ctx)
        return ctx

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, spec, sink):
        self._spec, self._sink = spec, sink

    async def launch(self, **kwargs):
        browser = _FakeBrowser(self._spec)
        self._sink.append(browser)
        return browser


class _FakePlaywright:
    def __init__(self, spec, sink):
        self.chromium = _FakeChromium(spec, sink)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def fake_browser(monkeypatch):
    """把 playwright 换成假浏览器，返回 (spec, launched)。spec 在调用前可改。"""
    spec: dict = {"responses": [], "html": ""}
    launched: list = []
    monkeypatch.setattr(
        "playwright.async_api.async_playwright",
        lambda: _FakePlaywright(spec, launched),
    )
    return spec, launched


def _mtop_body(ids):
    return ",".join(f'"offerId":"{i}"' for i in ids)


MTOP_URL = "https://h5api.m.1688.com/h5/mtop.relationrecommend.wirelessrecommend.recommend/1.0/"
NOISE_URL = "https://gm.mmstat.com/1688.searchoffer.gif"


async def test_search_offers_prefers_the_mtop_response(fake_browser):
    spec, launched = fake_browser
    spec["responses"] = [
        _FakeResponse(NOISE_URL, _mtop_body(["999000000001"])),
        _FakeResponse(MTOP_URL, _mtop_body(["999000000002", "999000000002", "999000000003"])),
    ]
    result = await sb.search_offers("type-c数据线", cookie_text="unb=1; cookie2=abc")

    assert result.offer_ids == ["999000000002", "999000000003"]  # 目标接口优先 + 去重
    assert result.source == "mtop"
    assert result.pages == 1
    browser = launched[0]
    assert browser.closed is True  # 用完即关，不常驻
    cookies = browser.contexts[0].cookie_batches[0]
    assert {c["domain"] for c in cookies} == {".1688.com"}
    assert {c["name"] for c in cookies} == {"unb", "cookie2"}


async def test_search_offers_accepts_the_real_mtop_body(fake_browser):
    """真样本整跑一遍 `search_offers`：验的不只是正则，还有"这种响应会被归进 mtop 桶"。

    分桶靠 URL 里的 `relationrecommend`（`_TARGET_HINT`），而真实回调体是 JSONP 包裹、
    还带前导空格——所以这条同时是"外层格式没把抽取打挂"的证据。
    """
    spec, _ = fake_browser
    spec["responses"] = [
        _FakeResponse(NOISE_URL, "[]"),
        _FakeResponse(MTOP_URL, read_search_mtop()),
    ]
    result = await sb.search_offers("无线鼠标")

    assert result.offer_ids == REAL_MTOP_IDS
    assert result.source == "mtop"
    assert result.pages == 1
    # 真实接口只回 6 条（页面 found=2000）：limit 没用满，说明"只拿到 6 条"是接口给的量，不是被截了
    assert result.raw_hits == 1


async def test_search_offers_falls_back_to_other_responses(fake_browser):
    """目标接口名改了（改版）时仍能用别的 JSON 响应出货，但 source 要如实标 dom。"""
    spec, _ = fake_browser
    spec["responses"] = [_FakeResponse(NOISE_URL, _mtop_body(["999000000004"]))]
    result = await sb.search_offers("保温杯")
    assert result.offer_ids == ["999000000004"]
    assert result.source == "dom"


async def test_search_offers_falls_back_to_rendered_dom(fake_browser):
    """接口响应全是跨域/二进制抓不到时，滚动渲染后的 DOM 里可能有链接。"""
    spec, _ = fake_browser
    spec["html"] = '<a href="https://detail.1688.com/offer/999000000005.html">x</a>'
    result = await sb.search_offers("数据线")
    assert result.offer_ids == ["999000000005"]


async def test_search_offers_raises_risk_shell(fake_browser):
    """拿不到 ID 必须抛，不能回空列表——上层会把"没资格看"当成"这个关键词没货"。"""
    spec, _ = fake_browser
    spec["html"] = f"<html>{RISK_SHELL_MARKERS[1]}_____tmd_____</html>"
    with pytest.raises(sb.SearchBlocked) as exc:
        await sb.search_offers("数据线")
    assert exc.value.reason == "risk_shell"


async def test_search_offers_raises_login_wall(fake_browser):
    spec, _ = fake_browser
    spec["html"] = "<html>请先登录</html>"
    with pytest.raises(sb.SearchBlocked) as exc:
        await sb.search_offers("数据线")
    assert exc.value.reason == "login_wall"


async def test_search_offers_truncates_to_limit(fake_browser):
    spec, _ = fake_browser
    ids = [str(9990000000 + i) for i in range(30)]
    spec["responses"] = [_FakeResponse(MTOP_URL, _mtop_body(ids))]
    result = await sb.search_offers("数据线", limit=5)
    assert result.offer_ids == ids[:5]
    assert result.raw_hits == 1


async def test_search_offers_rejects_blank_keyword(fake_browser):
    """空关键词要在启动浏览器之前拒掉，别白起一个 chromium。"""
    _, launched = fake_browser
    with pytest.raises(sb.SearchBlocked) as exc:
        await sb.search_offers("   ")
    assert exc.value.reason == "empty_keyword"
    assert launched == []


async def test_search_offers_paginates_only_when_short(fake_browser):
    """第一屏不够才翻第二屏——每翻一屏都多发一组 mtop 请求。"""
    spec, launched = fake_browser
    spec["responses"] = [_FakeResponse(MTOP_URL, _mtop_body(["999000000011"]))]
    result = await sb.search_offers("数据线", limit=5, max_pages=2)

    assert result.pages == 2
    urls = launched[0].contexts[0].page.goto_urls
    assert "beginPage=1" in urls[0] and "beginPage=2" in urls[1]


async def test_search_offers_stops_after_first_page_when_enough(fake_browser):
    spec, launched = fake_browser
    ids = [str(9990000000 + i) for i in range(6)]
    spec["responses"] = [_FakeResponse(MTOP_URL, _mtop_body(ids))]
    result = await sb.search_offers("数据线", limit=5, max_pages=3)

    assert result.pages == 1
    assert len(launched[0].contexts[0].page.goto_urls) == 1


async def test_duplicate_ids_across_responses_do_not_inflate_the_count(fake_browser):
    """同一个 ID 在目标接口和噪声响应里各占一个名额，会让"够了"的判定提前——少翻一屏就少一批货。

    假浏览器只在首屏回放 responses（第二屏什么都没有），所以 pages 就是"翻没翻"的证据。
    """
    spec, launched = fake_browser
    spec["responses"] = [
        _FakeResponse(MTOP_URL, _mtop_body(["999000000021", "999000000022"])),
        _FakeResponse(NOISE_URL, _mtop_body(["999000000021", "999000000022", "999000000023"])),
    ]
    result = await sb.search_offers("数据线", limit=4, max_pages=2)

    assert result.offer_ids == ["999000000021", "999000000022"]   # 目标接口那桶说了算
    assert result.pages == 2, "去重后首屏只有 3 个 ID，不该因重复计数就认定够了"
    assert len(launched[0].contexts[0].page.goto_urls) == 2


# ------------------------------------------------------ 检索 → 采集 的交接


async def test_collect_by_keyword_hands_ids_to_detail_chain(session_factory, monkeypatch, tmp_path):
    """检索只负责出 ID，入库仍走既有详情页链路（真 parser、真工作区）。"""
    ids = [DETAIL_OFFER]

    async def fake_search(keyword, *, cookie_text=None, limit=20, max_pages=1):
        assert keyword == "保温杯"
        assert cookie_text == "unb=1"
        return sb.SearchResult(offer_ids=list(ids), source="mtop", pages=1, raw_hits=1)

    monkeypatch.setattr(sb, "search_offers", fake_search)
    monkeypatch.setattr(
        AlibabaExecutor, "_fetch_detail", lambda self, oid: _resolved(read_detail(oid))
    )
    workspace = CollectWorkspace(tmp_path)
    executor = AlibabaExecutor(
        http=_FakeImageHttp(), request_delay_range=(0, 0), workspace=workspace, cookie_text="unb=1"
    )
    async with session_factory() as db:
        result = await executor.collect_by_keyword(db, 1, "保温杯")
    await executor.aclose()

    assert result["added"] == 1
    assert result["searched"] == {"keyword": "保温杯", "found": 1, "source": "mtop", "pages": 1}
    # 批次 slug 用关键词（make_batch_id 保留中文），台账里能反查"这批是搜什么搜出来的"
    assert "保温杯" in result["batch_id"]
    async with session_factory() as db:
        goods = (await db.execute(select(Goods))).scalars().one()
    assert (goods.pool_status, goods.source_offer_id) == ("SOURCED", DETAIL_OFFER)


async def test_collect_by_keyword_does_not_swallow_blocked(session_factory, monkeypatch):
    """检索失败必须原样抛出：吞掉就会落一条 added=0 的空批次，看着像"没货"。"""

    async def blocked(keyword, **kwargs):
        raise sb.SearchBlocked("risk_shell", "被拦")

    monkeypatch.setattr(sb, "search_offers", blocked)
    executor = AlibabaExecutor(cookie_text="unb=1")
    async with session_factory() as db:
        with pytest.raises(sb.SearchBlocked):
            await executor.collect_by_keyword(db, 1, "数据线")


# ------------------------------------------------------------------ 调试落盘开关


def _dump_root(monkeypatch, tmp_path, value=None):
    """把调试目录指到 tmp；`value` 传字符串可覆盖成任意路径（含非法路径）。"""
    monkeypatch.setattr(
        sb.settings, "collect_debug_dump_path", str(tmp_path if value is None else value)
    )


def test_dumper_is_off_by_default(monkeypatch, tmp_path):
    """默认必须整体关掉：它是"把未脱敏的上游响应写在磁盘上"的开关，不能默认开。"""
    monkeypatch.setattr(sb.settings, "collect_debug_dump_path", "")
    assert sb._Dumper("数据线").dir is None


def test_dumper_keeps_the_keyword_out_of_the_path(monkeypatch, tmp_path):
    """目录名只用 UTC 时间戳——关键词是用户输入，拼进路径就是一条穿越口。"""
    _dump_root(monkeypatch, tmp_path)
    d = sb._Dumper("../../evil/..")
    assert d.dir is not None
    assert "evil" not in str(d.dir)
    assert d.dir.parent == tmp_path
    assert d.dir.is_dir()
    # meta.json 才是放关键词的地方（只在目录内部，不参与寻址）
    assert "../../evil/.." in (d.dir / "meta.json").read_text(encoding="utf-8")


def test_dumper_degrades_instead_of_raising(monkeypatch, tmp_path):
    """调试设施坏了不能把检索一起拖死：root 指到一个文件上 → 静默关闭。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    _dump_root(monkeypatch, tmp_path, blocker)
    assert sb._Dumper("数据线").dir is None


async def test_search_offers_dumps_bodies_and_summary(monkeypatch, fake_browser, tmp_path):
    """开开关后：命中的响应体原样落盘 + 一份 summary，供事后手工脱敏进 fixtures。"""
    _dump_root(monkeypatch, tmp_path)
    spec, _ = fake_browser
    body = _mtop_body(["999000000010", "999000000011"])
    spec["responses"] = [
        _FakeResponse(NOISE_URL, _mtop_body(["999000000099"])),
        _FakeResponse(MTOP_URL, body),
        _FakeResponse(MTOP_URL, "<html>no ids here</html>"),  # 没命中 ID 的不落盘
    ]
    result = await sb.search_offers("type-c数据线", limit=2)

    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    names = sorted(p.name for p in run_dir.iterdir())
    assert [n for n in names if n.endswith("txt")] == ["001_other_1ids.txt", "002_mtop_2ids.txt"]
    assert (run_dir / "002_mtop_2ids.txt").read_text(encoding="utf-8") == body
    assert (run_dir / "page.html").exists()

    import json as _json

    summary = _json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["ok"] is True
    assert summary["offer_ids"] == result.offer_ids == ["999000000010", "999000000011"]
    assert summary["source"] == "mtop"
    assert summary["bodies_dumped"] == 2  # 第三个响应体没出 ID，不算一次


async def test_search_offers_dumps_the_blocked_case_too(monkeypatch, fake_browser, tmp_path):
    """最有价值的恰恰是失败那次——"为什么没结果"要靠落下来的壳页判断，不能只 dump 成功。"""
    _dump_root(monkeypatch, tmp_path)
    spec, _ = fake_browser
    spec["html"] = f"<html>{RISK_SHELL_MARKERS[1]}_____tmd_____</html>"
    with pytest.raises(sb.SearchBlocked):
        await sb.search_offers("数据线")

    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    import json as _json

    assert _json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["error"] == "risk_shell"
    assert "_____tmd_____" in (run_dir / "page.html").read_text(encoding="utf-8")


async def test_search_offers_works_with_the_switch_off(monkeypatch, fake_browser):
    """关着时行为与加这个开关之前完全一致——不碰磁盘。"""
    monkeypatch.setattr(sb.settings, "collect_debug_dump_path", "")
    spec, _ = fake_browser
    spec["responses"] = [_FakeResponse(MTOP_URL, _mtop_body(["999000000012"]))]
    assert (await sb.search_offers("数据线")).offer_ids == ["999000000012"]
