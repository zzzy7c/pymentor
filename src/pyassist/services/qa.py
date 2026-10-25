"""v1 文档问答的**唯一业务层**：`/chat` 与 `/knowledge` 共用这一条路径（SPEC D10）。

`answer()` 内部就是"把 `stream_answer()` 消费完，再从检查点取终态"，**不是**另一套
`ainvoke`。两条路径若各走一遍，`reasoning_content`、引用编号、空检索处置就会悄悄分叉，
而测试只会覆盖其中一条。

本模块只做业务，不 import fastapi —— 路由拿 `QAResult` 套信封、把事件翻成 SSE 字节。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from pyassist.agent.state import QaEvent
from pyassist.errors import (
    AppError,
    ConfigError,
    CorpusMissing,
    ErrorCode,
    LLMBadResponse,
    LLMInsufficientBalance,
    LLMModelNotFound,
    LLMRateLimited,
    LLMTimeout,
    StoreError,
)
from pyassist.llm.tracing import UsageSnapshot, add_usage
from pyassist.logging import get_logger
from pyassist.rag.citations import Citation

logger = get_logger(__name__)

_EMPTY_USAGE = UsageSnapshot(
    prompt_tokens=0,
    completion_tokens=0,
    cached_tokens=0,
    cost_usd=Decimal("0"),
    is_off_peak=True,
)

_INTERNAL_MESSAGE = "服务内部错误，请重试"

# **必须是 `list`，不能是 tuple**（L2 的实测补充）：
# langgraph 1.1.10 的 `_output()` 用 `isinstance(stream_mode, list)` 判断"多模式"，
# 传 tuple 会掉进单模式分支、直接 yield 裸 payload —— 于是 `async for mode, payload`
# 在这里抛 `ValueError: not enough values to unpack`。类型标注只为让 mypy 的重载匹配上。
_STREAM_MODES: Final[list[Literal["custom", "updates"]]] = ["custom", "updates"]


@dataclass(frozen=True, slots=True)
class QAResult:
    """非流式路径的返回值。`status="empty"` 由 `/knowledge` 翻译成 200 信封。"""

    answer: str
    citations: list[Citation]
    usage: UsageSnapshot
    status: Literal["ok", "empty"]


async def stream_answer(
    question: str,
    *,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    thread_id: str,
    request_id: str,
    on_usage: Callable[[UsageSnapshot], Awaitable[None]] | None = None,
) -> AsyncIterator[QaEvent]:
    """跑图，产出**已筛好**的事件（SPEC D5）。异常在这里被翻译成 `error` + `done`。

    `on_usage` 在 `done` **之前**调用一次（D8）：`/chat` 靠它把账记进
    `token_ledger`，而不新增 SSE 事件（S8）。

    - `graph.astream(..., stream_mode=["custom","updates"])`：`custom` 是节点自己写的
      业务事件（只有五种）；`updates` **只用来取用量**，永不转发；
    - `AppError` / `KeyError` → `error` 事件（`code=exc.code`，`details=exc.details`）；
    - **任何**其它异常 → `INTERNAL_ERROR` 的 `error` 事件（不泄露原文）；
    - 任何路径都以 `done` 收尾（§2.4 第 2 条）。
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    usage = _EMPTY_USAGE
    try:
        async for mode, payload in graph.astream(
            {"question": question}, config, stream_mode=_STREAM_MODES
        ):
            if mode == "custom":
                # 节点已经筛过（D5）：这里不判断类型，直接转发
                yield cast("QaEvent", payload)
            elif mode == "updates":
                round_usage = _usage_from_updates(payload)
                if round_usage is not None:
                    usage = add_usage(usage, round_usage)
    except (AppError, KeyError) as exc:
        yield _error_event(exc)
    except Exception:
        # 这一支**不是**"兜底吞异常"，是 §2.4 第 2 条的硬要求：缺了它，任何非 AppError
        # 异常都会让生成器直接结束，客户端拿到一条既无 error 也无 done 的半截流，
        # 前端会误判成断线并重连 —— 重连会重跑整张图，等于重复计费。
        logger.exception("qa.stream_crashed", request_id=request_id)
        yield {
            "type": "error",
            "code": ErrorCode.INTERNAL_ERROR.value,
            "message": _INTERNAL_MESSAGE,
            "details": None,
        }
    if on_usage is not None:
        await on_usage(usage)
    # `done` 在 try/except **之后**发，不在 finally 里：在 finally 里 yield 会让
    # 异步生成器在客户端断开（aclose / task.cancel）时抛
    # `RuntimeError: async generator ignored GeneratorExit`（L19 实测）。
    yield {"type": "done", "request_id": request_id, "usage": _usage_dict(usage)}


