"""1688 搜索列表页取 offer_id（真浏览器拦接口）。

为什么必须开浏览器：搜索页已改成前端渲染，DOM 初值里 0 条 offer 链接，商品 ID 只出现在
页面自己发出的 `mtop.relationrecommend.wirelessrecommend.recommend` 响应里。实测取证见
`scripts/spike_1688_search_auth.py`（P1 httpx 裸取 / P2 浏览器两条探针，P1 不可行），
决策见 docs/采集链路硬化与自然语言检索上架需求设计.md D-2026-09-19-16。

只做检索：不入库、不采集、不触达闲鱼。返回的 offer_id 交给
`executor.collect_by_offer_ids` 走既有的详情页链路。
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from app.collectors.base import is_risk_shell
from app.core.config import settings

logger = logging.getLogger(__name__)

SEARCH_URL = "https://s.1688.com/selloffer/offer_search.htm?keywords={kw}&beginPage={page}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 目标接口命中不到时，退而扫所有 JSON 响应里的 ID——最富的那个说了算（spike 实测口径）
_TARGET_HINT = "relationrecommend"

OFFER_RES = (
    re.compile(r"offer/(\d{6,20})\.html"),
    # (?!\d) 是必需的：不加的话 21 位以上的一串数字（订单号/时间戳拼接）会被截前 20 位
    # 当成 offer_id，下游拿去打详情页只会拿到风控壳，报出来的还是 blocked 而不是"ID 是假的"。
    re.compile(r'"offerId"\s*:\s*"?(\d{6,20})(?!\d)'),
    re.compile(r'"objectId"\s*:\s*"?(\d{6,20})(?!\d)'),
)
LOGIN_RE = re.compile(r"(请先?登录|立即登录|扫码登录|mini_login|loginId)")

# 一次只开一个浏览器：backend 容器里还有 auto_listing / order_sync 在跑，
# chromium 常驻内存以百 MB 计，并发检索会把进程吃掉。
_BROWSER_LOCK = asyncio.Semaphore(1)

_PAGE_TIMEOUT_MS = 45_000
_RENDER_SETTLE_S = 5
_SCROLL_SETTLE_S = 3


class SearchBlocked(RuntimeError):
    """检索不可行（风控壳 / 登录墙 / 解析落空）。

    刻意做成异常而不是返回空列表：空列表分不清"这个关键词没货"和"我们没资格看"，
    上层一旦按前者处理就会静默放弃一批货源。
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass
class SearchResult:
    offer_ids: list[str] = field(default_factory=list)
    source: str = ""          # "mtop" | "dom"，用于判断接口改版后还能不能退
    pages: int = 0
    raw_hits: int = 0         # 去重前命中的响应体个数，排查"为什么只有 3 条"时看这里


