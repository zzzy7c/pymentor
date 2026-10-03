"""阶段二的强制回归测试（SPEC 阶段二 §1.4 / §5.1）。

三条全部离线：前两条直接构造消息调 `_get_request_payload`（不发请求），
第三条用 `respx` mock 两轮。模型实例一律经 `llm/client.py` 取 ——
测试里自己构造模型会变成事实上的第二个所有者。
"""

from __future__ import annotations

import json

import httpx
import respx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

from pyassist.config import MODEL_FLASH, settings
from pyassist.llm.client import get_chat_model

TEST_API_KEY = SecretStr("test-only-placeholder-key")
CHAT_URL = f"{settings.deepseek_api_base_url}/chat/completions"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "检索 Python 文档",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
]


def _assistant_with_tool_call(*, reasoning: str | None) -> AIMessage:
    additional = {} if reasoning is None else {"reasoning_content": reasoning}
    return AIMessage(
        content="",
        tool_calls=[{"name": "search_docs", "args": {"query": "asyncio"}, "id": "call_1"}],
        additional_kwargs=additional,
    )


def test_payload_carries_reasoning_content_of_tool_call_message() -> None:
    """payload 级：带 tool_calls 的 assistant 消息，reasoning_content 必须出现在出站 JSON 里。"""
    model = get_chat_model(api_key=TEST_API_KEY)
    payload = model._get_request_payload(
        [HumanMessage("asyncio.gather 怎么用"), _assistant_with_tool_call(reasoning="先查文档")]
    )
    assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "先查文档"


def test_payload_backfills_empty_string_when_reasoning_content_missing() -> None:
    """回填级：带 tool_calls 却没有 reasoning_content 时，键必须存在且为空串 —— 不是被省略。"""
    model = get_chat_model(api_key=TEST_API_KEY)
    payload = model._get_request_payload(
        [HumanMessage("asyncio.gather 怎么用"), _assistant_with_tool_call(reasoning=None)]
    )
    assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert "reasoning_content" in assistant
    assert assistant["reasoning_content"] == ""


@respx.mock
def test_second_round_request_keeps_reasoning_content() -> None:
    """连续两轮回归（强制）：第一轮 tool_calls → ToolMessage → 第二轮请求必须带上该字段。

    这就是 §5.1 给所有涉及工具调用的功能设的那道门：只测"第一步能调工具"不算通过。
    """
    first_round = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL_FLASH,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "第一步：检索 asyncio 文档",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "search_docs",
                                "arguments": '{"query": "asyncio"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 64,
            "prompt_cache_miss_tokens": 36,
        },
    }
    second_round = {
        "id": "cmpl-2",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL_FLASH,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "用 asyncio.gather 并发执行协程。"},
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 15, "total_tokens": 215},
    }
    respx.post(CHAT_URL).mock(
        side_effect=[httpx.Response(200, json=first_round), httpx.Response(200, json=second_round)]
    )

    model = get_chat_model(api_key=TEST_API_KEY).bind_tools(TOOLS)
    messages = [HumanMessage("asyncio.gather 怎么用")]
    first_reply = model.invoke(messages)
    model.invoke(
        [
            *messages,
            first_reply,
            ToolMessage(content="文档片段：gather 并发执行。", tool_call_id="call_1"),
        ]
    )

    assert len(respx.calls) == 2
    second_request = json.loads(respx.calls[1].request.content)
    assistant = next(m for m in second_request["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "第一步：检索 asyncio 文档"
