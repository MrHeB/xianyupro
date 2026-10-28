"""运行设置接口（阶段 4-3 实测 + 阶段 F3 LLM 配置页）。

- GET /reply-mode          AI 回复模式 + 是否配了 key（读 DB 覆盖后的运行时值）
- PUT /reply-mode          （admin）切模式——写 DB 持久化
- GET /llm                 脱敏只读（任何登录用户）
- PUT /llm                 （admin）写 DB + 免重启生效
- POST /llm/test           （admin）连通测试
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import DbSession
from app.models import User
from app.settings import get_llm_config, public_config, save_llm_config, test_llm_connection
from app.users.deps import get_current_user, require_admin

router = APIRouter(prefix="/settings", tags=["settings"])

VALID_MODES = ("auto", "hybrid", "manual")


class ReplyModeIn(BaseModel):
    mode: str


class LlmConfigIn(BaseModel):
    """任意字段皆可空：空串/None = 清除 DB 覆盖，回退 .env。"""

    api_key: str | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=128)
    reply_mode: str | None = None
    max_daily_replies: int | None = Field(default=None, ge=1)
    blocked_keywords: str | None = None
    work_hour_start: int | None = Field(default=None, ge=0, le=23)
    work_hour_end: int | None = Field(default=None, ge=1, le=24)
    history_max_messages: int | None = Field(default=None, ge=1, le=50)
    max_message_age_seconds: int | None = Field(default=None, ge=0)
    system_prompt: str | None = Field(default=None, max_length=4000)

    @classmethod
    def to_updates(cls, m: "LlmConfigIn") -> dict:
        d = m.model_dump(exclude_unset=True)
        kmap = {
            "api_key": "llm_api_key",
            "base_url": "llm_base_url",
            "model": "llm_model",
            "reply_mode": "ai_reply_mode",
            "max_daily_replies": "ai_max_daily_replies",
            "blocked_keywords": "ai_blocked_keywords",
            "work_hour_start": "ai_work_hour_start",
            "work_hour_end": "ai_work_hour_end",
            "history_max_messages": "ai_history_max_messages",
            "max_message_age_seconds": "ai_max_message_age_seconds",
            "system_prompt": "ai_system_prompt",
        }
        return {key: d[field] for field, key in kmap.items() if field in d}


@router.get("/reply-mode")
async def get_reply_mode(db: DbSession, user: User = Depends(get_current_user)) -> dict:
    cfg = await get_llm_config(db)
    return {
        "mode": cfg.reply_mode,
        "max_daily_replies": cfg.max_daily_replies,
        "blocked_keywords": cfg.blocked_keywords,
        "model": cfg.model if cfg.configured else "(未配置 API Key)",
        "llm_configured": cfg.configured,
    }


@router.put("/reply-mode")
async def set_reply_mode(
    body: ReplyModeIn, db: DbSession, user: User = Depends(require_admin)
) -> dict:
    if body.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode 必须是 {VALID_MODES} 之一")
    await save_llm_config(db, {"ai_reply_mode": body.mode})
    await db.commit()
    cfg = await get_llm_config(db)
    return {"mode": cfg.reply_mode}


@router.get("/llm")
async def get_llm(db: DbSession, user: User = Depends(get_current_user)) -> dict:
    return await public_config(db)


@router.put("/llm")
async def update_llm(
    body: LlmConfigIn, db: DbSession, user: User = Depends(require_admin)
) -> dict:
    if body.reply_mode is not None and body.reply_mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"reply_mode 必须是 {VALID_MODES} 之一")
    await save_llm_config(db, LlmConfigIn.to_updates(body))
    await db.commit()
    return await public_config(db)


@router.post("/llm/test")
async def test_llm(db: DbSession, user: User = Depends(require_admin)) -> dict:
    return await test_llm_connection(db)