class _Dumper:
    """把检索过程中拿到的响应体原样落盘，只为两件事：排障、攒真实 fixture。

    默认**整体关闭**（`settings.collect_debug_dump_path` 为空）。开它的唯一场景是"接口到底还长
    不长这样"需要证据时——手搓的测试 payload 只能证明抽取逻辑自洽，证明不了上游没改版。

    三条硬约束：
    - **目录名不含关键词**：keyword 是用户输入，拼进路径等于给了一条穿越口（`../../`）。
      关键词写进 `meta.json`，路径只用 UTC 时间戳。
    - 任何一次落盘失败都不能影响检索本身：整体 `try/except` 吞掉，只记日志。
    - 落盘目录里是**未脱敏的上游响应**（含昵称/账号痕迹），所以它必须在 `.gitignore` 覆盖的
      `data/` 下，且**不能直接当 fixture 用**——进 `tests/fixtures/` 前要手工去过。
    """

    MAX_BODIES = 40

    def __init__(self, keyword: str) -> None:
        root = (settings.collect_debug_dump_path or "").strip()
        self.dir: Path | None = None
        self._n = 0
        if not root:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            self.dir = Path(root) / stamp
            self.dir.mkdir(parents=True, exist_ok=False)
            (self.dir / "meta.json").write_text(
                json.dumps({"keyword": keyword, "started_at": stamp}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("检索调试落盘不可用，跳过：%s", exc)
            self.dir = None

    def body(self, url: str, payload: str, ids: list[str]) -> None:
        if self.dir is None or self._n >= self.MAX_BODIES or not ids:
            return
        self._n += 1
        name = "mtop" if _TARGET_HINT in url else "other"
        try:
            (self.dir / f"{self._n:03d}_{name}_{len(ids)}ids.txt").write_text(
                payload, encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("检索响应体写盘失败（不影响检索）：%s", exc)

    def finish(self, html: str, result: "SearchResult | None", error: str) -> None:
        if self.dir is None:
            return
        try:
            (self.dir / "page.html").write_text(html or "", encoding="utf-8")
            (self.dir / "summary.json").write_text(
                json.dumps(
                    {
                        "ok": result is not None,
                        "error": error,
                        "bodies_dumped": self._n,
                        "offer_ids": result.offer_ids if result else [],
                        "source": result.source if result else "",
                        "pages": result.pages if result else 0,
                        "raw_hits": result.raw_hits if result else 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("检索收尾写盘失败（不影响检索）：%s", exc)


def build_search_url(keyword: str, page: int = 1) -> str:
    """keywords 按 GBK 百分号编码——1688 搜索页历史上只认 GBK，UTF-8 会检索成乱码词。"""
    raw = quote(keyword.encode("gbk", "ignore"))
    return SEARCH_URL.format(kw=raw, page=page)


def harvest_offer_ids(payload: str) -> list[str]:
    """从一段 HTML/JSON 文本里抽 offer_id，保序去重。"""
    seen: dict[str, None] = {}
    for rx in OFFER_RES:
        for oid in rx.findall(payload or ""):
            seen.setdefault(oid, None)
    return list(seen)


def classify_failure(payload: str) -> str:
    """没拿到 ID 时到底是哪种失败——决定上层是「重扫 Cookie」还是「换关键词」。"""
    if is_risk_shell(payload):
        return "risk_shell"
    if LOGIN_RE.search(payload or ""):
        return "login_wall"
    return "no_result"


async def search_offers(
    keyword: str,
    *,
    cookie_text: str | None = None,
    limit: int = 20,
    max_pages: int = 1,
) -> SearchResult:
    """关键词 → offer_id 列表。

    `cookie_text` 必须是**已登录**货源账号的 Cookie（`alibaba_cookie_text` 取）；
    匿名搜索页同样会返回 x5sec 壳。拿不到 ID 一律抛 `SearchBlocked`，不返空当成功。
    """
    if not keyword.strip():
        raise SearchBlocked("empty_keyword", "关键词为空")
    limit = max(1, min(int(limit), 50))
    dumper = _Dumper(keyword)

    from playwright.async_api import async_playwright

    async with _BROWSER_LOCK:
        # 边收边累积，避免"抓够了还继续滚"——每次滚动都会多发一组 mtop 请求
        mtop_ids: list[str] = []
        other_ids: list[str] = []
        raw_hits = 0
        pages_done = 0
        last_html = ""

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(
                    locale="zh-CN",
                    viewport={"width": 1366, "height": 900},
                    user_agent=HEADERS["User-Agent"],
                )
                if cookie_text:
                    from app.core.cookies import parse_cookie_text

                    await ctx.add_cookies([
                        {"name": k, "value": v, "domain": ".1688.com", "path": "/"}
                        for k, v in parse_cookie_text(cookie_text).items()
                    ])
                page = await ctx.new_page()
                page.set_default_timeout(_PAGE_TIMEOUT_MS)

                async def on_response(resp) -> None:
                    nonlocal raw_hits
                    try:
                        ct = (resp.headers or {}).get("content-type", "")
                        if "json" not in ct and "text" not in ct:
                            return
                        body = await resp.text()
                    except Exception:
                        return  # 二进制/已释放的响应体，跳过即可
                    ids = harvest_offer_ids(body)
                    if not ids:
                        return
                    # 同步写文件：只在显式开调试时走得到，一次几十 KB，不值得为它上 executor 线程
                    dumper.body(resp.url, body, ids)
                    raw_hits += 1
                    bucket = mtop_ids if _TARGET_HINT in resp.url else other_ids
                    # 跨两个桶一起判重：只跟本桶比会让同一个 ID 两边各占一个名额，
                    # 而下面的 `len(甲)+len(乙) >= limit` 会因此提前判定"够了"、少翻一屏。
                    for oid in ids:
                        if oid not in mtop_ids and oid not in other_ids:
                            bucket.append(oid)

                page.on("response", on_response)

                for pno in range(1, max_pages + 1):
                    try:
                        await page.goto(
                            build_search_url(keyword, pno),
                            wait_until="domcontentloaded",
                            timeout=_PAGE_TIMEOUT_MS,
                        )
                    except Exception as exc:
                        logger.warning("1688 搜索页打开失败 kw=%s p=%d: %s", keyword, pno, exc)
                        break
                    pages_done += 1
                    await page.wait_for_timeout(_RENDER_SETTLE_S * 1000)
                    # 列表页懒加载，不滚到底接口根本不会发第二屏请求
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(_SCROLL_SETTLE_S * 1000)
                    if len(mtop_ids) + len(other_ids) >= limit:
                        break

                last_html = await page.content()
            finally:
                await browser.close()

    ids = (mtop_ids or other_ids)[:limit]
    source = "mtop" if mtop_ids else "dom"
    if not ids:
        ids = harvest_offer_ids(last_html)
        source = "dom"

    if not ids:
        reason = classify_failure(last_html)
        logger.warning("1688 检索无结果 kw=%s reason=%s pages=%d", keyword, reason, pages_done)
        dumper.finish(last_html, None, reason)
        raise SearchBlocked(reason, f"关键词「{keyword}」没拿到 offer_id")

    logger.info("1688 检索成功 kw=%s ids=%d source=%s pages=%d", keyword, len(ids), source, pages_done)
    result = SearchResult(
        offer_ids=ids,
        source=source,
        pages=pages_done,
        raw_hits=raw_hits,
    )
    dumper.finish(last_html, result, "")
    return result
