"""验证方案 A 的前置问题：已入库的 1688 货源账号 Cookie 能不能打开搜索列表页拿 offer_id。

三条探针，总共发 3 个请求（对货源账号风控友好）。只读：不写库、不碰闲鱼、不打 1688 之外
的任何域。

  P1  httpx + 登录 Cookie → s.1688.com 搜索页   —— 不开浏览器能不能拿到 ID（最便宜的形态）
  P2  浏览器 + 登录 Cookie → s.1688.com 搜索页   —— 方案 A 的真实形态（JS 渲染后）
  P3  httpx + 登录 Cookie → detail.1688.com 详情页 → 喂现有 parser
      —— 顺带验 docs/后端接口文档.md §11 的头号未决风险「parser 字段映射未经真实页面验证」，
         以及 memory 里记的「拿已入库 Cookie 打 detail.1688.com 会不会被 x5sec 拦」。

跑法（必须在 backend 容器内：COOKIE_CRYPTO_SECRET 与 playwright 都在那边；
      本机原生 MySQL 里那套历史数据没有货源账号）：
    docker compose cp backend/scripts/spike_1688_search_auth.py backend:/app/scripts/
    docker compose exec backend python scripts/spike_1688_search_auth.py "type-c数据线"
    # 只跑浏览器那条（少发两个 httpx 请求，反复调 P2 时用）：
    docker compose exec backend python scripts/spike_1688_search_auth.py "type-c数据线" --browser-only
"""

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import quote

# 直接 python scripts/xxx.py 时 sys.path[0] 是 scripts/，不插这一行 import app 会失败
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import text

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.1688.com/",
}

# 反爬/风控证据。x5sec 是阿里系的拦截标记，出现即判"被拦"。
ANTI_RE = re.compile(r"(x5sec|滑块|验证码|punish|captcha|NCODE|security\.1688|_____tmd_____)")
# 登录墙证据：Cookie 失效时 1688 会把搜索页换成登录引导。
LOGIN_RE = re.compile(r"(请先?登录|立即登录|扫码登录|mini_login|loginId)")
OFFER_RES = (
    re.compile(r"offer/(\d{6,20})\.html"),
    re.compile(r'"offerId"\s*:\s*"?(\d{6,20})'),
    re.compile(r'"objectId"\s*:\s*"?(\d{6,20})'),
)


def harvest(html: str) -> list[str]:
    ids: set[str] = set()
    for rx in OFFER_RES:
        ids.update(rx.findall(html))
    return sorted(ids)


def verdict(body: str, ids: list[str]) -> str:
    if ids:
        return "PASS 拿到 offer_id"
    anti = sorted(set(ANTI_RE.findall(body)))
    if anti:
        return f"BLOCKED 风控拦截（命中 {anti}）"
    if LOGIN_RE.search(body):
        return "BLOCKED 登录墙（Cookie 未被接受或已过期）"
    return "MISS 未拦未墙但没解析出 ID（JS 壳/页面改版/无结果）"


def sniff(label: str, body: str, status=None, final=None) -> None:
    ids = harvest(body)
    print(f"\n[{label}] status={status} final={final} bytes={len(body)} ids={len(ids)}")
    print(f"    判定 → {verdict(body, ids)}")
    if ids:
        print(f"    样本 offer_id = {ids[:6]}")
    else:
        plain = re.sub(r"<[^>]+>", " ", body)
        plain = re.sub(r"\s+", " ", plain).strip()
        print(f"    正文片段 = {plain[:260]}")


async def load_cookie() -> tuple[int, str, dict[str, str]]:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                text(
                    "SELECT id, external_uid, encrypted_cookie FROM alibaba_account "
                    "WHERE deleted=0 AND status=1 ORDER BY id LIMIT 1"
                )
            )
        ).first()
    if row is None:
        raise SystemExit("库里没有可用的 1688 货源账号，先在 /collect/1688 扫码绑一个")
    from app.auth.alibaba_account_service import alibaba_cookie_text

    from app.core.cookies import parse_cookie_text

    async with AsyncSessionLocal() as db:
        cookie = parse_cookie_text(await alibaba_cookie_text(db, row.id))
    # 只报键名，绝不打印 Cookie 值（它会进终端滚动区/CI 日志）
    print(f"货源账号 id={row.id} unb={row.external_uid} keys={len(cookie)}")
    print(f"    关键字段 unb={'Y' if cookie.get('unb') else '-'} "
          f"cookie2={'Y' if cookie.get('cookie2') else '-'} "
          f"_m_h5_tk={'Y' if cookie.get('_m_h5_tk') else '-'}")
    return row.id, row.external_uid, cookie


