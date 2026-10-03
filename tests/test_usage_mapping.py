"""缺陷 B 的守护（SPEC 阶段二 §4.7）：升级 langchain-deepseek 后**先跑这个**。

DeepSeek 返回扁平的 `prompt_cache_hit_tokens`；langchain_openai 只认
`prompt_tokens_details.cached_tokens`。不归一化，`cached_tokens` 恒为空，
成本报表从上线第一天起就严重偏高（价差可达 50 倍）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import respx
from langchain_core.messages import AIMessageChunk, HumanMessage
from pydantic import SecretStr

from pyassist.config import MODEL_FLASH, settings
from pyassist.llm.client import get_chat_model
from pyassist.llm.tracing import extract_usage, price_usage

TEST_API_KEY = SecretStr("test-only-placeholder-key")
CHAT_URL = f"{settings.deepseek_api_base_url}/chat/completions"

# 峰值时刻（周一 02:00 UTC），让计价走 peak 一列，断言更具体。
PEAK_MOMENT = datetime(2026, 10, 5, 2, tzinfo=UTC)

RESPONSE_WITH_FLAT_CACHE_USAGE = {
    "id": "cmpl-cache",
    "object": "chat.completion",
    "created": 0,
    "model": MODEL_FLASH,
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "你好"}}
    ],
    "usage": {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 64,
        "prompt_cache_miss_tokens": 36,
    },
}

RESPONSE_WITHOUT_CACHE_USAGE = {
    "id": "cmpl-nocache",
    "object": "chat.completion",
    "created": 0,
    "model": MODEL_FLASH,
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "你好"}}
    ],
    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
}


@respx.mock
def test_flat_cache_hit_tokens_become_cache_read() -> None:
    """扁平的 prompt_cache_hit_tokens 必须归一到 input_token_details["cache_read"]。"""
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=RESPONSE_WITH_FLAT_CACHE_USAGE))
    message = get_chat_model(api_key=TEST_API_KEY).invoke([HumanMessage("你好")])

    assert message.usage_metadata is not None
    assert message.usage_metadata["input_token_details"]["cache_read"] == 64

    snapshot = extract_usage(message, model_id=MODEL_FLASH, now_utc=PEAK_MOMENT)
    assert snapshot.cached_tokens == 64
    assert not snapshot.is_off_peak
    assert snapshot.cost_usd == price_usage(
        MODEL_FLASH, cached_tokens=64, uncached_input_tokens=36, output_tokens=20, is_off_peak=False
    )


@respx.mock
def test_absent_flat_cache_fields_do_not_break_usage() -> None:
    """响应里没有扁平缓存字段时，计价照常走全价 miss，不抛异常。"""
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=RESPONSE_WITHOUT_CACHE_USAGE))
    message = get_chat_model(api_key=TEST_API_KEY).invoke([HumanMessage("你好")])

    assert message.usage_metadata is not None
    # 没有扁平字段时上游不写这个键（None 值会被丢掉），所以是"缺失"而非 None。
    assert message.usage_metadata["input_token_details"].get("cache_read") is None

    snapshot = extract_usage(message, model_id=MODEL_FLASH, now_utc=PEAK_MOMENT)
    assert snapshot.cached_tokens == 0
    assert snapshot.cost_usd == price_usage(
        MODEL_FLASH, cached_tokens=0, uncached_input_tokens=100, output_tokens=20, is_off_peak=False
    )


def test_streaming_path_also_normalizes_cache_usage() -> None:
    """流式路径必须同样归一化：只修非流式，阶段六的 SSE 会静默漏掉缓存命中（§4.1）。

    直接调 `_convert_chunk_to_generation_chunk`（不发请求），把 `_create_chat_result`
    之外的另一半 override 钉死 —— 上游只有一个 usage 解析函数，流式与非流式走不同
    入口，两个都必须 override。
    """
    chunk = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL_FLASH,
        "choices": [{"index": 0, "delta": {"content": "你"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 64,
            "prompt_cache_miss_tokens": 36,
        },
    }
    model = get_chat_model(api_key=TEST_API_KEY)
    generation = model._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)

    assert generation is not None
    assert generation.message.usage_metadata is not None
    assert generation.message.usage_metadata["input_token_details"]["cache_read"] == 64
