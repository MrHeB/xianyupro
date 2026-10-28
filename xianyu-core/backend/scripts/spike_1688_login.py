"""一次性 spike：1688 扫码登录接口 + mtop 主机可用性探测（新增 1688 账号功能的前置验证）。

只发匿名请求，不提交任何凭证。跑法：
    .venv/Scripts/python.exe scripts/spike_1688_login.py
"""

import asyncio
import json
import random
import re
import time

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
H_PAGE = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 1688 的 havana appName 候选（闲鱼是 "xianyu"）
APP_NAMES = ["cn", "taobao", "b2c_cn", "1688", "alibaba"]
LOGIN_HOSTS = ["https://passport.1688.com", "https://login.1688.com"]

# 1688 h5 mtop 主机 + 常见 web appKey 候选
MTOP_HOSTS = ["https://h5api.m.1688.com", "https://h5api.m.taobao.com"]
APP_KEYS = ["12574478", "34839810", "30949745"]


def view_data(html: str):
    for pat in (r"window\.viewData\s*=\s*(\{.*?\});", r"var\s+viewData\s*=\s*(\{.*?\});"):
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except ValueError:
                return {"_unparseable": m.group(1)[:120]}
    return None


async def probe_login_page(client: httpx.AsyncClient) -> dict | None:
    """找能拿到 loginFormData 的 host + appName 组合，并试生成二维码。"""
    for host in LOGIN_HOSTS:
        for app_name in APP_NAMES:
            params = {
                "lang": "zh_cn",
                "appName": app_name,
                "appEntrance": "web",
                "styleType": "vertical",
                "bizParams": "",
                "notLoadSsoView": "false",
                "notKeepLogin": "false",
                "isMobile": "false",
                "qrCodeFirst": "true",
                "rnd": str(random.random()),
            }
            url = f"{host}/mini_login.htm"
            try:
                r = await client.get(url, headers=H_PAGE, params=params, follow_redirects=True)
            except Exception as exc:
                print(f"[login] {host} appName={app_name} EXC {type(exc).__name__}")
                continue
            vd = view_data(r.text)
            form = (vd or {}).get("loginFormData") if isinstance(vd, dict) else None
            print(
                f"[login] {host} appName={app_name} status={r.status_code} "
                f"len={len(r.text)} viewData={'Y' if vd else 'N'} loginFormData={'Y' if form else 'N'}"
            )
            if vd and not form:
                print(f"    viewData keys={list(vd)[:8]} peek={r.text[:150]!r}")
            if not form:
                continue
            form["umidTag"] = "SERVER"
            print(f"    loginFormData keys={list(form)}")
            # 试二维码生成
            gen = f"{host}/newlogin/qrcode/generate.do"
            try:
                g = await client.get(
                    gen,
                    headers={**H_PAGE, "Accept": "application/json, text/plain, */*", "Referer": host + "/"},
                    params=form,
                )
                body = g.text
                print(f"    [qr-generate] status={g.status_code} len={len(body)}")
                try:
                    data = json.loads(body).get("content", {}).get("data") or {}
                except ValueError:
                    print(f"      非 JSON，peek={body[:200]!r}")
                    continue
                print(f"      data keys={list(data)}")
                if data.get("codeContent"):
                    print(f"      [OK] codeContent={data['codeContent'][:90]}")
                    print(f"      t={bool(data.get('t'))} ck={bool(data.get('ck'))} resultCode={data.get('resultCode')}")
                    # 轮询一次，确认 query.do 也通（未扫码应为 NEW）
                    form["t"] = data.get("t", "")
                    form["ck"] = data.get("ck", "")
                    q = await client.post(
                        f"{host}/newlogin/qrcode/query.do",
                        headers={**H_PAGE, "Accept": "application/json, text/plain, */*", "Referer": host + "/"},
                        data=form,
                    )
                    print(f"      [qr-query] status={q.status_code} body={q.text[:200]}")
                    return {"host": host, "appName": app_name, "form": form}
                print(f"      [NO] 无 codeContent，全量={body[:300]}")
            except Exception as exc:
                print(f"    [qr-generate] EXC {type(exc).__name__}: {exc}")
            return {"host": host, "appName": app_name, "form": form}
    return None


async def probe_mtop(client: httpx.AsyncClient) -> None:
    """1688 mtop 主机是否下发 _m_h5_tk（决定以后搜索/详情能否走 JSON 接口）。"""
    for host in MTOP_HOSTS:
        for app_key in APP_KEYS:
            data_str = '{"t":1}'
            api = "mtop.common.getTimestamp"
            url = f"{host}/h5/{api}/1.0/"
            t_ms = int(time.time() * 1000)
            form = {
                "jsv": "2.7.2",
                "appKey": app_key,
                "t": str(t_ms),
                "sign": "",
                "v": "1.0",
                "type": "originaljson",
                "dataType": "json",
                "api": api,
                "data": data_str,
            }
            try:
                await client.get(url, headers=H_PAGE)
                r = await client.post(url, headers=H_PAGE, data=form)
                tk = client.cookies.get("_m_h5_tk")
                print(
                    f"[mtop] {host} appKey={app_key} status={r.status_code} "
                    f"_m_h5_tk={'Y' if tk else 'N'} body={r.text[:120]}"
                )
            except Exception as exc:
                print(f"[mtop] {host} appKey={app_key} EXC {type(exc).__name__}")


async def probe_detail_with_cookie(client: httpx.AsyncClient, kw: str) -> None:
    """带 _m_h5_tk/cookie2 的裸会话再打一次详情页，看是否仍被 x5sec punish。"""
    url = f"https://detail.1688.com/offer/{kw}.html"
    r = await client.get(url, headers={**H_PAGE, "Referer": "https://www.1688.com/"})
    punished = "_____tmd_____" in r.text or "x5secdata" in r.text
    print(f"[detail] status={r.status_code} len={len(r.text)} punished={punished}")


async def main() -> None:
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
        await probe_mtop(client)
        got = await probe_login_page(client)
        print(f"\n=== login 结果 === {json.dumps({k: v for k, v in (got or {}).items() if k != 'form'}, ensure_ascii=False)}")
        await probe_detail_with_cookie(client, "635691212077")


if __name__ == "__main__":
    asyncio.run(main())
