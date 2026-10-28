"""结构化输出层（设计文档 §7.2 / M0 修复项 4）：LLM 回 JSON → pydantic 校验。

chat_json 契约：
- 首选 response_format={"type":"json_object"}；端点不支持（部分 Ollama/自建）时
  降级为"提示词要求 JSON + 剥 ```json 围栏"宽容解析；
- 解析/校验失败 → 追加一轮"只回 JSON"修正重试（retries 次）；
- 重试耗尽**抛错，不猜**（区别于 provider.chat 的 ok=False 约定——定价/文案是
  决策数据，宁可不产出也不能产出半真半假的对象）。
"""

import json
import logging
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.ai.provider import llm_provider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)

# 端点拒绝 response_format 的特征串（命中即降级为提示词模式，不算失败）
_UNSUPPORTED_MARKERS = ("response_format", "json_object", "json mode")


class StructuredOutputError(RuntimeError):
    """结构化输出不可用（调用失败 / 脏输出重试耗尽）。上层据此走降级链，禁止兜底猜值。"""


def json_mode_unsupported(error: str) -> bool:
    low = (error or "").lower()
    hit_marker = any(m in low for m in _UNSUPPORTED_MARKERS)
    hit_says_no = any(w in low for w in ("not supported", "unsupported", "not support", "unknown field", "unexpected"))
    return hit_marker and hit_says_no


def extract_json(text: str):
    """宽容提取 JSON 对象：裸文本 → 剥围栏 → 截取首尾花括号。失败抛 ValueError。"""
    text = (text or "").strip()
    candidates = [text]
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except (ValueError, TypeError):
            continue
    raise ValueError(f"输出不是合法 JSON：{text[:200]!r}")


async def chat_json(
    system: str,
    user: str,
    schema: type[T],
    *,
    provider=None,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    retries: int = 1,
) -> T:
    """单轮结构化对话：返回校验通过的 schema 实例，失败抛 StructuredOutputError。"""
    prov = provider or llm_provider
    schema_hint = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    json_mode = True
    last_err = ""

    for attempt in range(retries + 1):
        result = None
        if json_mode:
            result = await prov.chat(
                list(messages),
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            if not result.ok and json_mode_unsupported(result.error):
                logger.info("端点不支持 response_format，降级提示词模式: %s", result.error[:200])
                json_mode = False
                result = None
        if result is None:
            # 降级模式：提示词内要求纯 JSON（无围栏）
            msgs = [
                {"role": "system", "content": f"{system}\n只输出一个 JSON 对象，不要任何解释文字、不要 markdown 围栏。"},
                *messages[1:],
            ]
            result = await prov.chat(msgs, temperature=temperature, max_tokens=max_tokens)

        if not result.ok:
            last_err = f"LLM 调用失败：{result.error}"
        else:
            try:
                return schema.model_validate(extract_json(result.text))
            except (ValueError, ValidationError) as exc:
                last_err = f"输出校验失败：{exc}"
                messages.append({"role": "assistant", "content": result.text})

        if attempt < retries:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上一次输出不可用（{last_err[:300]}）。"
                        "只回复一个符合以下 JSON Schema 的 JSON 对象，不要任何其它文字：\n"
                        f"{schema_hint}"
                    ),
                }
            )

    raise StructuredOutputError(f"chat_json 重试 {retries + 1} 次仍失败：{last_err}")
