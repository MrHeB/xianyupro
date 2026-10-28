"""ai：LLM 接入（自动回复、标题/描述生成，阶段 4-3 移植）。"""

from app.ai.provider import LlmResult, OpenAICompatibleProvider, llm_provider
from app.ai.reply_policy import PolicyContext, PolicyDecision, ReplyMode, evaluate_reply_policy

__all__ = [
    "LlmResult",
    "OpenAICompatibleProvider",
    "llm_provider",
    "PolicyContext",
    "PolicyDecision",
    "ReplyMode",
    "evaluate_reply_policy",
]