def search_url(keyword: str, gbk: bool) -> str:
    """keywords 参数按 GBK 百分号编码——1688 搜索页历史上只认 GBK，UTF-8 会检索成乱码词。"""
    raw = keyword.encode("gbk", "ignore") if gbk else keyword.encode("utf-8")
    return f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote(raw)}"


async def p_httpx(cookie: dict[str, str], url: str, label: str) -> tuple[str, list[str]]:
    async with httpx.AsyncClient(timeout=25.0, headers={**HEADERS, "Cookie": "; ".join(f"{k}={v}" for k, v in cookie.items())}) as c:
        r = await c.get(url, follow_redirects=True)
        sniff(label, r.text, r.status_code, r.url)
        return r.text, harvest(r.text)


async def p_browser(cookie: dict[str, str], url: str, label: str) -> tuple[str, list[str]]:
    """浏览器探针：除了 DOM，还抓页面自己发出的 JSON 接口。

    为什么要抓接口：s.1688.com 搜索页已改成前端渲染，DOM 里有没有 ID 取决于渲染成没成功；
    而它调的那个 JSON 接口才是真正想要的东西——如果接口能直接拿 Cookie 重放，
    方案 A 就不必常驻浏览器。
    """
    from playwright.async_api import async_playwright

    json_hits: list[tuple[str, int, list[str]]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="zh-CN", viewport={"width": 1366, "height": 900}, user_agent=HEADERS["User-Agent"])
        await ctx.add_cookies([{"name": k, "value": v, "domain": ".1688.com", "path": "/"} for k, v in cookie.items()])
        page = await ctx.new_page()

        async def on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "")
                if "json" not in ct and "text" not in ct:
                    return
                body = await resp.text()
            except Exception:
                return
            ids = harvest(body)
            low = resp.url.lower()
            if ids or any(t in low for t in ("search", "offer", "mtop", "item")):
                json_hits.append((resp.url, len(body), ids))

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(5000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)
        except Exception as exc:
            print(f"    goto/scroll 异常 {type(exc).__name__}: {str(exc)[:160]}")

        html = await page.content()
        print(f"\n[{label}] 最终 URL={page.url} DOM bytes={len(html)}")
        ids = harvest(html)
        sniff(f"{label} DOM", html)

        # --- 渲染诊断：区分"真被风控"和"滑块 SDK 常驻导致的误报" ---
        print(f"    'detail.1688.com/offer' 出现次数 = {html.count('detail.1688.com/offer')}")
        for probe_name, rx in (
            ("punish/验证 iframe", re.compile(r"(punish|x5sec|sec.taobao|_randp)", re.I)),
            ("结果容器", re.compile(r"(searchoffer|offerList|listmod|J_offer|normal-sortbar)", re.I)),
            ("空结果话术", re.compile(r"(没有找到|无搜索结果|相关的货源)")),
        ):
            m = rx.findall(html)
            print(f"    {probe_name} = {sorted(set(m))[:5] or '无'}")
        try:
            visible = await page.evaluate(
                "() => document.body ? document.body.innerText.replace(/\\s+/g,' ').slice(0,400) : ''"
            )
            print(f"    可见文字 = {visible[:400]}")
        except Exception as exc:
            print(f"    可见文字取不到 {type(exc).__name__}")

        # --- 页面自己调的数据接口 ---
        with_ids = [h for h in json_hits if h[2]]
        print(f"\n    [{label} 网络] 抓到 JSON/文本响应 {len(json_hits)} 个，其中含 offer_id 的 {len(with_ids)} 个")
        for u, n, i in json_hits[:14]:
            print(f"      {n:>8}B ids={len(i):>3} {u[:130]}")
        if with_ids:
            u, n, i = max(with_ids, key=lambda h: len(h[2]))
            print(f"    ★ 最富接口 = {u[:150]}\n      bytes={n} ids={len(i)} 样本={i[:5]}")
        await browser.close()
        return html, ids or [x for _, _, ii in with_ids for x in ii]


