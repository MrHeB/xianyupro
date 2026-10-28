"""阶段 4-3 验收脚本：真实账号 WS 监听 + AI 回复链路验证（流程方案 §4 顺序 3）。

用法（backend 目录下，需先起 MySQL 并有已登录账号）：
    python -m scripts.verify_ws            # 取第一个 cookie_status=1 的账号，只监听
    python -m scripts.verify_ws --reply 3  # 指定账号，开启 AI 自动回复（会真实发消息！）

验收标准（流程方案 §4 表）：
    监听消息 → LLM 生成回复 → 发出。即：
    1) 用另一个账号（或手机）给该账号发消息；
    2) 脚本应打印解码后的消息字段（sId/pnmId/senderUserId/msgContent/contentType）；
    3) --reply 模式下消息入库、生成回复并发出 code=200；
    4) 付款卡片消息触发 ORDER_PAID 事件打印。
"""

import argparse
import asyncio
import logging

from sqlalchemy import select

from app.api_client import MtopClient
from app.auth.account_service import decrypt_cookie
from app.core.db import AsyncSessionLocal
from app.messaging.events import Event, EventBus, EventType
from app.messaging.manager import WsManager
from app.messaging.ws_client import XianyuWebSocketClient
from app.models import Account, AccountAuth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("verify_ws")


class PrintingBus(EventBus):
    """打印版总线：打印事件，再走默认派发（--reply 模式下引擎靠它收到事件）。"""

    async def publish(self, event: Event) -> None:
        if event.type == EventType.MESSAGE_RECEIVED:
            p = event.payload
            print(
                f"\n[MESSAGE_RECEIVED] account={event.account_id} "
                f"sId={p.get('sId')} pnmId={p.get('pnmId')} "
                f"sender={p.get('senderUserId')}({p.get('senderUserName')}) "
                f"type={p.get('contentType')} time={p.get('messageTime')}\n"
                f"  content: {str(p.get('msgContent'))[:120]}"
            )
        elif event.type == EventType.ORDER_PAID:
            p = event.payload
            print(f"\n[ORDER_PAID] account={event.account_id} goods={p.get('xyGoodsId')} "
                  f"content={str(p.get('msgContent'))[:80]}")
        await super().publish(event)


async def load_account(account_id: int | None) -> tuple[Account, str]:
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
        raise SystemExit("没有可用账号：先扫码登录（cookie_status=1）再跑本脚本")
    account, auth = row
    return account, decrypt_cookie(auth.encrypted_cookie)


class StandaloneManager(WsManager):
    """脱离全局 session_pool 的独立管理器（verify 脚本自建会话）。"""

    def start_one(self, account: Account, cookie: str, bus: EventBus) -> None:
        self.bus = bus
        client = XianyuWebSocketClient(
            account_id=account.id,
            external_uid=account.external_uid,
            cookie=cookie,
            mtop=MtopClient(cookie_provider=lambda: cookie),
            bus=bus,
        )
        import asyncio

        self._tasks[account.id] = asyncio.create_task(
            self._run_account(account.id, client), name=f"ws-verify-{account.id}"
        )
        self._clients[account.id] = client


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("account_id", nargs="?", type=int, default=None)
    parser.add_argument("--reply", action="store_true", help="开启 AI 自动回复（会真实发消息）")
    args = parser.parse_args()

    account, cookie = await load_account(args.account_id)
    print(f"账号: id={account.id} nickname={account.nickname} unb={account.external_uid}")
    print("监听中（Ctrl+C 退出）……用另一个账号给它发消息试试\n")

    bus = PrintingBus()
    manager = StandaloneManager()
    manager.start_one(account, cookie, bus)

    if args.reply:
        from app.ai.reply_engine import create_reply_engine

        engine = create_reply_engine(
            AsyncSessionLocal,
            uid_lookup=lambda _aid: account.external_uid,
            sender=lambda _aid, peer, text: manager.get_client(account.id).send_text(peer, text),
            bus=bus,
        )
        engine.start()
        print("（--reply 已开启：AI 回复将真实发出）\n")

    try:
        await asyncio.Event().wait()  # 常驻
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await manager.stop_all()
        from app.core.db import engine as db_engine

        await db_engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
