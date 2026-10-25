"""`llm/client.py::get_structured_output` 的出站契约（思考模式 400 的回归闸门）。

2026-10-25 实测：`with_structured_output()` 默认 `method="function_calling"`，
它把 `tool_choice` 绑成强制工具调用；DeepSeek 思考模式直接拒绝：

    400 Thinking mode does not support this tool_choice

单轮问答测不出来 —— 只有走到结构化修复/出题/批改/推荐才会炸。本文件锁三件事：

1. 请求里是 `response_format={"type": "json_object"}`，**没有** `tool_choice` / `tools`；
2. json_mode 不发 schema，格式说明必须并进末条 human 消息（否则模型不知道字段名）；
3. `include_raw=True` 保留，`parsed` 是 Pydantic 实例，`raw` 带 usage。
"""

from __future__ import annotations

import json

import httpx
import respx
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, SecretStr

from pyassist.config import MODEL_FLASH, settings
from pyassist.llm.client import get_chat_model, get_structured_output

TEST_API_KEY = SecretStr("test-only-placeholder-key")
CHAT_URL = f"{settings.deepseek_api_base_url}/chat/completions"


class _Draft(BaseModel):
    """字段名故意与生产 schema 不同：断言的是"说明来自 schema"，不是抄来的文本。"""

    topic: str
    steps: list[str]


def _response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl",
            "object": "chat.completion",
            "created": 0,
            "model": MODEL_FLASH,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_cache_hit_tokens": 64,
                "prompt_cache_miss_tokens": 36,
            },
        },
    )


@respx.mock
def test_payload_uses_json_mode_and_never_forces_tool_choice() -> None:
    respx.post(CHAT_URL).mock(
        return_value=_response(json.dumps({"topic": "装饰器", "steps": ["先读文档"]}))
    )
    model = get_chat_model(api_key=TEST_API_KEY)

    result = get_structured_output(model, _Draft).invoke(
        [SystemMessage("你是推荐器。"), HumanMessage("下一步学什么？")]
    )

    payload = json.loads(respx.calls[0].request.content)
    assert payload["response_format"] == {"type": "json_object"}
    # ← 思考模式的 400 就出在这两个键上
    assert "tool_choice" not in payload
    assert "tools" not in payload

    assert isinstance(result, dict)
    assert result["parsed"] == _Draft(topic="装饰器", steps=["先读文档"])
    assert result["raw"] is not None
    assert result["raw"].usage_metadata is not None


@respx.mock
def test_format_instructions_are_appended_to_the_last_human_message() -> None:
    respx.post(CHAT_URL).mock(
        return_value=_response(json.dumps({"topic": "生成器", "steps": ["做练习"]}))
    )
    model = get_chat_model(api_key=TEST_API_KEY)

    get_structured_output(model, _Draft).invoke(
        [SystemMessage("你是推荐器。"), HumanMessage("下一步学什么？")]
    )

    messages = json.loads(respx.calls[0].request.content)["messages"]
    # 末条仍是那条 human 任务 —— 不是被格式说明顶掉的一条新消息
    assert messages[0]["role"] == "system"
    assert len(messages) == 2
    assert "下一步学什么？" in messages[-1]["content"]
    # 字段名来自 Pydantic schema，不是 prompt 里手抄的
    assert "topic" in messages[-1]["content"]
    assert "steps" in messages[-1]["content"]
    assert "json" in messages[-1]["content"].lower()
