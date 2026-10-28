"""LLM 接入层：OpenAI 兼容薄客户端（httpx 手写，不用 SDK，参考 xianyu-pilot 盘点 §4.5）。

约定：generate 不抛异常，失败返回 ok=False（上层决定降级策略）。
任意兼容端点可配（OpenAI / DeepSeek / 通义 compatible-mode）。
"""

import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class LlmResult:
    ok: bool
    text: str = ""
    error: str = ""


class OpenAICompatibleProvider:
    """薄客户端：/v1/chat/completions。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self.api_key = api_key or settings.openai_api_key
        self.base_url = base_url or settings.openai_base_url
        self.model = model or settings.openai_model

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        response_format: dict | None = None,
    ) -> LlmResult:
        if not self.api_key:
            return LlmResult(ok=False, error="未配置 OPENAI_API_KEY")
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        try:
            async with httpx.AsyncClient(timeout=settings.ai_request_timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
            return LlmResult(ok=True, text=data["choices"][0]["message"]["content"])
        except httpx.HTTPStatusError as exc:
            # 带响应体：structured.chat_json 靠它识别"端点不支持 response_format"并降级
            body = ""
            try:
                body = exc.response.text[:300]
            except Exception:
                pass
            logger.warning("LLM 调用失败: %s %s", exc, body)
            return LlmResult(ok=False, error=f"HTTP {exc.response.status_code}: {body}")
        except Exception as exc:  # 不抛异常只返 ok=False（盘点 §4.5 约定）
            logger.warning("LLM 调用失败: %s", exc)
            return LlmResult(ok=False, error=str(exc))


# 全局单例
llm_provider = OpenAICompatibleProvider()


async def runtime_provider(db) -> OpenAICompatibleProvider:
    """按运行时配置（DB 覆盖 .env）构造 provider。

    绝不能用上面的 `llm_provider` 单例：那是 import 时按 env 建的，前端设置页里配的 key
    对它无效，任何部署下都恒定退化成规则兜底（采集链踩过，见 D-2026-09-19-12 的回归测试）。
    `get_llm_config` 在函数内 import，测试才能整条替换它。
    """
    from app.settings import get_llm_config

    cfg = await get_llm_config(db)
    return OpenAICompatibleProvider(api_key=cfg.api_key, base_url=cfg.base_url, model=cfg.model)
