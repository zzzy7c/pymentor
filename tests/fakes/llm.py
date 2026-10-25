"""按脚本逐轮返回 `AIMessage` 的聊天模型替身（SPEC §5.13 / L14）。

**为什么不用 `GenericFakeChatModel`**：它的 `bind_tools()` 直接
`NotImplementedError`（L14 实测），而图里 `agent` 节点必须 `bind_tools`。
自写四十行，比给现成 fake 打补丁便宜。

它是**离线**的：`astream` 不碰网络，一条断言都不用 respx。
真正的 HTTP 形状（含 `reasoning_content` 回传）由 `test_sse.py` 里
respx 打桩的 `ChatDeepSeekPatched` 单独覆盖 —— 两类测试各管一段。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from pyassist.config import MODEL_FLASH


class ScriptedChatModel(BaseChatModel):
    """`script` 里的消息按顺序被"消费"：每次 `_astream` / `_generate` 弹出一条。

    `model_name` 必须设：`model_id_of()` 靠它查价，缺了就是 `ConfigError`
    —— 宁可在这里显式给，也不要在图里对未知模型静默按 0 计价。
    """

    model_name: str = MODEL_FLASH
    script: list[AIMessage] = Field(default_factory=list)
    # 每次 `bind_tools` 收到的 specs（判据 9：模型看到的工具名 == 函数名）
    bound_tool_specs: list[list[dict[str, Any]]] = Field(default_factory=list)
    # 每次调用收到的消息序列（用于断言 system 在最前、没有第二个 system）
    requests: list[list[BaseMessage]] = Field(default_factory=list)
    # `with_structured_output` 的脚本：每轮弹出一个 Pydantic 实例（或要抛的异常）
    structured_script: list[BaseModel | Exception] = Field(default_factory=list)
    structured_requests: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        """记录 tools 并返回 `self`。**真的绑定一次都不做** —— 行为全由 `script` 决定。"""
        self.bound_tool_specs.append(
            [dict(spec) if isinstance(spec, dict) else spec for spec in tools]
        )
        return self

    def with_structured_output(
        self,
        schema: Any,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        """`repair` 节点用。`include_raw=True` 时回一个含 `raw` 的 dict ——
        与 LangChain 默认实现同形，图才能从 `raw` 里读到 usage（D6 的硬要求）。"""
        return RunnableLambda(
            lambda messages: self._structured_step(messages, include_raw=include_raw)
        )

    def _structured_step(self, messages: Any, *, include_raw: bool) -> Any:
        self.structured_requests.append(list(messages))
        if not self.structured_script:
            raise AssertionError(
                "ScriptedChatModel 的 structured_script 用完了：用例没有为修复轮准备结果"
            )
        item = self.structured_script.pop(0)
        if isinstance(item, Exception):
            raise item
        if not include_raw:
            return item
        return {
            "raw": AIMessage(
                content="",
                usage_metadata={
                    "input_tokens": 40,
                    "output_tokens": 20,
                    "total_tokens": 60,
                },
            ),
            "parsed": item,
            "parsing_error": None,
        }

    def _next_message(self, messages: list[BaseMessage]) -> AIMessage:
        self.requests.append(list(messages))
        if not self.script:
            raise AssertionError(
                "ScriptedChatModel 的 script 用完了：用例没有为这一轮准备 AIMessage"
            )
        return self.script.pop(0)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self._next_message(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """逐**字符**吐 `content`，于是"逐字上屏"在测试里是真的逐字。

        tool_calls 与 `usage_metadata` 都放在最后一块 —— 与真实流式响应的形状一致
        （delta 先来、`finish_reason` 与用量最后到）。`AIMessageChunk.__add__`
        会把它们合并进最终聚合块，图的 `_to_ai_message()` 再从聚合块取。
        """
        message = self._next_message(messages)
        content = message.content if isinstance(message.content, str) else ""
        for char in content:
            yield ChatGenerationChunk(message=AIMessageChunk(content=char))

        # additional_kwargs 带 reasoning_content 时也一并回放：图必须原样带进
        # 下一轮请求（DeepSeek 思考模式的硬要求，AGENTS.md §1）。
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_calls=list(message.tool_calls),
                usage_metadata=message.usage_metadata,
                additional_kwargs=dict(message.additional_kwargs),
                response_metadata=dict(message.response_metadata),
                id=message.id,
            )
        )
