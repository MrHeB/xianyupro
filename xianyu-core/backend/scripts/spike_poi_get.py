"""真机取证：`mtop.taobao.idle.local.poi.get` 到底返回什么结构。

起因：发布链路第 3 步报「账号无默认收货地址（poi.get 空）」。有两种可能——
账号真没地址，或者我们取错了层级（我们现在在**每条地址里**找 `selectedPoi`，
而 goofish-cli 的参考实现在 **data 顶层**找；入参我们传 `{}`，参考传经纬度）。
这条脚本打一发只读请求、只出结构，用来把结论钉死。

跑法（必须在 backend 容器内：COOKIE_CRYPTO_SECRET 在那边）：
    docker compose cp backend/scripts/spike_poi_get.py backend:/app/scripts/
    docker compose exec -T backend python -m scripts.spike_poi_get
    docker compose exec -T backend python -m scripts.spike_poi_get 3   # 指定 account_id

输出纪律：**不打印 Cookie**，也不打印门牌详址/收件人/手机号（PII 集合见 `_REDACT`），
只出字段名、类型和行政区级粗值——够定位结构，不够还原一个人。
"""

import asyncio
import sys

from sqlalchemy import select

from app.api_client import MtopClient
from app.api_client.errors import XianyuApiError
from app.auth.account_service import decrypt_cookie
from app.core.db import AsyncSessionLocal
from app.models import Account, AccountAuth

POI_API = "mtop.taobao.idle.local.poi.get"
POI_VERSION = "1.0"

# 粗粒度行政区可以出值，落到"某人住哪"的一律打码
_REDACT = {
    "address",
    "addressdetail",
    "addressforedition",
    "fullname",
    "receiver",
    "mobile",
    "mobilephone",
    "phonenumber",
    "phone",
    "tel",
    "contactname",
    "detailaddress",
    "housenumber",
    "confidence",
    "usertag",
}


def preview(value):
    """标量原样（截断），dict/list 只报形状，PII 键打码。"""
    if isinstance(value, dict):
        return f"<dict keys={sorted(value)}>"
    if isinstance(value, list):
        head = value[0] if value else None
        return f"<list len={len(value)} first={preview(head) if head is not None else '-'}>"
    text = repr(value)
    return f"{text} :{type(value).__name__}" if len(text) <= 60 else text[:60] + "…"


def dump(label: str, data: dict) -> None:
    print(f"\n--- {label} ---")
    print("顶层键:", sorted(data))
    addrs = data.get("commonAddresses") or []
    print(f"commonAddresses: len={len(addrs)}")
    for i, addr in enumerate(addrs):
        print(f"  [{i}] 键 = {sorted(addr)}")
        for key in sorted(addr):
            shown = "<redacted>" if key.lower() in _REDACT else preview(addr[key])
            print(f"      {key} = {shown}")
    # selectedPoi 到底在哪一层，是本次取证的正题：顶层有值就证明「账号没地址」是误报
    if isinstance(data.get("selectedPoi"), dict):
        print("data.selectedPoi（顶层）逐字段：")
        for key in sorted(data["selectedPoi"]):
            val = data["selectedPoi"][key]
            shown = "<redacted>" if key.lower() in _REDACT else preview(val)
            print(f"      {key} = {shown}")
    else:
        print("data.selectedPoi（顶层）:", preview(data.get("selectedPoi", "缺失")))
    for i, addr in enumerate(addrs):
        print(f"  [{i}].selectedPoi:", addr.get("selectedPoi", "缺失"))
    # 兜底字段名一并列出，防闲鱼叫别的名
    for alt in ("poi", "defaultAddress", "addresses", "senderAddress", "userAddress"):
        if alt in data:
            print(f"额外候选 {alt}:", preview(data[alt]))


async def call(client: MtopClient, label: str, payload: dict) -> None:
    try:
        data = await client.call(POI_API, POI_VERSION, payload)
    except XianyuApiError as exc:
        print(f"\n--- {label} --- 失败 kind={exc.kind}：{exc}")
        return
    dump(f"{label}（入参 {payload}）", data if isinstance(data, dict) else {"_": data})


async def main() -> None:
    account_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    async with AsyncSessionLocal() as db:
        # 刻意不筛 cookie_status：本账号此前出现过「poi.get 已跑通到第 3 步」但库里的
        # cookie_status 仍是 0 的情况（会话在 session_pool 内存里，DB 只是快照）。
        # 到底能不能用，让 poi.get 自己回话，比看标记准。
        stmt = select(Account, AccountAuth).join(AccountAuth, AccountAuth.account_id == Account.id)
        if account_id is not None:
            stmt = stmt.where(Account.id == account_id)
        row = (await db.execute(stmt.order_by(Account.id))).first()
    if row is None:
        print("库里没有带 auth 行的账号：先扫码登录再跑本脚本")
        sys.exit(1)
    account, auth = row
    cookie = decrypt_cookie(auth.encrypted_cookie)
    print(
        f"账号 id={account.id} nickname={account.nickname} unb={account.external_uid} "
        f"account.status={account.status} cookie_status={auth.cookie_status} "
        f"last_login_at={auth.last_login_at}"
    )

    client = MtopClient(cookie_provider=lambda: cookie)
    try:
        # 三种入参形状真机结果一致（{} 与带/不带经纬度返回同一结构），坐标不影响本接口，
        # 所以取证只留一发；再打两次对账号没好处。
        await call(client, "A 默认入参", {})
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
