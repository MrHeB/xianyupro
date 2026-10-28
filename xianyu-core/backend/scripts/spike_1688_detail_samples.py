"""改 parser 之前的取样实验：1688 详情页是不是只有一个数据载体模板。

为什么要先跑这个：`spike_1688_search_auth.py` 里那份真实详情页样本是 **jgdz 加工定制模板**
（`contextPath="/processing"`、`channelType=jgdz`），而它的 `window.context` 让现有 parser 的
三条正则全部落空。**只按一个样本改 parser 就是按一个模板改死**——所以先跨品类取几份，
看 `window.context=(function(...))` 这个载体是不是普遍成立、还是存在多种结构。

只读取样：浏览器只为拿 offer_id 开 2 个搜索页，详情页全部走 httpx。总页面加载 ≈6 次，
带随机间隔，别拿它当常驻采集器用。

跑法：
    docker compose cp backend/scripts/spike_1688_detail_samples.py backend:/app/scripts/
    docker compose exec backend python scripts/spike_1688_detail_samples.py "保温杯" "手机壳"
    # 样本落在容器 /app/scripts/.samples/，取回本地：
    docker compose cp backend:/app/scripts/.samples backend/scripts/.samples
"""

import asyncio
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import spike_1688_search_auth as s  # noqa: E402  复用 Cookie 装载 / 浏览器搜索 / 详情页探针

SAMPLE_DIR = Path(__file__).resolve().parent / ".samples"

# 候选数据载体：现状 parser 认的三个 + 本次新发现的
CARRIERS = {
    "window.__INIT_DATA__": re.compile(r"window\.__INIT_DATA__\s*="),
    "window.runParams": re.compile(r"window\.runParams\s*="),
    '"offer":…,"supplier"': re.compile(r'"offer"\s*:\s*\{.*?\}\s*,\s*"supplier"', re.S),
    "window.context": re.compile(r"window\.context\s*=\s*\("),
}
# 页面里几个"有没有真商品数据"的硬指标
EVIDENCE = {
    "subject": re.compile(r'"subject"\s*:\s*"([^"]{6,80})'),
    "skuPriceScale": re.compile(r'"skuPriceScale"\s*:\s*"([^"]{0,40})'),
    "price键": re.compile(r'"price"\s*:\s*"?([\d.]+)'),
    "imageInfos": re.compile(r'"(?:imageInfos|images|imageURI)"'),
    "leafCategoryId": re.compile(r'"leafCategoryId"\s*:\s*"?(\d+)'),
    "winport/商家": re.compile(r'"(?:companyName|sellerLoginId|memberId)"\s*:\s*"?([^",}]{2,40})'),
}


def report(oid: str, html: str) -> dict:
    cp = re.search(r'contextPath\s*=\s*"([^"]*)"', html)
    gl = re.search(r'"(?:channelType|verticalType|sceneKey)"\s*:\s*"?([\w-]+)', html)
    print(f"\n--- {oid} bytes={len(html)} contextPath={cp.group(1) if cp else '?'} "
          f"首个模板参数={gl.group(1) if gl else '?'} ---")
    hit = {k: bool(rx.search(html)) for k, rx in CARRIERS.items()}
    print("    数据载体: " + "  ".join(f"{k}={'Y' if v else '-'}" for k, v in hit.items()))
    for k, rx in EVIDENCE.items():
        m = rx.search(html)
        print(f"    {k:<14} = {(m.group(1)[:40] if m and m.groups() else ('有' if m else '无'))}")
    from app.collectors.alibaba.parser import parse_offer

    p = parse_offer(html, oid)
    ok = bool(p.title and p.price_cent and p.image_urls)
    print(f"    现有 parser → title={p.title[:34]!r} price_cent={p.price_cent} "
          f"images={len(p.image_urls or [])} 判定={'PASS' if ok else 'FAIL'}")
    return {"oid": oid, "carriers": hit, "parser_ok": ok, "parser_empty": not p.title and not p.price_cent}


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keywords = args or ["保温杯", "手机壳"]
    per_kw = 2 if "--one-per-kw" not in sys.argv else 1
    _, _, cookie = await s.load_cookie()
    SAMPLE_DIR.mkdir(exist_ok=True)

    picked: list[str] = []
    for kw in keywords:
        print(f"\n=== 搜索页取 id：{kw}")
        await asyncio.sleep(random.uniform(2.0, 4.0))
        _, ids = await s.p_browser(cookie, s.search_url(kw, gbk=True), f"{kw}")
        fresh = [i for i in ids if i not in picked][:per_kw]
        print(f"    本页取 {len(fresh)} 个（该词共 {len(ids)} 个候选）：{fresh}")
        picked.extend(fresh)

    print(f"\n=== 逐个详情页取样（{len(picked)} 个）")
    results = []
    for oid in picked:
        await asyncio.sleep(random.uniform(2.0, 5.0))
        html, _ = await s.p_httpx(cookie, f"https://detail.1688.com/offer/{oid}.html", f"detail {oid}")
        (SAMPLE_DIR / f"detail_{oid}.html").write_text(html, encoding="utf-8")
        results.append(report(oid, html))

    # 把上一轮那份 jgdz 样本一起纳入对照
    prev = Path(__file__).resolve().parent / ".fixture_detail_1688.html"
    if prev.exists():
        print("\n=== 对照组：上一轮那份 jgdz 样本")
        results.append(report("1053709411360(旧)", prev.read_text(encoding="utf-8")))

    print("\n" + "=" * 62)
    print("取样汇总（决定 parser 怎么改）")
    for r in results:
        print(f"  {r['oid']:<22} 载体={','.join(k for k, v in r['carriers'].items() if v) or '无'} "
              f"parser空={r['parser_empty']}")
    only_ctx = all(r["carriers"]["window.context"] and not any(
        r["carriers"][k] for k in list(CARRIERS)[:3]) for r in results)
    print(f"\n  所有样本都只认 window.context（旧三条正则一律落空）？ → {only_ctx}")
    print(f"  样本目录：{SAMPLE_DIR}（含 loginId/clientIP 痕迹，勿提交）")


if __name__ == "__main__":
    asyncio.run(main())
