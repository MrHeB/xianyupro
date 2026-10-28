"""探针：1688 mini_login.htm 在不同 appName/appEntrance 下给出的 loginFormData 长什么样。

为什么需要：扫码 CONFIRMED 后拿不到 unb/cookie2，怀疑是 appName=taobao 把登录态
发到了 .taobao.com 域，而 1688 侧要靠 returnUrl 那一跳换。这一步不触网到用户手机，
只看登录页自己声明的回跳目标，就能判断该用哪个 appName。
"""

import asyncio
import json
import re

import httpx

LOGIN_PAGE = "https://passport.1688.com/mini_login.htm"
H = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

COMBOS = [
    {"appName": "1688", "appEntrance": "web"},
    {
        "appName": "1688",
        "appEntrance": "web",
        "returnUrl": "https://login.1688.com/member/jump.htm?target=https%3A%2F%2Fwww.1688.com%2F",
    },
    {"appName": "1688", "appEntrance": "web", "returnUrl": "https://www.1688.com/"},
    {
        "appName": "taobao",
        "appEntrance": "web",
        "returnUrl": "https://login.1688.com/member/jump.htm?target=https%3A%2F%2Fwww.1688.com%2F",
    },
]


async def probe(client: httpx.AsyncClient, combo: dict) -> None:
    params = {
        "lang": "zh_cn",
        "styleType": "vertical",
        "bizParams": "",
        "notLoadSsoView": "false",
        "notKeepLogin": "false",
        "isMobile": "false",
        "qrCodeFirst": "true",
        **combo,
    }
    try:
        resp = await client.get(LOGIN_PAGE, headers=H, params=params)
    except Exception as exc:
        print(f"[NO] {combo} -> {type(exc).__name__}: {exc}")
        return
    match = re.search(r"window\.viewData\s*=\s*(\{.*?\});", resp.text, re.DOTALL) or re.search(
        r"var\s+viewData\s*=\s*(\{.*?\});", resp.text, re.DOTALL
    )
    label = f"{combo['appName']}/{combo['appEntrance']}/returnUrl={'Y' if combo.get('returnUrl') else '-'}"
    if not match:
        print(f"[NO] {label} status={resp.status_code} bytes={len(resp.text)} 无 viewData")
        return
    view = json.loads(match.group(1))
    form = view.get("loginFormData", {})
    print(f"[OK] {label} status={resp.status_code} formDataKeys={sorted(form)}")
    # viewData 顶层与 loginFormData 各有一个 returnUrl：只有顶层可能带换票地址
    for key in ("appName", "appEntrance", "returnUrl", "targetSite", "site", "loginSite", "mainPage"):
        if key in form:
            print(f"       form.{key} = {str(form[key])[:180]}")
        if key in view:
            print(f"       view.{key} = {str(view[key])[:180]}")
    print(f"       viewDataKeys={sorted(view)}")


async def main() -> None:
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for combo in COMBOS:
            await probe(client, combo)


if __name__ == "__main__":
    asyncio.run(main())
