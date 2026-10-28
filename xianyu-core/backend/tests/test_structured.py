"""chat_json 三路径测试（设计文档 §7.2 / M0 修复项 4 验收）。

覆盖：正常 JSON / 脏输出（围栏+废话→修正轮）/ 端点不支持 response_format 降级 /
重试耗尽抛错不猜 / pydantic 硬边界拒非法值。
"""

import json

import pytest
from pydantic import BaseModel, Field

from app.ai.provider import LlmResult
from app.ai.structured import StructuredOutputError, chat_json, extract_json


class Price(BaseModel):
    pricing: str
    price_cent: int = Field(ge=100, le=1000000)


class FakeProvider:
    """脚本化假 provider：script 逐项弹出 LlmResult，记录每次调用参数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def chat(self, messages, *, temperature=0.7, max_tokens=1024, response_format=None):
        self.calls.append({"messages": messages, "response_format": response_format})
        step = self.script.pop(0)
        return step(messages) if callable(step) else step


async def test_happy_path_json_mode():
    prov = FakeProvider([LlmResult(ok=True, text='{"pricing":"llm","price_cent":1990}')])
    out = await chat_json("定价助手", "商品X", Price, provider=prov)
    assert out.price_cent == 1990
    assert prov.calls[0]["response_format"] == {"type": "json_object"}


async def test_dirty_output_gets_correction_round():
    prov = FakeProvider([
        LlmResult(ok=True, text="好的！\n```json\n{\"pricing\":\"llm\",\"price_cent\":1990}\n```\n希望有帮助"),
    ])
    out = await chat_json("s", "u", Price, provider=prov)
    assert out.price_cent == 1990  # 围栏宽容解析即中，无需修正轮


async def test_correction_round_then_success():
    prov = FakeProvider([
        LlmResult(ok=True, text="我拒绝输出JSON"),
        lambda msgs: LlmResult(ok=True, text=json.dumps({"pricing": "manual", "price_cent": 500})),
    ])
    out = await chat_json("s", "u", Price, provider=prov, retries=1)
    assert out.pricing == "manual"
    assert len(prov.calls) == 2
    # 第二轮带上了上一轮的坏输出与修正指令
    assert "上一次输出不可用" in prov.calls[1]["messages"][-1]["content"]


async def test_unsupported_response_format_falls_back_to_prompt_mode():
    prov = FakeProvider([
        LlmResult(ok=False, error="HTTP 400: unknown field response_format is not supported"),
        LlmResult(ok=True, text='{"pricing":"llm","price_cent":800}'),
    ])
    out = await chat_json("s", "u", Price, provider=prov)
    assert out.price_cent == 800
    assert prov.calls[1]["response_format"] is None
    assert "只输出一个 JSON 对象" in prov.calls[1]["messages"][0]["content"]


async def test_exhausted_retries_raises_not_guess():
    prov = FakeProvider([
        LlmResult(ok=True, text="garbage"),
        LlmResult(ok=True, text="still garbage"),
    ])
    with pytest.raises(StructuredOutputError):
        await chat_json("s", "u", Price, provider=prov, retries=1)
    assert len(prov.calls) == 2


async def test_pydantic_hard_boundary_rejects_out_of_range():
    prov = FakeProvider([
        LlmResult(ok=True, text='{"pricing":"llm","price_cent":5}'),  # < 100 分
        lambda msgs: LlmResult(ok=True, text='{"pricing":"llm","price_cent":150}'),
    ])
    out = await chat_json("s", "u", Price, provider=prov, retries=1)
    assert out.price_cent == 150  # 越界值在修正轮前被 schema 拦下


def test_extract_json_plain_and_fenced_and_embedded():
    assert extract_json('{"a":1}') == {"a": 1}
    assert extract_json('```json\n{"a":2}\n```') == {"a": 2}
    assert extract_json('废话 {"a":3} 废话') == {"a": 3}
    with pytest.raises(ValueError):
        extract_json("no json here")
