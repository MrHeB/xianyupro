"""系统用户登录接口（阶段 F1）。注意：这是"后台操作者"登录，
非闲鱼账号扫码（后者在 /accounts/qr-login）。

- POST  /api/auth/register  注册普通用户（user 角色）
- POST  /api/auth/login     登录，返回 {access_token, token_type, user}
- GET   /api/auth/me        当前用户信息
- POST  /api/auth/logout    无状态登出（前端清 token；预留黑名单位）
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.api.deps import DbSession
from app.models import User
from app.users import create_access_token, hash_password, verify_password
from app.users.deps import get_current_user
from fastapi import Depends

router = APIRouter(prefix="/auth", tags=["auth"])

USERNAME_RE = r"^[A-Za-z0-9_.-]{3,64}$"


class RegisterIn(BaseModel):
    username: str
    password: str
    remark: str | None = None

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str) -> str:
        import re

        if not re.match(USERNAME_RE, v):
            raise ValueError("用户名需为 3-64 位字母/数字/._-")
        return v

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("密码至少 8 位")
        return v


class LoginIn(BaseModel):
    username: str
    password: str


def _public_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "status": user.status,
        "remark": user.remark,
        "created_at": user.created_at,
    }


@router.post("/register")
async def register(body: RegisterIn, db: DbSession) -> dict:
    """注册普通用户（user 角色）。注册策略默认开放；正式运营可改白名单。"""
    exists = await db.scalar(select(User).where(User.username == body.username))
    if exists:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role="user",
        remark=body.remark,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"user": _public_user(user)}


@router.post("/login")
async def login(body: LoginIn, db: DbSession) -> dict:
    user = await db.scalar(select(User).where(User.username == body.username))
    if user is None or user.deleted:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.status != 1:
        raise HTTPException(status_code=403, detail="账号已停用")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(user.id, username=user.username, role=user.role)
    return {"access_token": token, "token_type": "bearer", "user": _public_user(user)}


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> dict:
    return {"user": _public_user(user)}


@router.post("/logout")
async def logout() -> dict:
    """无状态 JWT：前端删除 token 即登出。返回统一提示。"""
    return {"ok": True}