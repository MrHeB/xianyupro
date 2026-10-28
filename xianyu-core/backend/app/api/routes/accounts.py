"""账号管理接口：扫码登录 + 会话池（阶段 4-1）；登录用户的数据范围见 users/scoping。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbSession
from app.auth import qr_login_service, upsert_account_from_cookies
from app.auth.account_service import decrypt_cookie
from app.auth.qr_login import QrLoginError
from app.models import User
from app.users.deps import get_current_user
from app.users.scoping import ensure_account_scope

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("")
async def list_accounts(db: DbSession, user: User = Depends(get_current_user)) -> list[dict]:
    """账号列表（脱敏：只暴露昵称/状态，不含登录态——前端与 Agent 都不碰 Cookie）。"""
    from app.models import Account, AccountAuth, AccountReplyConfig

    stmt = (
        select(Account, AccountAuth, AccountReplyConfig.persona_prompt)
        .join(AccountAuth, AccountAuth.account_id == Account.id)
        .outerjoin(
            AccountReplyConfig, AccountReplyConfig.account_id == Account.id
        )
        .where(Account.deleted == 0)
        .order_by(Account.id)
    )
    if user.role != "admin":
        stmt = stmt.where(Account.user_id == user.id)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "id": a.id,
            "nickname": a.nickname,
            "external_uid": a.external_uid,
            "status": a.status,
            "cookie_status": auth.cookie_status,
            "fish_shop_user": bool(a.fish_shop_user),
            "last_login_at": auth.last_login_at,
            "has_persona": bool((persona or "").strip()),
        }
        for a, auth, persona in rows
    ]


@router.get("/guard")
async def guard_status(db: DbSession, user: User = Depends(get_current_user)) -> list[dict]:
    """写操作止损熔断状态（风控手册 §4 R-9：熔断必须前端可见，不能只在日志里）。

    key 形如 `publish:3` / `reply:3` / `delivery:3`，三域独立计数（R-3）。
    解锁路径：暂停到期 / 一次成功写入 / 该账号重新扫码（Cookie 变新）。
    """
    from app.core.failure_guard import failure_guard

    ids = None
    if user.role != "admin":
        from sqlalchemy import select

        from app.models import Account

        ids = {
            str(row[0])
            for row in (
                await db.execute(select(Account.id).where(Account.user_id == user.id))
            ).all()
        }
    out = []
    for key in sorted(failure_guard.status()):
        domain, _, acct = key.partition(":")
        if ids is not None and acct not in ids:
            continue
        entry = failure_guard.status(key)
        pause = failure_guard.is_paused(key)
        out.append(
            {
                "key": key,
                "domain": domain,
                "account_id": int(acct) if acct.isdigit() else None,
                "failures": entry.get("failures", 0),
                "paused": pause is not None,
                "paused_until": getattr(pause, "until", None),
                "remaining_seconds": getattr(pause, "remaining_seconds", 0),
            }
        )
    return out


@router.post("/qr-login")
async def start_qr_login(user: User = Depends(get_current_user)) -> dict:
    """生成扫码登录二维码。返回 {session_id, qr_image(base64 data URI)}。"""
    try:
        return await qr_login_service.generate()
    except QrLoginError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/qr-login/{session_id}")
async def poll_qr_login(
    session_id: str, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """轮询扫码状态（前端 1s 一次）。

    confirmed 时自动入库（Cookie 加密 + 绑定当前用户 + 会话池注册）并返回账号信息。
    """
    result = await qr_login_service.poll(session_id)
    if result.get("status") != "confirmed":
        return result

    cookies = await qr_login_service.take_cookies(session_id)
    if not cookies:
        return result

    try:
        account = await upsert_account_from_cookies(db, cookies, owner_user_id=user.id)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"账号入库/绑定失败: {exc}") from exc

    # 登录成功即拉起该账号的 WS 消息监听（阶段 4-3；已启动则幂等跳过）
    try:
        from app.auth.session_pool import session_pool
        from app.messaging.manager import ws_manager

        session = session_pool.get(account.id)
        if session:
            ws_manager.start_account(session)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "WS 监听拉起失败 accountId=%d（连接恢复后由 lifespan 兜底）", account.id
        )

    return {
        **result,
        "account_id": account.id,
        "nickname": account.nickname,
    }


class StatusIn(BaseModel):
    status: int = Field(ge=0, le=1)


@router.patch("/{account_id}/status")
async def set_account_status(
    account_id: int, body: StatusIn, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """启用/停用（下线）一个闲鱼账号。

    停用：status=0 + 当场停该账号 WS + 移出会话池——只改 status 不这些的话，进程内那一
    路还在收消息、还会自动回复。启用：status=1 + 用库里 Cookie 重新注册会话池并拉起 WS。
    """
    from app.auth.session_pool import session_pool
    from app.messaging.manager import ws_manager
    from app.models import Account, AccountAuth

    await ensure_account_scope(db, user, account_id)
    account = await db.get(Account, account_id)
    if account is None or account.deleted:
        raise HTTPException(404, f"账号不存在：{account_id}")

    if body.status == account.status:
        return {"id": account.id, "status": account.status, "changed": False}

    if body.status == 0:
        await ws_manager.stop_account(account_id)
        await session_pool.remove(account_id)
        account.status = 0
        await db.commit()
        return {"id": account.id, "status": 0, "changed": True, "note": "已下线：停止收消息与自动回复"}

    auth = (
        await db.execute(select(AccountAuth).where(AccountAuth.account_id == account_id))
    ).scalar_one_or_none()
    if auth is None or auth.cookie_status != 1:
        raise HTTPException(409, "该账号 Cookie 不可用（未扫码或已过期），请重新扫码后再启用")
    try:
        cookie_text = decrypt_cookie(auth.encrypted_cookie)
    except Exception as exc:
        raise HTTPException(409, f"Cookie 解密失败，无法启用：{exc}") from exc
    account.status = 1
    await db.commit()
    session_pool.register(account.id, account.external_uid, cookie_text)
    session = session_pool.get(account.id)
    if session:
        ws_manager.start_account(session)
    return {"id": account.id, "status": 1, "changed": True, "note": "已上线：恢复收消息与自动回复"}


class PersonaIn(BaseModel):
    persona_prompt: str = Field(default="", max_length=4000)


@router.get("/{account_id}/reply-config")
async def get_reply_config(
    account_id: int, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """本账号 AI 人设 + 最终生效的系统提示词预览（全局打底 + 人设叠加）。"""
    from app.models import Account, AccountReplyConfig
    from app.settings import compose_system_prompt, get_llm_config

    await ensure_account_scope(db, user, account_id)
    if await db.get(Account, account_id) is None:
        raise HTTPException(404, f"账号不存在：{account_id}")
    persona = (
        await db.execute(
            select(AccountReplyConfig.persona_prompt).where(
                AccountReplyConfig.account_id == account_id
            )
        )
    ).scalar_one_or_none() or ""
    cfg = await get_llm_config(db)
    return {
        "persona_prompt": persona,
        "global_system_prompt": cfg.system_prompt,
        "effective_system_prompt": compose_system_prompt(cfg.system_prompt, persona),
    }


@router.put("/{account_id}/reply-config")
async def save_reply_config(
    account_id: int,
    body: PersonaIn,
    db: DbSession,
    user: User = Depends(get_current_user),
) -> dict:
    """写本账号人设（空文本 = 删除覆盖，回退全局提示词）。下一条消息即生效，无需重启。"""
    from app.models import Account, AccountReplyConfig

    await ensure_account_scope(db, user, account_id)
    if await db.get(Account, account_id) is None:
        raise HTTPException(404, f"账号不存在：{account_id}")

    row = (
        await db.execute(
            select(AccountReplyConfig).where(AccountReplyConfig.account_id == account_id)
        )
    ).scalar_one_or_none()
    text = body.persona_prompt.strip()
    if not text:
        if row is not None:
            await db.delete(row)
    elif row is None:
        db.add(AccountReplyConfig(account_id=account_id, persona_prompt=text))
    else:
        row.persona_prompt = text
    await db.commit()
    return {"account_id": account_id, "has_persona": bool(text)}


@router.delete("/{account_id}")
async def delete_account(
    account_id: int, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """解绑（软删除）闲鱼账号：停 WS + 移出会话池 + deleted=1。已采商品与历史不受影响。"""
    from app.auth.session_pool import session_pool
    from app.messaging.manager import ws_manager
    from app.models import Account

    await ensure_account_scope(db, user, account_id)
    account = await db.get(Account, account_id)
    if account is None or account.deleted:
        raise HTTPException(404, f"账号不存在：{account_id}")
    await ws_manager.stop_account(account_id)
    await session_pool.remove(account_id)
    account.deleted = 1
    account.status = 0
    await db.commit()
    return {"id": account_id, "deleted": True}
