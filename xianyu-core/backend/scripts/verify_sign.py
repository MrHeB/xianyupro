"""阶段 4-2 验收脚本：用只读接口验证 mtop 签名正确（流程方案 §4 顺序 2）。

用法（backend 目录下，需先起 MySQL 并有已登录账号）：
    python -m scripts.verify_sign            # 取第一个 cookie_status=1 的账号
    python -m scripts.verify_sign 3          # 指定 account_id

验收标准（流程方案 §4 表）：
    在售列表 mtop.idle.web.xyh.item.list + 已售订单 mtop.taobao.idle.trade.merchant.sold.get
    两个只读接口返回 SUCCESS → 签名算法/公共参数/登录态注入全部正确。
"""

import asyncio
import sys

from sqlalchemy import select

from app.api_client import MtopClient
from app.api_client.errors import XianyuApiError
from app.auth.account_service import decrypt_cookie
from app.core.db import AsyncSessionLocal
from app.models import Account, AccountAuth

# 只读接口（protocol.md §3.3 / §5）
ITEM_LIST_API = ("mtop.idle.web.xyh.item.list", "1.0")
SOLD_ORDER_API = ("mtop.taobao.idle.trade.merchant.sold.get", "1.0")


async def load_account(account_id: int | None) -> tuple[Account, str]:
    """从库取账号并解密 Cookie。"""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(Account, AccountAuth)
            .join(AccountAuth, AccountAuth.account_id == Account.id)
            .where(Account.status == 1, AccountAuth.cookie_status == 1)
        )
        if account_id is not None:
            stmt = stmt.where(Account.id == account_id)
        row = (await db.execute(stmt.order_by(Account.id))).first()
    if row is None:
        print("没有可用账号：先扫码登录（cookie_status=1）再跑本脚本")
        sys.exit(1)
    account, auth = row
    return account, decrypt_cookie(auth.encrypted_cookie)


async def verify(account: Account, cookie: str) -> None:
    unb = account.external_uid
    client = MtopClient(cookie_provider=lambda: cookie)
    try:
        # ① 在售列表
        api, version = ITEM_LIST_API
        data = await client.call(api, version, {"userId": unb, "pageNumber": 1, "pageSize": 20})
        cards = data.get("cardList") or []
        titles = [
            (c.get("cardData") or {}).get("title", "?") for c in cards[:5]
        ]
        print(f"[OK] 在售列表 {api}: 共 {len(cards)} 条（前 {min(5, len(cards))} 个标题）")
        for t in titles:
            print(f"     - {t}")

        # ② 已售订单
        api, version = SOLD_ORDER_API
        data = await client.call(
            api,
            version,
            {
                "needGroupInfo": True,
                "pageNumber": 1,
                "pageSize": 10,
                "lastEndRow": "",
                "userId": unb,
            },
        )
        # 已售列表结构按账号而异，只打顶层键数确认拿到了 data
        print(f"[OK] 已售订单 {api}: data 顶层键 = {sorted(data.keys())[:10]}")

        print("\n✅ 验收通过：签名算法 + 公共参数 + 登录态注入全部正确")
    except XianyuApiError as exc:
        print(f"\n❌ {exc}")
        print(f"   kind={exc.kind} retryable={exc.retryable} ret={exc.ret}")
        sys.exit(2)
    finally:
        await client.aclose()


async def main() -> None:
    account_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
    account, cookie = await load_account(account_id)
    print(f"账号: id={account.id} nickname={account.nickname} unb={account.external_uid}")
    await verify(account, cookie)


if __name__ == "__main__":
    asyncio.run(main())
