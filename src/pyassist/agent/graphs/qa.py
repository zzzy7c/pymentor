"""★ v1 文档问答图：`retrieve → agent ⇄ tools → finalize`（SPEC D1 / §5.5）。

**节点全部手拼，不用 `langgraph.prebuilt`**（AGENTS.md §1）。理由不是洁癖：
v2/v3 的核心是"工具循环 + 沙箱验证"，那个循环必须每一步都看得见
（工具被调了几次、每次往 `chunks` 里追加了什么）；预置 Agent 会把这些细节藏起来，
而阶段七重写这个循环时，`reasoning_content` 会被再坑一次。

三条不变量：

1. **状态里只有本轮的东西**：`retrieve` 每轮**覆盖** `chunks / citations / tool_rounds /
   usage / answer`。检查点带过来的上一轮片段绝不能被追加进来（D7）。
2. **编号只有一个来源**：`build_citations(chunks)` 的 `index = 全局位置 + 1`。
   `tools` 节点把 `len(chunks) + 1` 作为下一批的起点传给 `search_docs`，
   于是 `ToolMessage.content` 里的 `[n]` 与 `citations[n-1]` 恒等（D4）。
3. **送给模型的消息序列里只有一条 system，且永远在最前**。片段块是**一条
   `HumanMessage`**，只在 `agent` 节点里现渲染、**不写回状态** ——
   于是"历史被裁了但 system 还在"是结构事实，前缀缓存也照常命中（D9 / D8）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Any, Literal, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.prompts import ChatPromptTemplate
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from pyassist.agent.registry import get_tool, tool_specs
from pyassist.agent.state import QAState
from pyassist.agent.tools.docs import Fetcher, Searcher, _default_fetch, _default_search
from pyassist.config import (
    DEFAULT_TOP_K,
    MAX_HISTORY_MESSAGES,
    MAX_TOOL_ROUNDS,
    OUTPUT_LANGUAGE,
    QA_CONTEXT_PROMPT_NAME,
    QA_PROMPT_NAME,
    QA_TOOL_NAMES,
)
from pyassist.errors import ConfigError, LLMBadResponse, RetrievalEmpty
from pyassist.llm.client import get_chat_model
from pyassist.llm.prompts import PROMPT_DIR, load_prompt
from pyassist.llm.tracing import UsageSnapshot, extract_usage, log_llm_response, model_id_of
from pyassist.logging import get_logger
from pyassist.rag.citations import (
    Citation,
    build_citations,
    find_ungrounded_markers,
    render_reference_block,
)
from pyassist.rag.retriever import assert_index_ready

logger = get_logger(__name__)

_MARKER_RE = re.compile(r"\[(\d+)\]")

_TOOL_LIMIT_NOTE = "（已达到工具调用上限，可能有未覆盖的内容）"

_ZERO_USAGE = UsageSnapshot(
    prompt_tokens=0,
    completion_tokens=0,
    cached_tokens=0,
    cost_usd=Decimal(0),
    is_off_peak=True,
)


@dataclass(frozen=True, slots=True)
class QAGraph:
    """编译好的图 + 与它同源的检索上下文。**两者必须一起走**（SPEC §5.5）。

    开流前的预检（D6）必须用图节点用的**同一组**依赖，否则会出现
    "预检用 bge-m3 校验一个哈希夹具建的集合"这种只在测试里发生的漂移。
    """

    # 四个类型参数是 `CompiledStateGraph[StateT, ContextT, InputT, OutputT]`。
    # 图的状态已经由 `QAState` 钉死，调用方只通过 `astream` / `aget_state` 交互，
    # 故这里用 `Any` 而不去复刻 langgraph 的内部泛型推导。
    graph: CompiledStateGraph[Any, Any, Any, Any]
    preflight: Callable[[], Awaitable[None]]
    # 账本要 `model_id`，而 `UsageSnapshot` 不含它：构建时记下，记账处不猜（D8）。
    model_id: str


def build_qa_graph(
    *,
    checkpointer: BaseCheckpointSaver[str] | None,
    model: BaseChatModel | None = None,
    search: Searcher | None = None,
    fetch: Fetcher | None = None,
    preflight: Callable[[], Awaitable[None]] | None = None,
) -> QAGraph:
    """拼图并编译。`model=None` / 三个可调用对象全 `None` 时走生产默认。"""
    searcher, fetcher, preflight_fn = _resolve_deps(search, fetch, preflight)
    chat_model = model if model is not None else get_chat_model()

    async def retrieve(state: QAState) -> dict[str, Any]:
        question = state["question"]
        started = time.perf_counter()
        chunks = await searcher(question, DEFAULT_TOP_K)
        if not chunks:
            # 空检索是**正常业务结果**（§3.2 映射到 200），但在图里它必须终止流程：
            # 没有片段就没法"基于文档回答"，而凭记忆作答是产品原则明令禁止的。
            raise RetrievalEmpty(query=question)

        citations = build_citations(chunks)
        logger.info(
            "qa.retrieve",
            doc_ids=[chunk.chunk_id for chunk in chunks],
            citations=len(citations),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        get_stream_writer()({"type": "citations", "citations": _serialize_citations(citations)})
        return {
            "chunks": list(chunks),
            "citations": citations,
            "tool_rounds": 0,
            "usage": _ZERO_USAGE,
            "answer": "",
            # 只有 messages 是追加语义（add_messages），其余一律覆盖（D7）
            "messages": [HumanMessage(str(question))],
        }

    async def agent(state: QAState) -> dict[str, Any]:
        messages = _render_model_messages(state["messages"], state["citations"], state["question"])
        bound = chat_model.bind_tools(tool_specs(QA_TOOL_NAMES))
        writer = get_stream_writer()
        started = time.perf_counter()
        aggregate: AIMessageChunk | None = None
        async for piece in bound.astream(messages):
            # `astream` 的静态返回类型是 `BaseMessage`（运行期永远是 chunk），
            # 不 cast 的话 `aggregate + piece` 会被推断成 `AIMessage | BaseMessageChunk`。
            piece_chunk = cast("AIMessageChunk", piece)
            aggregate = piece_chunk if aggregate is None else aggregate + piece_chunk
            text = _content_text(piece.content)
            if text:
                writer({"type": "token", "text": text})

        if aggregate is None:
            raise LLMBadResponse("模型没有返回任何内容", details={"stage": "agent"})

        reply = _to_ai_message(aggregate)
        model_id = model_id_of(chat_model)
        usage = extract_usage(reply, model_id=model_id)
        log_llm_response(
            usage,
            model_id=model_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return {"messages": [reply], "usage": usage}

    async def tools(state: QAState) -> dict[str, Any]:
        last = state["messages"][-1]
        tool_calls: list[dict[str, Any]] = [
            dict(call) for call in (getattr(last, "tool_calls", None) or [])
        ]
        writer = get_stream_writer()
        chunks = list(state["chunks"])
        # 并行 tool_calls 必须**递增**游标：各自都取"当前长度 + 1"会让两个
        # search_docs 拿到同一个起点，`[6]` 指向两页不同文档（SPEC §5.5）。
        next_index = len(chunks) + 1
        produced: list[ToolMessage] = []

        for tool_call in tool_calls:
            name = str(tool_call["name"])
            writer({"type": "tool", "phase": "start", "name": name})
            message = await get_tool(name).ainvoke(
                _tool_call_with_injection(
                    tool_call, next_index=next_index, searcher=searcher, fetcher=fetcher
                )
            )
            if not isinstance(message, ToolMessage):
                raise LLMBadResponse(
                    f"工具 {name!r} 返回了非 ToolMessage 对象：{type(message).__name__}",
                    details={"tool_name": name},
                )
            produced.append(message)
            artifact = message.artifact if isinstance(message.artifact, list) else []
            chunks.extend(artifact)
            next_index += len(artifact)  # 只有 search_docs 有 artifact
            writer({"type": "tool", "phase": "end", "name": name})

        citations = build_citations(chunks)
        writer({"type": "citations", "citations": _serialize_citations(citations)})
        return {
            "messages": produced,
            "chunks": chunks,
            "citations": citations,
            "tool_rounds": int(state["tool_rounds"]) + 1,
        }

    async def finalize(state: QAState) -> dict[str, Any]:
        messages = state["messages"]
        answer = _last_ai_content(messages)
        citations = state["citations"]

        ungrounded = find_ungrounded_markers(answer, citations)
        if ungrounded:
            # 本阶段不重生成（重生成要再付一次钱）：剥掉越界标记就够了 ——
            # 它已经不再指向不存在的来源（MAX_GROUNDING_RETRIES = 0，D4）。
            logger.warning(
                "qa.ungrounded_markers",
                markers=sorted(ungrounded),
                citation_count=len(citations),
            )
            answer = _strip_markers(answer, ungrounded)

        if int(state["tool_rounds"]) >= MAX_TOOL_ROUNDS and _last_has_tool_calls(messages):
            # 宁可少说，不可编造：到上限时明确告诉用户"可能没覆盖全"。
            answer = _append_note(answer, _TOOL_LIMIT_NOTE)

        block = render_reference_block(citations)
        if block:
            # 即使模型一个 [n] 都没写，来源块仍然在（§5.2 判据 2 的兜底）
            answer = _append_note(answer, block)
        return {"answer": answer}

    builder = StateGraph(QAState)
    builder.add_node("retrieve", retrieve)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "agent")
    builder.add_conditional_edges(
        "agent",
        _should_continue,
        {"tools": "tools", "finalize": "finalize"},
    )
    builder.add_edge("tools", "agent")
    builder.add_edge("finalize", END)

    compiled = builder.compile(checkpointer=checkpointer)
    return QAGraph(graph=compiled, preflight=preflight_fn, model_id=model_id_of(chat_model))


def _resolve_deps(
    search: Searcher | None,
    fetch: Fetcher | None,
    preflight: Callable[[], Awaitable[None]] | None,
) -> tuple[Searcher, Fetcher, Callable[[], Awaitable[None]]]:
    """三个要么全给、要么全不给。混着给在**构图时**就炸，不等开流才炸。

    只注入一半时，会出现"预检说索引兼容、检索却漂移"—— 那种错只在测试里发生，
    生产测试不出来（SPEC D11）。
    """
    injected = (search is not None, fetch is not None, preflight is not None)
    if len(set(injected)) > 1:
        raise ConfigError("search / fetch / preflight 必须同时注入或同时省略")
    if search is not None and fetch is not None and preflight is not None:
        return search, fetch, preflight
    return _default_search, _default_fetch, assert_index_ready  # 生产：全局依赖


def _should_continue(state: QAState) -> Literal["tools", "finalize"]:
    """`agent` 之后的唯一分支：有 tool_calls 且没到轮次上限就进工具。"""
    last = state["messages"][-1]
    has_calls = isinstance(last, AIMessage) and bool(last.tool_calls)
    if has_calls and int(state["tool_rounds"]) < MAX_TOOL_ROUNDS:
        return "tools"
    return "finalize"


def _system_message() -> SystemMessage:
    """`qa.jinja2` 渲染成的系统提示。**逐字节稳定**，每轮都一样（前缀缓存，§1）。"""
    rendered = load_prompt(QA_PROMPT_NAME).format_messages(
        history=[], output_language=OUTPUT_LANGUAGE
    )
    return cast("SystemMessage", rendered[0])


@cache
def _context_template() -> ChatPromptTemplate:
    """`qa_context.jinja2` 的**独立加载器**。**不走 `load_prompt()`**（L17 实测）。

    `load_prompt()` 把模板当 `system` 角色并要求一个 `history` 占位符：
    少传 history 直接 `KeyError`；补上之后得到的是**第二条 SystemMessage**，
    它会插到历史中间，前缀缓存的前提（system 恒在最前）当场破裂。
    这里唯一区别是角色 `human`、不带 placeholder。
    """
    path = PROMPT_DIR / f"{QA_CONTEXT_PROMPT_NAME}.jinja2"
    return ChatPromptTemplate.from_messages(
        [("human", path.read_text(encoding="utf-8"))], template_format="jinja2"
    )


def _render_model_messages(
    messages: Sequence[AnyMessage],
    citations: Sequence[Citation],
    question: str,
) -> list[BaseMessage]:
    """拼出**这一轮**要发给模型的消息序列（不写回状态）。

    `[SystemMessage] + trimmed_history[:-1] + [HumanMessage(渲染后的片段块)]`
    —— 用渲染块**替换**最后那条裸提问，不是追加。理由三条：
    1. 追加会让模型连看两条 human，且问题在状态里出现两次；
    2. 渲染块只在这一轮存在，**不进状态**（否则检查点里每轮多存一份全文）；
    3. 变长的片段块永远在**最后**，system + 老历史逐字节稳定 → 前缀缓存照常命中。
    """
    trimmed = _trim_history(messages, limit=MAX_HISTORY_MESSAGES)
    if not trimmed or not isinstance(trimmed[-1], HumanMessage):
        # 工具轮：最后一条是 ToolMessage，原样发（再塞一遍片段块会让模型重复看同批证据）
        return [_system_message(), *trimmed]
    context = _context_template().format_messages(citations=citations, question=question)
    return [_system_message(), *trimmed[:-1], *context]


def _trim_history(
    messages: Sequence[AnyMessage], *, limit: int = MAX_HISTORY_MESSAGES
) -> list[BaseMessage]:
    """把老轮次裁掉，但**绝不在一组 `AIMessage(tool_calls) → ToolMessage*` 中间切**（D8）。

    组边界是 `HumanMessage`。从尾部往前凑满 `limit` 组就停；不完整的组整组丢 ——
    切断了会让模型看到"调用了 `call_1` 却没有任何结果"，触发它的重试冲动（白烧钱）。

    状态里**没有** `SystemMessage`，所以不需要"保留开头那一条"的特例：
    系统提示在渲染时才拼上去（`_render_model_messages`）。
    """
    groups: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage) and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)

    kept: list[list[BaseMessage]] = []
    for group in reversed(groups):
        if len(kept) >= limit:
            break
        if not _is_complete_group(group):
            # 尾部停在 AIMessage(tool_calls) 之后、ToolMessage 之前：整组丢。
            if not kept:
                continue
            break
        kept.append(group)
    kept.reverse()

    trimmed = [message for group in kept for message in group]
    if trimmed:
        return trimmed
    # 全部组都不完整（病态输入）：至少别给模型一个空列表。
    return list(messages[-1:])


def _is_complete_group(group: Sequence[BaseMessage]) -> bool:
    """组内每个 `AIMessage.tool_calls` 都由对应的 `ToolMessage` 配上了。"""
    pending: set[str] = set()
    for message in group:
        if isinstance(message, AIMessage) and message.tool_calls:
            pending.update(str(call["id"]) for call in message.tool_calls)
        elif isinstance(message, ToolMessage):
            pending.discard(str(message.tool_call_id))
    return not pending


def _tool_call_with_injection(
    tool_call: dict[str, Any], *, next_index: int, searcher: Searcher, fetcher: Fetcher
) -> dict[str, Any]:
    """把注入参数并进 `tool_call["args"]`，**但保持 `ToolCall` 形状**（L18）。

    ⚠️ **不能**只传 `{**tc["args"], ...}` —— 那会让 `ainvoke` 返回裸 `str`，
    `ToolMessage.artifact` 与 `tool_call_id` 全部消失。必须原样带上
    `name` / `id` / `type` 三个键。

    按工具名**分别**组装：`read_doc` 没有 `start_index`，多传虽然会被静默丢掉，
    但"静默丢键"正是最难查的那类 bug。
    """
    args = dict(tool_call["args"])
    if tool_call["name"] == "search_docs":
        args.update(start_index=next_index, searcher=searcher)
    else:  # read_doc
        args["fetcher"] = fetcher
    return {**tool_call, "args": args}


def _to_ai_message(chunk: AIMessageChunk) -> AIMessage:
    """`AIMessageChunk` → `AIMessage`，**必须带上 `additional_kwargs`**。

    `reasoning_content` 就在这里：思考模式下 DeepSeek 把推理过程放在首块的
    `additional_kwargs` 里，聚合体保留它；下一轮请求由 `ChatDeepSeekPatched`
    写回出站 payload。丢了它，回环第二轮就是 400（SPEC D9 / R1）。
    """
    return AIMessage(
        content=chunk.content,
        additional_kwargs=dict(chunk.additional_kwargs),
        response_metadata=dict(chunk.response_metadata),
        tool_calls=list(chunk.tool_calls),
        invalid_tool_calls=list(chunk.invalid_tool_calls),
        usage_metadata=chunk.usage_metadata,
        id=chunk.id,
    )


def _content_text(content: Any) -> str:
    """把 `AIMessageChunk.content` 归一成文本（字符串或内容块列表都支持）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _last_ai_content(messages: Sequence[AnyMessage]) -> str:
    """最后一条 `AIMessage` 的文本；没有就是空串。"""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not isinstance(message, AIMessageChunk):
            return _content_text(message.content)
    return ""


def _last_has_tool_calls(messages: Sequence[AnyMessage]) -> bool:
    return bool(messages) and isinstance(messages[-1], AIMessage) and bool(messages[-1].tool_calls)


def _strip_markers(text: str, markers: Collection[int]) -> str:
    """剥掉越界的 `[n]`（本阶段唯一的接地失败处置，D4）。

    只删标记本身，不动句子 —— 重写句子需要再来一次模型调用，而"标记消失"已经
    消除了"指向不存在的来源"这个错误。
    """
    return _MARKER_RE.sub(
        lambda match: "" if int(match.group(1)) in markers else match.group(0), text
    )


def _append_note(answer: str, note: str) -> str:
    return f"{answer}\n\n{note}" if answer.strip() else note


def _serialize_citations(citations: Sequence[Citation]) -> list[dict[str, Any]]:
    """`Citation` → 线上事件形状（与 `api/schemas.CitationData` 字段逐字一致）。"""
    return [
        {
            "index": citation.index,
            "title": citation.title,
            "section": citation.section,
            "url": citation.url,
            "snippet": citation.snippet,
        }
        for citation in citations
    ]


__all__ = [
    "QAGraph",
    "_render_model_messages",
    "_trim_history",
    "build_qa_graph",
]
