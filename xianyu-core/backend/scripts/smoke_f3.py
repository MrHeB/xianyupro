"""F3 冒烟：本地起 uvicorn → register/login → GET/PUT/GET/POST /settings/llm → 收尾清理进程。

用法：backend 目录下运行
    .venv\\Scripts\\python.exe scripts\\smoke_f3.py
"""
import asyncio
import os
import subprocess

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
PY = os.path.join(BACKEND, ".venv", "Scripts", "python.exe")
BASE = "http://127.0.0.1:9101"


async def main() -> None:
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--port", "9101", "--log-level", "warning"],
        cwd=BACKEND,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            # 等就绪
            up = False
            for _ in range(30):
                try:
                    r = await c.get(f"{BASE}/api/health")
                    if r.status_code == 200:
                        up = True
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            if not up:
                raise SystemExit("后端未在 30s 内就绪")

            # 1) 注册 admin（已存在则跳过）
            try:
                r = await c.post(
                    f"{BASE}/api/auth/register",
                    json={"username": "smoke_admin", "password": "smoke123", "role": "admin"},
                )
                print("register:", r.status_code, r.text[:120])
            except httpx.HTTPError as e:
                print("register err:", e)

            # 2) 登录
            r = await c.post(
                f"{BASE}/api/auth/login",
                json={"username": "smoke_admin", "password": "smoke123"},
            )
            r.raise_for_status()
            login = r.json()
            h = {"Authorization": f"Bearer {login['access_token']}"}
            print("login OK role=", login["user"]["role"])

            # 3) GET 初始配置（脱敏）
            r = await c.get(f"{BASE}/api/settings/llm", headers=h)
            r.raise_for_status()
            cfg = r.json()
            print("GET:", {k: cfg[k] for k in ("base_url", "model", "reply_mode", "api_key_set", "max_daily_replies")})

            # 4) 未登录 GET → 401
            anon = await c.get(f"{BASE}/api/settings/llm")
            print("anonymous GET ->", anon.status_code, "(期望 401)")

            # 5) PUT 保存（DB 覆盖）
            put_body = {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "reply_mode": "auto",
                "max_daily_replies": 250,
                "blocked_keywords": "转人工,投诉",
                "system_prompt": "你是闲鱼客服助手",
            }
            r = await c.put(f"{BASE}/api/settings/llm", headers=h, json=put_body)
            r.raise_for_status()
            up = r.json()
            print("PUT:", {k: up[k] for k in ("base_url", "model", "reply_mode", "max_daily_replies")})

            # 6) 再 GET 确保持久化且 Key 未回显
            r = await c.get(f"{BASE}/api/settings/llm", headers=h)
            r.raise_for_status()
            cfg2 = r.json()
            print("reGET:", {k: cfg2[k] for k in ("base_url", "model", "reply_mode", "api_key_set", "max_daily_replies")})
            assert cfg2["base_url"] == "https://api.deepseek.com/v1", "PUT 未持久化"

            # 7) 连通测试（无真实 Key → ok=false 而非 500）
            t = await c.post(f"{BASE}/api/settings/llm/test", headers=h)
            tt = t.json() if t.status_code < 400 else {"http": t.status_code}
            print("test: status=%s ok=%s" % (t.status_code, tt.get("ok")))

            # 8) 权限：普通用户 PUT → 403，GET OK
            await c.post(
                f"{BASE}/api/auth/register",
                json={"username": "smoke_user", "password": "smoke123", "role": "user"},
            )
            lu = await c.post(
                f"{BASE}/api/auth/login",
                json={"username": "smoke_user", "password": "smoke123"},
            )
            hu = {"Authorization": f"Bearer {lu.json()['access_token']}"}
            pu = await c.put(f"{BASE}/api/settings/llm", json={"model": "x"}, headers=hu)
            print("user PUT ->", pu.status_code, "(期望 403)")
            g3 = await c.get(f"{BASE}/api/settings/llm", headers=hu)
            print("user GET ->", g3.status_code, "(期望 200)")

            print("\n== SMOKE F3: PASS ==")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())