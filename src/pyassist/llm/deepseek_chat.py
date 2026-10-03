"""★ ChatDeepSeekPatched：修 `langchain-deepseek` 1.1.0 的两处缺陷（SPEC 阶段二 §1 / §2）。

缺陷 A：`reasoning_content` 只入不出。
    入站方向 ChatDeepSeek 会把响应里的 `reasoning_content` 存进
    `AIMessage.additional_kwargs`；出站方向（`_get_request_payload` →
    langchain_openai 的模块级 `_convert_message_to_dict`）完全不读这个字段，
    于是被静默丢弃。思考模式下，一旦出现「模型 → tool_calls → ToolMessage →
    再调模型」，第二轮请求缺 `reasoning_content`，DeepSeek 直接返回 400。

缺陷 B：缓存命中的输入 token 会算成全价。
    langchain_openai 的 `_create_usage_metadata` 只认 OpenAI 的嵌套形状
    `prompt_tokens_details.cached_tokens`；DeepSeek 返回的是扁平的
    `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。不归一化，
    §3.1 的 `usage.cached_tokens` 恒为空，成本报表严重偏高（价差可达 50 倍）。

上游状态（2026-10-03 实测 1.1.0）：社区 PR #37461 / #35093 **均未合入**，
两个补丁都必须自己写。升级依赖后先跑
`tests/test_deepseek_reasoning.py` 与 `tests/test_usage_mapping.py`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    InputTokenDetails,
    UsageMetadata,
)
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_deepseek import ChatDeepSeek

REASONING_CONTENT_KEY = "reasoning_content"
CACHE_HIT_KEY = "prompt_cache_hit_tokens"
EMPTY_REASONING = ""  # 带 tool_calls 却缺该字段时回填这个值，不能省略键


def mirror_reasoning_content(messages: Sequence[BaseMessage], payload: dict[str, Any]) -> None:
    """把入站存下的 `reasoning_content` 写回出站 assistant 消息（SPEC §1）。

    带 `tool_calls` 而字段缺失时回填空串 —— **键必须存在，不能被省略**。
    `payload["messages"]` 与传入的 `messages` 一一对应；长度不符时宁可不动手。
    """
    outbound = payload.get("messages")
    if not isinstance(outbound, list) or len(outbound) != len(messages):
        return
    for source, target in zip(messages, outbound, strict=True):
        if not isinstance(source, AIMessage) or target.get("role") != "assistant":
            continue
        reasoning = source.additional_kwargs.get(REASONING_CONTENT_KEY)
        if reasoning is None:
            if "tool_calls" in target:
                target[REASONING_CONTENT_KEY] = EMPTY_REASONING
        else:
            target[REASONING_CONTENT_KEY] = reasoning


def normalize_cache_usage(
    message: AIMessage | AIMessageChunk,
    raw_usage: Mapping[str, Any] | None,
) -> None:
    """把 DeepSeek 的扁平 `prompt_cache_hit_tokens` 归一到 `input_token_details["cache_read"]`。

    只认上游原始 usage —— `usage_metadata` 里这个信息已经被 langchain_openai 丢掉了。
    """
    if not raw_usage:
        return
    cache_hit = raw_usage.get(CACHE_HIT_KEY)
    if cache_hit is None:
        return
    usage = message.usage_metadata
    if usage is None:
        return
    # dict(TypedDict) 在 mypy 里退化成 dict[str, object]；显式 cast 收窄回 InputTokenDetails。
    details = cast("InputTokenDetails", dict(usage.get("input_token_details") or {}))
    details["cache_read"] = int(cache_hit)
    message.usage_metadata = UsageMetadata(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        total_tokens=usage["total_tokens"],
        input_token_details=details,
        output_token_details=usage.get("output_token_details") or {},
    )


class ChatDeepSeekPatched(ChatDeepSeek):
    """全项目唯一允许使用的模型类（§2）。**构造点只有 `llm/client.py`。**"""

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        mirror_reasoning_content(self._convert_input(input_).to_messages(), payload)
        return payload

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict[str, Any] | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        raw_usage = (result.llm_output or {}).get("token_usage")
        for generation in result.generations:
            # 非流式路径产出 AIMessage；isinstance 收窄既满足 mypy --strict，
            # 也避免把非 assistant 产物送进 usage 归一化。
            if isinstance(generation.message, AIMessage):
                normalize_cache_usage(generation.message, raw_usage)
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk | None:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is not None and isinstance(generation.message, AIMessageChunk):
            normalize_cache_usage(generation.message, chunk.get("usage"))
        return generation
