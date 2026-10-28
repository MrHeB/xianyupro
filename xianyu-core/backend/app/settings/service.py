"""运行时设置 service（阶段 F3）：DB（app_setting）覆盖 .env，免重启生效。

取值顺序：app_setting 有值 → 用它；否则 config.Settings 默认（.env）。
get_llm_config() 返回 runtime 合并结果，settings API 用；
get_llm_config_for_account() 再叠加该账号人设，reply_engine 用（每个闲鱼号说话风格不同）。
"""

import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as env_settings
from app.models import AppSetting


def _nullable_int(raw: str) -> int | None:
    s = (raw or "").strip()
    return int(s) if s else None


# key → (config 字段名, 解析函数)；未在 DB 时回退 env 对应字段
_LLM_DEFS: dict[str, tuple[str, callable, str]] = {
    "llm_api_key": ("api_key", str, "openai_api_key"),
    "llm_base_url": ("base_url", str, "openai_base_url"),
    "llm_model": ("model", str, "openai_model"),
    "ai_reply_mode": ("reply_mode", str, "ai_reply_mode"),
    "ai_max_daily_replies": ("max_daily_replies", int, "ai_max_daily_replies"),
    "ai_blocked_keywords": ("blocked_keywords", str, "ai_blocked_keywords"),
    "ai_work_hour_start": ("work_hour_start", _nullable_int, "ai_work_hour_start"),
    "ai_work_hour_end": ("work_hour_end", _nullable_int, "ai_work_hour_end"),
    "ai_history_max_messages": ("history_max_messages", int, "ai_history_max_messages"),
    "ai_max_message_age_seconds": ("max_message_age_seconds", int, "ai_max_message_age_seconds"),
    "ai_system_prompt": ("system_prompt", str, "ai_system_prompt"),
}

# API 读写允许的 key（PUT 时只接受这些；空字符串 = 清除覆盖，回退 .env）
WRITABLE_KEYS = set(_LLM_DEFS)


def _nullable_int(raw: str) -> int | None:
    s = (raw or "").strip()
    return int(s) if s else None


@dataclass
class LlmConfig:
    api_key: str | None = None
    base_url: str = ""
    model: str = ""
    reply_mode: str = "auto"
    max_daily_replies: int = 200
    blocked_keywords: str = ""
    work_hour_start: int | None = None
    work_hour_end: int | None = None
    history_max_messages: int = 10
    max_message_age_seconds: int = 600
    system_prompt: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def blocked_keyword_list(self) -> tuple[str, ...]:
        return tuple(k.strip() for k in (self.blocked_keywords or "").split(",") if k.strip())


async def _load_kv(db: AsyncSession) -> dict[str, str]:
    rows = (await db.execute(select(AppSetting.key, AppSetting.value))).all()
    return {key: val for key, val in rows if val}


async def get_llm_config(db: AsyncSession) -> LlmConfig:
    """合并 DB 覆盖 + env 默认，返回运行时 LLM/回复策略配置（全局，不含账号人设）。"""
    kv = await _load_kv(db)
    values: dict[str, object] = {}
    for key, (field, parse, env_field) in _LLM_DEFS.items():
        raw = kv.get(key)
        if raw is not None:
            values[field] = parse(raw)
        else:
            values[field] = getattr(env_settings, env_field)
    return LlmConfig(**values)


def compose_system_prompt(base: str, persona: str | None) -> str:
    """全局打底 + 账号人设叠加。人设为空 → 与改动前完全一致。"""
    persona = (persona or "").strip()
    base = (base or "").strip()
    if not persona:
        return base
    return f"{base}\n\n【本账号人设（仅影响说话风格与身份，不改变上面的合规要求）】\n{persona}".strip()


async def get_persona_prompt(db: AsyncSession, account_id: int) -> str:
    from app.models import AccountReplyConfig

    return (
        await db.execute(
            select(AccountReplyConfig.persona_prompt).where(
                AccountReplyConfig.account_id == account_id
            )
        )
    ).scalar_one_or_none() or ""


async def get_llm_config_for_account(db: AsyncSession, account_id: int) -> LlmConfig:
    """回复引擎用：全局配置 + 该账号人设合成后的 system_prompt。"""
    # 走包属性取 get_llm_config，测试才能整条替换它（同 provider.runtime_provider 的约定）
    import app.settings as _pkg

    cfg = await _pkg.get_llm_config(db)
    cfg.system_prompt = compose_system_prompt(
        cfg.system_prompt, await get_persona_prompt(db, account_id)
    )
    return cfg


async def save_llm_config(db: AsyncSession, updates: dict) -> dict:
    """写 LLM 相关设置（键白名单；空字符串=清除覆盖回退 .env）。commit 由调用方负责。"""
    for key, raw in updates.items():
        if key not in WRITABLE_KEYS:
            continue
        if raw is None or (isinstance(raw, str) and not raw.strip()) or raw == "":
            row = (
                await db.execute(select(AppSetting).where(AppSetting.key == key))
            ).scalar_one_or_none()
            if row is not None:
                await db.delete(row)
            continue
        row = (
            await db.execute(select(AppSetting).where(AppSetting.key == key))
        ).scalar_one_or_none()
        if row is None:
            row = AppSetting(key=key)
            db.add(row)
        row.value = str(raw)
    await db.flush()
    return await _load_kv(db)


def _mask_key(api_key: str | None) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "***"
    return f"{api_key[:3]}***{api_key[-4:]}"


async def public_config(db: AsyncSession) -> dict:
    """前端可读（脱敏）配置。"""
    cfg = await get_llm_config(db)
    return {
        "api_key_masked": _mask_key(cfg.api_key),
        "api_key_set": cfg.configured,
        "base_url": cfg.base_url,
        "model": cfg.model,
        "reply_mode": cfg.reply_mode,
        "max_daily_replies": cfg.max_daily_replies,
        "blocked_keywords": cfg.blocked_keywords,
        "work_hour_start": cfg.work_hour_start,
        "work_hour_end": cfg.work_hour_end,
        "history_max_messages": cfg.history_max_messages,
        "max_message_age_seconds": cfg.max_message_age_seconds,
        "system_prompt": cfg.system_prompt,
    }


async def test_llm_connection(db: AsyncSession) -> dict:
    """连通测试：用当前生效配置发最小请求；超时/无 key 不抛异常。"""
    from app.ai.provider import OpenAICompatibleProvider

    cfg = await get_llm_config(db)
    if not cfg.configured:
        return {"ok": False, "error": "未配置 API Key（env OPENAI_API_KEY 或页面填写）"}
    provider = OpenAICompatibleProvider(
        api_key=cfg.api_key, base_url=cfg.base_url, model=cfg.model
    )
    start = time.perf_counter()
    result = await provider.chat(
        [{"role": "user", "content": "ping"}], temperature=0, max_tokens=5
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    if not result.ok:
        return {"ok": False, "latency_ms": latency_ms, "error": result.error}
    return {"ok": True, "latency_ms": latency_ms, "echo": result.text[:60]}