"""1688 货源账号接口：扫码登录 + 列表 + 解绑。

与 /api/accounts（闲鱼发布账号）分开：两套凭据、两个域名、两种生命周期，
混在一个 router 里会让"哪个 Cookie 能发哪个站"这件事重新变成隐性知识。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from urllib.parse import unquote

from app.api.deps import DbSession
from app.auth.alibaba_account_service import (
    AlibabaAccountError,
    upsert_alibaba_account_from_cookies,
)
from app.auth.alibaba_login import AlibabaLoginError, alibaba_qr_login_service
from app.models import AlibabaAccount, User
from app.users.deps import get_current_user

router = APIRouter(prefix="/alibaba", tags=["alibaba"])


def _visible_to(user: User):
    """非 admin 只看自己绑定的货源账号（与闲鱼账号列表同一口径）。"""
    return None if user.role == "admin" else user.id


@router.get("/accounts")
async def list_alibaba_accounts(
    db: DbSession, user: User = Depends(get_current_user)
) -> list[dict]:
    """货源账号列表（脱敏：不回传 Cookie，只回状态与最后登录时间）。"""
    stmt = select(AlibabaAccount).where(AlibabaAccount.deleted == 0).order_by(AlibabaAccount.id)
    owner = _visible_to(user)
    if owner is not None:
        stmt = stmt.where(AlibabaAccount.user_id == owner)
    return [
        {
            "id": a.id,
            "external_uid": a.external_uid,
            "nickname": a.nickname,
            "status": a.status,
            "cookie_status": a.cookie_status,
            "last_login_at": a.last_login_at,
            "remark": a.remark,
        }
        for a in (await db.execute(stmt)).scalars().all()
    ]


@router.post("/qr-login")
async def start_alibaba_qr_login(user: User = Depends(get_current_user)) -> dict:
    """生成 1688 扫码登录二维码。返回 {session_id, qr_image(base64 data URI)}。"""
    try:
        return await alibaba_qr_login_service.generate()
    except AlibabaLoginError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/qr-login/{session_id}")
async def poll_alibaba_qr_login(
    session_id: str, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """轮询扫码状态（前端 1s 一次）。confirmed 时加密入库并绑定当前用户。

    缺 unb/cookie2 时不入库——这种 Cookie 打详情页仍会被 punish，存进去只会让人
    误以为"已登录"，不如把 missing 报给前端。
    """
    result = await alibaba_qr_login_service.poll(session_id)
    if result.get("status") != "confirmed":
        return result

    cookies = await alibaba_qr_login_service.take_cookies(session_id)
    if not cookies:
        return result
    missing = result.get("missing") or []
    if "unb" in missing or "cookie2" in missing:
        raise HTTPException(
            502,
            f"扫码成功但未拿到 1688 登录态（缺 {', '.join(missing)}）；"
            "若浏览器提示二次校验，请完成后重试",
        )

    try:
        account = await upsert_alibaba_account_from_cookies(
            db,
            cookies,
            owner_user_id=user.id,
            nickname=unquote(cookies.get("tracknick") or "") or None,
        )
    except AlibabaAccountError as exc:
        raise HTTPException(status_code=409, detail=f"货源账号入库/绑定失败: {exc}") from exc

    return {
        "status": "confirmed",
        "missing": missing,
        "nickname": result.get("nickname") or "",
        "unb": result.get("unb") or account.external_uid,
        "account": {"id": account.id, "external_uid": account.external_uid},
    }


@router.delete("/accounts/{alibaba_account_id}")
async def delete_alibaba_account(
    alibaba_account_id: int, db: DbSession, user: User = Depends(get_current_user)
) -> dict:
    """软删货源账号（只解绑，不动用它采过的商品）。"""
    row = await db.get(AlibabaAccount, alibaba_account_id)
    if row is None or row.deleted:
        raise HTTPException(404, "货源账号不存在")
    owner = _visible_to(user)
    if owner is not None and row.user_id != owner:
        raise HTTPException(403, "无权解绑该货源账号")
    row.deleted = 1
    await db.commit()
    return {"deleted": row.id}
