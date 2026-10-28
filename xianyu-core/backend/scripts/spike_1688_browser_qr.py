"""spike：1688 扫码在真浏览器里到底会不会下发 unb/cookie2（headful，你自己扫窗口里的码）。

为什么要这个 spike：alibaba_login.py 的手写 httpx 三步能拿到 CONFIRMED，但 query.do
一个 Set-Cookie 都不下发；jump.htm 的正文又是 JS 门控跳转（if(false){location.href=
'https://login.taobao.com/jump?target=...'}），说明换票动作要真浏览器执行。reference/
goofish-cli 的扫码登录同样是 Playwright 而不是手写 HTTP。

跑法（复用本机已装 Chrome，不下浏览器内核）：
    .venv/Scripts/python.exe scripts/spike_1688_browser_qr.py
"""

import asyncio
import sys
import time
from urllib.parse import quote

from playwright.async_api import async_playwright

# 入口 URL 抄自 reference 之外的实证：github.com/zhangjiancong/MarketSpider GetCookie.py:12-21。
# 关键两点：登录主机是 login.taobao.com（不是 passport.1688.com），且回跳链里有
# marketSigninJump.htm 这一跳——它才是把 .taobao.com 会话换成 1688 会话的那一步。
MARKET_SIGNIN_JUMP = "https://login.1688.com/member/marketSigninJump.htm?Done=" + quote(
    "https://www.1688.com/", safe=""
)
HOP1 = "https://login.1688.com/member/jump.htm?target=" + quote(MARKET_SIGNIN_JUMP, safe="")
LOGIN_PAGE = "https://login.taobao.com/?redirect_url=" + quote(HOP1, safe="") + "&style=tao_custom&from=1688web"
QR_SELECTORS = [
    ".qrcode-login img",
    ".qrcode-img img",
    ".login-qrcode img",
    "#qrcodeImg",
    "img[data-qr]",
    ".qrcode img",
    "canvas",
]
WAIT_SECONDS = 240
TARGET_KEYS = ("unb", "cookie2", "_m_h5_tk", "sgcookie", "_tb_token_", "tracknick")


def say(*a) -> None:
    print(*a, flush=True)


async def main() -> int:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=False)
        context = await browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 860})
        page = await context.new_page()
        say(f">>> 打开 {LOGIN_PAGE[:120]}...")
        try:
            await page.goto(LOGIN_PAGE, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            say(f"!!! goto 失败 {type(exc).__name__}: {exc}")
            await browser.close()
            return 2

        found = None
        for sel in QR_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=6000, state="visible")
                found = sel
                break
            except Exception:
                continue
        say(f">>> QR 选择器命中: {found!r}  page.url={page.url[:120]}")
        frames = [f.url for f in page.frames]
        say(f">>> frames: {[u[:90] for u in frames]}")
        if not found:
            classes = await page.evaluate(
                "() => [...document.querySelectorAll('[class],[id]')]"
                ".map(e=>(e.tagName+'.'+(e.className||'')+'#'+(e.id||''))).slice(0,80)"
            )
            say(f"!!! 没找到二维码。页面元素: {classes}")
        await page.screenshot(path="scripts/.spike_1688_browser_qr.png")
        say(">>> 已截图 scripts/.spike_1688_browser_qr.png；请在弹出的 Chrome 窗口里扫码并确认")

        started = time.time()
        seen: set[str] = set()
        while time.time() - started < WAIT_SECONDS:
            cookies = await context.cookies()
            names = {c["name"] for c in cookies if c.get("value")}
            fresh = names - seen
            if fresh:
                say(f"[{time.time() - started:6.1f}s] 新出现 cookie: {sorted(fresh)}  共 {len(names)} 个")
                seen |= fresh
            if all(k in names for k in ("unb", "cookie2")):
                unb = next(c["value"] for c in cookies if c["name"] == "unb")
                say("\n=== 成功：真浏览器拿到了 unb + cookie2 ===")
                say(f"    unb = {unb}")
                say(f"    目标字段命中 = {[k for k in TARGET_KEYS if k in names]}")
                say(f"    全部字段名   = {sorted(names)}")
                by_domain: dict[str, list[str]] = {}
                for c in cookies:
                    if c.get("value"):
                        by_domain.setdefault(c["domain"], []).append(c["name"])
                for dom, keys in sorted(by_domain.items()):
                    say(f"    {dom}: {sorted(keys)}")
                await browser.close()
                return 0
            if page.url != LOGIN_PAGE:
                say(f"[{time.time() - started:6.1f}s] url -> {page.url[:120]}")
            # 二维码 60~90s 就失效，不自动点刷新就会像上一轮一样白等 240s
            try:
                refresh = await page.query_selector("text=刷新二维码")
                if refresh:
                    await refresh.click()
                    say(f"[{time.time() - started:6.1f}s] 二维码已失效，已自动点刷新")
            except Exception:
                pass
            await asyncio.sleep(1)

        say(f"\n!!! {WAIT_SECONDS}s 内没凑齐 unb+cookie2；当前字段名={sorted(seen)}")
        await page.screenshot(path="scripts/.spike_1688_browser_qr_timeout.png")
        say("!!! 超时截图见 scripts/.spike_1688_browser_qr_timeout.png")
        await browser.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

