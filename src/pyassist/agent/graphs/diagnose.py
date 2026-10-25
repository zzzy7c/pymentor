"""v2 错误诊断图（SPEC D5）：检索 → 探索（工具循环）→ 结构化修复 → 沙箱验证 → 回环/收尾。

两条与其他图不同的纪律：

1. **验证是图节点，不是工具。** 模型不能自己宣布"修好了"：`verify` 用 `fixed_code`
   真的跑一次容器，结果写回状态，由条件边决定是否继续修。
2. **修复不走散文。** `repair` 用 `llm/client.py::get_structured_output` 拿
   `RepairDraft` 实例（JSON 模式，见该函数注释），不从 ```python 围栏里抠代码
   （AGENTS.md §3.4）。

`reasoning_content` 的教训同样适用：`agent` 直接返回 `ainvoke` 得到的 `AIMessage`，
**不重新构造**、不丢 `additional_kwargs` —— 思考模式下第二轮工具调用靠它回传（阶段二）。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Any, Literal, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from pyassist.agent.graphs.qa import (
    _append_note,
    _resolve_deps,
    _strip_markers,
    _trim_history,
)
from pyassist.agent.registry import get_tool, tool_specs
from pyassist.agent.state import DiagnoseState
from pyassist.agent.tools.docs import ErrorSignature, Fetcher, Searcher, parse_traceback
from pyassist.agent.tools.sandbox_tools import execute_cached
from pyassist.config import (
    DEFAULT_TOP_K,
    DIAGNOSE_CONTEXT_PROMPT_NAME,
    DIAGNOSE_MODEL_TIER,
    DIAGNOSE_PROMPT_NAME,
    DIAGNOSE_REPAIR_PROMPT_NAME,
    DIAGNOSE_TOOL_NAMES,
    MAX_HISTORY_MESSAGES,
    MAX_REPAIR_ATTEMPTS,
    MAX_TOOL_ROUNDS,
    OUTPUT_LANGUAGE,
)
from pyassist.errors import ConfigError, LLMBadResponse, RetrievalEmpty
from pyassist.llm.client import get_chat_model, get_structured_output
from pyassist.llm.prompts import PROMPT_DIR, load_prompt
from pyassist.llm.tracing import (
    UsageSnapshot,
    add_usage,
    extract_usage,
    log_llm_response,
    model_id_of,
)
from pyassist.logging import get_logger
from pyassist.rag.citations import (
    build_citations,
    find_ungrounded_markers,
    render_reference_block,
)
from pyassist.sandbox.runner import SandboxResult, SandboxRunner

logger = get_logger(__name__)

_ZERO_USAGE = UsageSnapshot(
    prompt_tokens=0,
    completion_tokens=0,
    cached_tokens=0,
    cost_usd=Decimal(0),
    is_off_peak=True,
)


class RepairDraft(BaseModel):
    """`repair` 节点的结构化输出（D6）。字段即契约，改它要同步改 prompt 与测试。"""

    error_type: str
    explanation: str
    fixed_code: str
    change_summary: list[str]


@dataclass(frozen=True, slots=True)
class DiagnoseGraph:
    """编译好的图 + 与它同源的检索预检（与 `QAGraph` 同构，SPEC §5.5）。"""

    graph: CompiledStateGraph[Any, Any, Any, Any]
    preflight: Callable[[], Awaitable[None]]
    # 账本要 `model_id`，而 `UsageSnapshot` 不含它：构建时记下，记账处不猜（D8）。
    model_id: str


def build_diagnose_graph(
    *,
    checkpointer: BaseCheckpointSaver[str] | None,
    model: BaseChatModel | None = None,
    runner: SandboxRunner | None = None,
    search: Searcher | None = None,
    fetch: Fetcher | None = None,
    preflight: Callable[[], Awaitable[None]] | None = None,
) -> DiagnoseGraph:
    """拼图并编译。**`runner` 必填**：没有沙箱就没有 v2 的验证环节。"""
    if runner is None:
        raise ConfigError("诊断图缺少沙箱 runner")
    searcher, fetcher, preflight_fn = _resolve_deps(search, fetch, preflight)
    chat_model = model if model is not None else get_chat_model(tier=DIAGNOSE_MODEL_TIER)
    model_id = model_id_of(chat_model)
    # JSON 模式 + include_raw：前者避开思考模式对 tool_choice 的 400，
    # 后者保住 usage_metadata（D6/S10）。两个约束都封装在唯一构造点里。
    structured: Runnable[Any, Any] = get_structured_output(chat_model, RepairDraft)

    async def retrieve(state: DiagnoseState) -> dict[str, Any]:
        error_text = state["error_text"]
        signature = parse_traceback(error_text)
        query = _diagnose_query(signature)
        started = time.perf_counter()
        chunks = await searcher(query, DEFAULT_TOP_K)
        if not chunks:
            # 和 v1 同一条产品原则：文档里没有就直说，不凭记忆编造（§3.2 → 200）。
            raise RetrievalEmpty(query=query)
        citations = build_citations(chunks)
        logger.info(
            "diagnose.retrieve",
            exception_type=signature.exception_type,
            doc_ids=[chunk.chunk_id for chunk in chunks],
            citations=len(citations),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return {
            "chunks": list(chunks),
            "citations": citations,
            "error_signature": signature,
            "tool_rounds": 0,
            "repair_attempts": 0,
            "sandbox_results": {},
            "last_verification": None,
            "usage": _ZERO_USAGE,
            "answer": "",
            "error_type": None,
            "explanation": "",
            "fixed_code": None,
            "change_summary": [],
            # 只有 messages 是追加语义（add_messages），其余一律覆盖（D9）
            "messages": [HumanMessage(error_text)],
        }

    async def agent(state: DiagnoseState) -> dict[str, Any]:
        messages = _render_agent_messages(state)
        bound = chat_model.bind_tools(tool_specs(DIAGNOSE_TOOL_NAMES))
        started = time.perf_counter()
        reply = await bound.ainvoke(messages)
        if not isinstance(reply, AIMessage):
            raise LLMBadResponse(
                f"模型返回了非 AIMessage 对象：{type(reply).__name__}",
                details={"stage": "agent"},
            )
        # 原样进状态：`additional_kwargs` 里的 reasoning_content 由补丁在下一轮写回（R1）。
        usage = extract_usage(reply, model_id=model_id)
        log_llm_response(usage, model_id=model_id, latency_ms=_elapsed_ms(started))
        return {"messages": [reply], "usage": add_usage(state["usage"], usage)}

    async def tools(state: DiagnoseState) -> dict[str, Any]:
        last = state["messages"][-1]
        tool_calls: list[dict[str, Any]] = [
            dict(call) for call in (getattr(last, "tool_calls", None) or [])
        ]
        chunks = list(state["chunks"])
        # 并行 tool_calls 必须**递增**游标，否则两个检索工具会共用同一个起始编号。
        next_index = len(chunks) + 1
        sandbox_results = dict(state["sandbox_results"])
        produced: list[ToolMessage] = []

        for tool_call in tool_calls:
            name = str(tool_call["name"])
            message = await get_tool(name).ainvoke(
                _inject(
                    tool_call,
                    next_index=next_index,
                    searcher=searcher,
                    fetcher=fetcher,
                    runner=runner,
                    cache=sandbox_results,
                )
            )
            if not isinstance(message, ToolMessage):
                raise LLMBadResponse(
                    f"工具 {name!r} 返回了非 ToolMessage 对象：{type(message).__name__}",
                    details={"tool_name": name},
                )
            produced.append(message)
            artifact = message.artifact
            if isinstance(artifact, list):
                # search_docs / explain_error：追加片段并推进全局编号
                chunks.extend(artifact)
                next_index += len(artifact)
            elif isinstance(artifact, SandboxResult):
                # run_code：执行结果进缓存，**不碰**引用编号
                sandbox_results[artifact.idempotency_key] = artifact

        citations = build_citations(chunks)
        return {
            "messages": produced,
            "chunks": chunks,
            "citations": citations,
            "sandbox_results": sandbox_results,
            "tool_rounds": int(state["tool_rounds"]) + 1,
        }

    async def repair(state: DiagnoseState) -> dict[str, Any]:
        started = time.perf_counter()
        result = await structured.ainvoke(_render_repair_messages(state))
        if not isinstance(result, dict):
            raise LLMBadResponse("结构化修复返回了非 dict 对象", details={"stage": "repair"})
        raw = result.get("raw")
        parsed = result.get("parsed")
        usage = state["usage"]
        if isinstance(raw, AIMessage):
            round_usage = extract_usage(raw, model_id=model_id)
            log_llm_response(round_usage, model_id=model_id, latency_ms=_elapsed_ms(started))
            usage = add_usage(usage, round_usage)
        if not isinstance(parsed, RepairDraft):
            raise LLMBadResponse(
                "修复结果不是合法的结构化输出",
                details={
                    "stage": "repair",
                    "parsing_error": str(result.get("parsing_error")),
                },
            )
        fixed_code = parsed.fixed_code.strip()
        if not fixed_code:
            # 不允许猜一段代码出来（D6）。
            raise LLMBadResponse("模型没有给出可运行的修复代码", details={"stage": "repair"})
        change_summary = [item.strip() for item in parsed.change_summary if item.strip()]
        return {
            "error_type": parsed.error_type.strip() or None,
            "explanation": parsed.explanation,
            "fixed_code": fixed_code,
            "change_summary": change_summary,
            "repair_attempts": int(state["repair_attempts"]) + 1,
            "usage": usage,
        }

    async def verify(state: DiagnoseState) -> dict[str, Any]:
        fixed_code = state.get("fixed_code")
        if not fixed_code:
            raise LLMBadResponse("没有可验证的修复代码", details={"stage": "verify"})
        started = time.perf_counter()
        result = await execute_cached(
            fixed_code,
            runner=runner,
            cache=state["sandbox_results"],
        )
        logger.info(
            "diagnose.verify",
            idempotency_key=result.idempotency_key,
            status=result.status,
            exit_code=result.exit_code,
            repair_attempts=int(state["repair_attempts"]),
            latency_ms=_elapsed_ms(started),
        )
        return {
            "last_verification": result,
            "sandbox_results": {result.idempotency_key: result},
        }

    async def finalize(state: DiagnoseState) -> dict[str, Any]:
        citations = state["citations"]
        answer = state.get("explanation") or ""
        ungrounded = find_ungrounded_markers(answer, citations)
        if ungrounded:
            logger.warning(
                "diagnose.ungrounded_markers",
                markers=sorted(ungrounded),
                citation_count=len(citations),
            )
            answer = _strip_markers(answer, ungrounded)
        block = render_reference_block(citations)
        if block:
            answer = _append_note(answer, block)
        return {"answer": answer}

    builder = StateGraph(DiagnoseState)
    builder.add_node("retrieve", retrieve)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_node("repair", repair)
    builder.add_node("verify", verify)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "agent")
    builder.add_conditional_edges(
        "agent",
        _should_call_tools,
        {"tools": "tools", "repair": "repair"},
    )
    builder.add_edge("tools", "agent")
    builder.add_edge("repair", "verify")
    builder.add_conditional_edges(
        "verify",
        _should_retry,
        {"repair": "repair", "finalize": "finalize"},
    )
    builder.add_edge("finalize", END)

    compiled = builder.compile(checkpointer=checkpointer)
    return DiagnoseGraph(graph=compiled, preflight=preflight_fn, model_id=model_id_of(chat_model))


def _diagnose_query(signature: ErrorSignature) -> str:
    """D10：检索 query **确定性**地由解析结果派生，不让模型自由发挥。

    同一段报错永远得到同一个 query —— 这是评估可复现的前提，也让前缀缓存更稳。
    """
    if signature.message:
        return f"{signature.exception_type}: {signature.message}"[:200]
    return signature.exception_type[:200]


def _should_call_tools(state: DiagnoseState) -> Literal["tools", "repair"]:
    last = state["messages"][-1]
    has_calls = isinstance(last, AIMessage) and bool(last.tool_calls)
    if has_calls and int(state["tool_rounds"]) < MAX_TOOL_ROUNDS:
        return "tools"
    return "repair"


def _should_retry(state: DiagnoseState) -> Literal["repair", "finalize"]:
    last = state.get("last_verification")
    if last is not None and last.status == "ok":
        return "finalize"
    if int(state["repair_attempts"]) < MAX_REPAIR_ATTEMPTS:
        return "repair"
    return "finalize"


def _system_message() -> SystemMessage:
    """`diagnose.jinja2` 渲染成的系统提示。**逐字节稳定**（前缀缓存，§1）。"""
    rendered = load_prompt(DIAGNOSE_PROMPT_NAME).format_messages(
        history=[], output_language=OUTPUT_LANGUAGE
    )
    return cast("SystemMessage", rendered[0])


@cache
def _context_template() -> ChatPromptTemplate:
    """`diagnose_context.jinja2` 的独立加载器。**不走 `load_prompt()`**（L17）。"""
    path = PROMPT_DIR / f"{DIAGNOSE_CONTEXT_PROMPT_NAME}.jinja2"
    return ChatPromptTemplate.from_messages(
        [("human", path.read_text(encoding="utf-8"))], template_format="jinja2"
    )


@cache
def _repair_template() -> ChatPromptTemplate:
    """`diagnose_repair.jinja2` 的独立加载器。理由同上：它必须是 human，不是第二条 system。"""
    path = PROMPT_DIR / f"{DIAGNOSE_REPAIR_PROMPT_NAME}.jinja2"
    return ChatPromptTemplate.from_messages(
        [("human", path.read_text(encoding="utf-8"))], template_format="jinja2"
    )


def _render_agent_messages(state: DiagnoseState) -> list[BaseMessage]:
    """`[system] + 裁剪后的历史（末条换成本轮 context）` —— 与 qa 同构（§5.9）。

    工具轮的最后一条是 `ToolMessage`，原样发送：再塞一遍片段块会让模型重复看同批证据。
    """
    trimmed = _trim_history(state["messages"], limit=MAX_HISTORY_MESSAGES)
    if not trimmed or not isinstance(trimmed[-1], HumanMessage):
        return [_system_message(), *trimmed]
    context = _context_template().format_messages(
        error_text=state["error_text"],
        broken_code=state.get("broken_code"),
        citations=state["citations"],
    )
    return [_system_message(), *trimmed[:-1], *context]


def _render_repair_messages(state: DiagnoseState) -> list[BaseMessage]:
    """`[system] + 裁剪后的历史 + [HumanMessage(repair 模板)]`（§5.9）。

    修复模板自己带文档片段与上一次失败结果，所以不必把首轮渲染块再塞一遍。
    """
    signature = state.get("error_signature") or ErrorSignature(
        exception_type="Unknown",
        message="",
        frames=(),
        last_frame=None,
        raw_tail="",
    )
    repair = _repair_template().format_messages(
        error_signature=signature,
        last_failure=state.get("last_verification"),
        broken_code=state.get("broken_code"),
        citations=state["citations"],
    )
    history = _trim_history(state["messages"], limit=MAX_HISTORY_MESSAGES)
    return [_system_message(), *history, *repair]


def _inject(
    tool_call: dict[str, Any],
    *,
    next_index: int,
    searcher: Searcher,
    fetcher: Fetcher,
    runner: SandboxRunner,
    cache: dict[str, SandboxResult],
) -> dict[str, Any]:
    """按工具名注入**隐藏参数**，保持 `ToolCall` 的四键形状 `{name, id, type, args}`（L18/S8）。

    只传 `{**args, ...}` 会让 `ainvoke` 返回裸 `str`，`artifact` 与 `tool_call_id` 一起消失。
    未知键一个都不多传：注入键会被静默丢弃，而静默丢键是最难查的那类 bug（S7）。
    """
    name = str(tool_call["name"])
    args = dict(tool_call["args"])
    if name in {"search_docs", "explain_error"}:
        args.update(start_index=next_index, searcher=searcher)
    elif name == "read_doc":
        args["fetcher"] = fetcher
    elif name == "run_code":
        args.update(runner=runner, cache=cache)
    else:
        raise KeyError(f"未注册的工具：{name!r}")
    return {**tool_call, "args": args}


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "DiagnoseGraph",
    "RepairDraft",
    "_render_agent_messages",
    "_render_repair_messages",
    "_repair_template",
    "_should_call_tools",
    "_should_retry",
    "build_diagnose_graph",
]