def diagnose_detail_html(html: str, offer_id: str) -> None:
    """parser 输出全空时，判断到底是"页面结构变了（改提取键）"还是"被软拦了（换方案）"。"""
    print("\n    --- 详情页结构诊断 ---")
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    print(f"    <title> = {(t.group(1).strip()[:80] if t else '无')!r}")
    cbu_imgs = len(re.findall(r"cbu\d*\.alicdn\.com", html))
    has_price = bool(re.search(r"[¥￥]|\"price\"", html))
    print(f"    软拦证据: 登录墙={'Y' if LOGIN_RE.search(html) else '-'} "
          f"风控词={'Y' if ANTI_RE.search(html) else '-'} "
          f"cbu 图片数={cbu_imgs} 含价格符号={'Y' if has_price else '-'}")

    # 逐个字段报命中情况（parser 现认 window.context 里的定向键，见 parser.py 模块注释）
    from app.collectors.alibaba import parser as _p

    sp = _p.parse_offer(html, offer_id)
    for name, val in (
        ("title", sp.title),
        ("price_cent", sp.price_cent or None),
        ("images", sp.image_urls and len(sp.image_urls)),
        ("category", sp.raw.get("category")),
        ("supplier", sp.raw.get("supplier")),
        ("specs", sp.raw.get("specs")),
        ("detail_url", sp.raw.get("detail_url")),
    ):
        print(f"    字段 {name:<10} = {val if val else '空 ✗'}")

    # 页面里到底有哪些内嵌 JSON 全局变量（结构再改版时靠这行定位新载体）
    globals_ = re.findall(r"(?:window\.|var\s+|const\s+|let\s+)([A-Za-z_$][\w$.]{2,40})\s*=\s*(?=[{\[])", html)
    tally: dict[str, int] = {}
    for g in globals_:
        tally[g] = tally.get(g, 0) + 1
    cand = sorted((k for k in tally if "data" in k.lower() or "init" in k.lower() or "offer" in k.lower() or "detail" in k.lower() or "context" in k.lower()))
    print(f"    内嵌 JSON 全局变量（疑似数据源）= {cand[:20] or '无'}")
    print(f"    全部全局变量候选 = {sorted(tally)[:30]}")
    print("    注：真实页面样本已脱敏进 tests/fixtures/alibaba/，本脚本不再落 .fixture_*.html")


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    browser_only = "--browser-only" in sys.argv
    keyword = args[0] if args else "type-c数据线"
    _, _, cookie = await load_cookie()

    ids: list[str] = []
    html = ""
    if not browser_only:
        print(f"\n=== P1 httpx + 登录 Cookie 打搜索页（GBK 编码，1688 历史要求）keyword={keyword}")
        html, ids = await p_httpx(cookie, search_url(keyword, gbk=True), "P1 GBK")
        if not ids:
            print("\n=== P1b UTF-8 编码重试")
            html, ids = await p_httpx(cookie, search_url(keyword, gbk=False), "P1b UTF8")
    else:
        print("=== 跳过 P1（--browser-only）")

    print("\n=== P2 headless chromium + 登录 Cookie 打搜索页（方案 A 真实形态）")
    b_html, b_ids = await p_browser(cookie, search_url(keyword, gbk=True), "P2")

    pool = ids or b_ids
    print("\n=== P3 详情页：真实 HTML → 现有 parser（验 §11 字段映射）")
    if not pool:
        print("    跳过：前两步没拿到任何 offer_id，无详情页可验。")
    else:
        oid = pool[0]
        d_html, _ = await p_httpx(cookie, f"https://detail.1688.com/offer/{oid}.html", f"P3 detail {oid}")
        from app.collectors.alibaba.parser import parse_offer

        p = parse_offer(d_html, oid)
        print(f"    parser 输出 → title={p.title[:60]!r}")
        print(f"                  price_cent={p.price_cent} images={len(p.image_urls or [])} "
              f"stock={p.raw.get('stock')} category={p.raw.get('category')} supplier={p.raw.get('supplier')}")
        print(f"    判定 = {'PASS 字段齐' if p.title and p.price_cent and p.image_urls else 'FAIL 有字段没解析出来（要改 parser，见上）'}")
        diagnose_detail_html(d_html, oid)

    print("\n" + "=" * 60)
    print(f"结论速览：httpx 侧 ids={len(ids)}  浏览器侧 ids={len(b_ids)}")
    print(f"  A 可行（浏览器形态）: {bool(b_ids)}")
    print(f"  A 可行（免浏览器形态）: {bool(ids)}")
    print("  两者都为 False 时看上面的『判定』行区分是被拦 / 登录墙 / 还是纯 JS 壳。")


if __name__ == "__main__":
    asyncio.run(main())
