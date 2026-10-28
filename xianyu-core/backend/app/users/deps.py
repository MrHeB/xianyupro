"""FastAPI 鉴权依赖：get_current_user / require_admin。

用法：
    @router.get("/goods")
    async def list_goods(db: DbSession, user: User = Depends(get_current_user)): ...
"""

import jwt as pyjwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import DbSession
from app.models import User
from app.users.security import decode_token

bearer_scheme = HTTPBearer(auto_error=False)

_UNAUTH = HTTPException(status_code=401, detail="未登录或登录已过期")
_FORBIDDEN = HTTPException(status_code=403, detail="无权限（仅管理员可操作）")


async def get_current_user(
    db: DbSession, credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)
) -> User:
    """从 Authorization: Bearer <token> 解析当前用户。

    - 缺/坏 token → 401；用户停用/不存在 → 401；
    - 返回值：User ORM 对象（status=1）。
    """
    if credentials is None:
        raise _UNAUTH
    try:
        payload = decode_token(credentials.credentials)
    except pyjwt.PyJWTError:
        raise _UNAUTH from None

    user_id = int(payload.get("sub", "0"))
    user = await db.get(User, user_id)
    if user is None or user.status != 1 or user.deleted:
        raise _UNAUTH
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise _FORBIDDEN
    return user