async def answer(
    question: str,
    *,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    thread_id: str,
    request_id: str,
) -> QAResult:
    """把 `stream_answer()` 消费完，拼回结果（D10）。**不是**另一套 `ainvoke`。

    终态的 `answer` / `citations` 从**检查点**读 —— 它是对话状态的唯一真相
    （AGENTS.md §2），也是"模型输出 → 剥越界标记 → 追加来源块"唯一发生过的版本。
    """
    usage = _EMPTY_USAGE
    async for event in stream_answer(
        question, graph=graph, thread_id=thread_id, request_id=request_id
    ):
        kind = event.get("type")
        if kind == "done":
            usage = _usage_from_dict(event.get("usage"))
        elif kind == "error":
            # 空检索是正常业务结果：由 /knowledge 决定翻译成 200 信封（D6）
            if event.get("code") == ErrorCode.RETRIEVAL_EMPTY.value:
                return QAResult(answer="", citations=[], usage=_EMPTY_USAGE, status="empty")
            raise _exception_for(event)

    values = await _final_state_values(graph, thread_id)
    answer_text = values.get("answer")
    citations = values.get("citations")
    return QAResult(
        answer=answer_text if isinstance(answer_text, str) else "",
        citations=[c for c in citations if isinstance(c, Citation)]
        if isinstance(citations, list)
        else [],
        usage=usage,
        status="ok",
    )


async def _final_state_values(
    graph: CompiledStateGraph[Any, Any, Any, Any], thread_id: str
) -> dict[str, Any]:
    """从检查点取终态。检查点是 state authority（AGENTS.md §2「恢复时不要传状态」）。"""
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return dict(snapshot.values)


def _error_event(exc: AppError | KeyError) -> QaEvent:
    """`AppError` → 原样翻译；`KeyError` → `LLMBadResponse`。两条路都产出 `error` 事件。"""
    if isinstance(exc, KeyError):
        # `str(KeyError("x"))` 会带上引号，故从 args 取
        tool_name = str(exc.args[0]) if exc.args else ""
        return {
            "type": "error",
            "code": ErrorCode.LLM_BAD_RESPONSE.value,
            "message": f"模型调用了未注册的工具：{tool_name}",
            "details": {"tool_name": tool_name},
        }
    return {
        "type": "error",
        "code": exc.code.value,
        "message": exc.message,
        "details": exc.details,
    }


def _exception_for(event: QaEvent) -> AppError:
    """`error` 事件（非空检索）→ 可抛的异常，交给 `api/errors.py` 映射。

    这里是**唯一**从事件反推异常的地方：`stream_answer` 已经把异常对象吃掉了，
    `/knowledge` 又必须让真故障以真实状态码出去（不能一律 200）。
    """
    code = event.get("code", ErrorCode.INTERNAL_ERROR.value)
    message = event.get("message") or _INTERNAL_MESSAGE
    details = event.get("details")

    if code == ErrorCode.CONFIG_ERROR.value:
        return ConfigError(message, details=details)
    if code == ErrorCode.CORPUS_MISSING.value:
        return CorpusMissing(message, details=details)
    if code == ErrorCode.LLM_TIMEOUT.value:
        return LLMTimeout(message, details=details)
    if code == ErrorCode.LLM_RATE_LIMITED.value:
        return LLMRateLimited(message, details=details)
    if code == ErrorCode.LLM_INSUFFICIENT_BALANCE.value:
        return LLMInsufficientBalance(message, details=details)
    if code == ErrorCode.LLM_MODEL_NOT_FOUND.value:
        return LLMModelNotFound(message, details=details)
    if code == ErrorCode.LLM_BAD_RESPONSE.value:
        return LLMBadResponse(message, details=details)
    if code == ErrorCode.STORE_ERROR.value:
        return StoreError(message, details=details)
    # INTERNAL_ERROR 与任何未登记的组合：沿用基类 —— 它映射到 500 / INTERNAL_ERROR，
    # 与事件里的 code 一致。**message 用固定文案**，不透传异常原文（§3.2）。
    return AppError(_INTERNAL_MESSAGE)


def _usage_from_updates(payload: object) -> UsageSnapshot | None:
    """从 `updates` 的 payload 里取一次节点产出的 usage。取不到返回 `None`。

    `updates` 的形状是 `{节点名: 该节点返回的 dict}`；只有 `agent` 会带非零 usage。
    """
    if not isinstance(payload, dict):
        return None
    for update in payload.values():
        if isinstance(update, dict):
            candidate = update.get("usage")
            if isinstance(candidate, UsageSnapshot):
                return candidate
    return None


def _usage_dict(snapshot: UsageSnapshot) -> dict[str, Any]:
    """`done` 事件里的 usage。字段与 §3.1 的 `meta.usage` 一一对应。"""
    return {
        "prompt_tokens": snapshot.prompt_tokens,
        "completion_tokens": snapshot.completion_tokens,
        "cached_tokens": snapshot.cached_tokens,
        "cost_usd": round(float(snapshot.cost_usd), 8),
        "is_off_peak": snapshot.is_off_peak,
    }


def _usage_from_dict(raw: object) -> UsageSnapshot:
    """`done` 事件 → `UsageSnapshot`。缺字段按零值处理，不省略。"""
    if not isinstance(raw, dict):
        return _EMPTY_USAGE
    return UsageSnapshot(
        prompt_tokens=_as_int(raw.get("prompt_tokens")),
        completion_tokens=_as_int(raw.get("completion_tokens")),
        cached_tokens=_as_int(raw.get("cached_tokens")),
        cost_usd=Decimal(str(raw.get("cost_usd", 0))),
        is_off_peak=bool(raw.get("is_off_peak", True)),
    )


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
