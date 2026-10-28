"""一次性 spike：实测 1688 搜索列表页能否稳定解析出 offer_id（选 A 路线的前置验证）。

跑法：.venv/Scripts/python.exe scripts/spike_1688_search.py
"""

import asyncio
import re
from urllib.parse import quote

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.1688.com/",
}

KEYWORD = "type-c数据线"

# 1688 搜索页 keywords 历史上要 GBK 百分号编码，两种都试
CANDIDATES = {
    "selloffer_gbk": f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote(KEYWORD.encode('gbk'))}",
    "selloffer_utf8": f"https://s.1688.com/selloffer/offer_search.htm?keywords={quote(KEYWORD)}",
    "searchshop": f"https://s.1688.com/company/company_search.htm?keywords={quote(KEYWORD.encode('gbk'))}",
    "mobile_air": f"https://search.1688.com/quote/quoteList.htm?keywords={quote(KEYWORD)}",
}

OFFER_RE = re.compile(r'offer/(\d{6,20})\.html')
DETAIL_RE = re.compile(r'"offerId"\s*:\s*"?(\d{6,20})')
ANTI_RE = re.compile(r"(滑块|验证|punish|captcha|security\.1688|NCODE|请输入验证码)")


async def probe(client: httpx.AsyncClient, name: str, url: str) -> None:
    try:
        r = await client.get(url, follow_redirects=True)
        body = r.text
        ids = set(OFFER_RE.findall(body)) | set(DETAIL_RE.findall(body))
        anti = ANTI_RE.findall(body)
        print(
            f"[{name}] status={r.status_code} final={r.url} len={len(body)} "
            f"offer_ids={len(ids)} anti={sorted(set(anti))[:3]}"
        )
        print(f"    sample_ids={sorted(ids)[:5]}")
        if not ids:
            # 打一段正文片段，看是登录墙 / JS 壳 / 反爬页
            text = re.sub(r"<[^>]+>", " ", body)
            text = re.sub(r"\s+", " ", text)
            print(f"    peek={text[:300]}")
    except Exception as exc:
        print(f"[{name}] EXC {type(exc).__name__}: {exc}")


async def main() -> None:
    async with httpx.AsyncClient(timeout=25.0, headers=HEADERS) as client:
        for name, url in CANDIDATES.items():
            await probe(client, name, url)


if __name__ == "__main__":
    asyncio.run(main())
