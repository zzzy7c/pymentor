"""v1 问答图（SPEC §5.17，判据 1–6 / 12–13）。

全部离线：
- 图本身用 `ScriptedChatModel`（`tests/fakes/llm.py`），不触网；
- 只有 ⑤ 用 `respx` 打桩真实 `ChatDeepSeekPatched` 的 **SSE** 响应 ——
  那条断言的对象是"出站 JSON 里有没有 `reasoning_content`"，
  用 fake 模型就测不到出站序列化这一步（那正是本阶段最高风险点 R1）。

检索一律走阶段五的夹具集合（`collection` + `hashing_embedder`），
与 `test_retriever.py` 同一份语料 —— 于是"检索对但图错"与"图对但检索错"能区分开。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx
from chromadb.api.models.Collection import Collection
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import MessagesState, add_messages
from pydantic import SecretStr

from fakes.embedder import HashingEmbedder
from fakes.llm import ScriptedChatModel
from pyassist.agent.checkpointer import open_checkpointer
from pyassist.agent.graphs.qa import QAGraph, _trim_history, build_qa_graph
from pyassist.agent.state import QaEvent
from pyassist.config import MODEL_FLASH, settings
from pyassist.errors import ConfigError, RetrievalEmpty
from pyassist.llm.client import get_chat_model
from pyassist.rag import retriever
from pyassist.rag.citations import Citation
from pyassist.rag.retriever import RetrievedChunk
from pyassist.services.qa import answer, stream_answer

TEST_API_KEY = SecretStr("test-only-placeholder-key")
CHAT_URL = f"{settings.deepseek_api_base_url}/chat/completions"

_QUESTION = "asyncio.gather 里某个任务抛异常会怎样？"


# ---------- 夹具与小工具 ----------


def _deps(
    collection: Collection,
    embedder: HashingEmbedder,
    *,
    search: Callable[[str, int], Awaitable[list[RetrievedChunk]]] | None = None,
) -> tuple[
    Callable[[str, int], Awaitable[list[RetrievedChunk]]],
    Callable[[str], Awaitable[list[RetrievedChunk]]],
    Callable[[], Awaitable[None]],
]:
    """三个依赖**成对**组装（D11）：测试里也绝不半套注入。"""

    async def default_search(query: str, top_k: int) -> list[RetrievedChunk]:
        return await retriever.retrieve(
            query, top_k=top_k, collection=collection, embedder=embedder
        )

    async def fetch(doc_url: str) -> list[RetrievedChunk]:
        return await retriever.fetch_document(doc_url, collection=collection)

    async def preflight() -> None:
        await retriever.assert_index_ready(collection=collection, embedder=embedder)

    return search or default_search, fetch, preflight


def _ai(
    content: str,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning: str | None = None,
    cache_read: int = 64,
) -> AIMessage:
    """带 usage 的脚本消息。`usage_metadata` 让成本记账与检查点往返有真实数值可断言。"""
    return AIMessage(
        content=content,
        tool_calls=list(tool_calls or []),
        additional_kwargs={} if reasoning is None else {"reasoning_content": reasoning},
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": cache_read},
        },
    )


def _config(thread_id: str = "thread-1") -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


async def _run(
    qa: QAGraph, question: str = _QUESTION, *, thread_id: str = "thread-1"
) -> tuple[list[QaEvent], dict[str, Any]]:
    """跑一遍图，收集 `custom` 事件并读终态（与 `services/qa.py` 同一条调用形状）。"""
    events: list[QaEvent] = []
    config = _config(thread_id)
    async for mode, payload in qa.graph.astream(
        {"question": question}, config, stream_mode=["custom", "updates"]
    ):
        if mode == "custom":
            events.append(payload)
    snapshot = await qa.graph.aget_state(config)
    return events, dict(snapshot.values)


@pytest.fixture
async def saver(tmp_path) -> AsyncIterator[Any]:
    """临时检查点库。**不用 MemorySaver**（AGENTS.md §2 明令禁止）。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'checkpoint.db').as_posix()}"
    async with open_checkpointer(database_url=url) as handle:
        yield handle


# ---------- ① 实质路径 ----------


async def test_answer_flow_emits_tokens_and_reference_block(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    scripted_model.script = [_ai("gather 的异常会立即传播给等待者（见 [1]）。")]
    qa = build_qa_graph(
        checkpointer=saver, model=scripted_model, **_deps_kwargs(collection, hashing_embedder)
    )

    events, values = await _run(qa)

    assert [event["type"] for event in events if event["type"] == "token"]
    assert any(event["type"] == "citations" for event in events)
    # finalize 追加来源块：即使模型只写了一个 [1]，来源也是可点击的
    assert "**来源**" in values["answer"]
    assert "https://docs.python.org/3.12/library/asyncio-task.html" in values["answer"]


def _deps_kwargs(
    collection: Collection,
    embedder: HashingEmbedder,
    *,
    search: Callable[[str, int], Awaitable[list[RetrievedChunk]]] | None = None,
) -> dict[str, Any]:
    s, f, p = _deps(collection, embedder, search=search)
    return {"search": s, "fetch": f, "preflight": p}


# ---------- ②③ 引用编号 ----------


async def test_citation_indices_are_continuous_from_one(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    scripted_model.script = [_ai("见 [1]。")]
    qa = build_qa_graph(
        checkpointer=saver, model=scripted_model, **_deps_kwargs(collection, hashing_embedder)
    )
    _, values = await _run(qa)

    citations: list[Citation] = values["citations"]
    assert [citation.index for citation in citations] == list(range(1, len(citations) + 1))
    # 每个 [n] 都能在 citations 里找到（§5.2 判据 2）
    indices = {citation.index for citation in citations}
    assert all(int(marker) in indices for marker in _markers(values["answer"]))


def _markers(text: str) -> list[str]:
    return re.findall(r"\[(\d+)\]", text)


# ---------- ④ 空检索 ----------


async def test_empty_retrieval_surfaces_as_retrieval_empty(saver: Any, hashing_embedder) -> None:
    async def empty_search(query: str, top_k: int) -> list[RetrievedChunk]:
        return []

    async def fetch(doc_url: str) -> list[RetrievedChunk]:
        return []

    async def preflight() -> None:
        return None

    model = ScriptedChatModel()
    model.script = [_ai("这条永远不该被生成。")]
    qa = build_qa_graph(
        checkpointer=saver,
        model=model,
        search=empty_search,
        fetch=fetch,
        preflight=preflight,
    )

    # 图里：空检索直接抛（终止流程），模型一次都没被调用
    with pytest.raises(RetrievalEmpty):
        await _run(qa)
    assert model.requests == []

    # 服务层：事件里只有 error + done，**零 token**；`answer()` 给 status="empty"
    events = [
        event
        async for event in stream_answer(_QUESTION, graph=qa.graph, thread_id="x", request_id="r")
    ]
    assert [event["type"] for event in events] == ["error", "done"]
    assert events[0]["code"] == "RETRIEVAL_EMPTY"

    result = await answer(_QUESTION, graph=qa.graph, thread_id="y", request_id="r")
    assert result.status == "empty"
    assert result.answer == ""
    assert result.citations == []


# ---------- ⑤ 连续两轮工具调用 + reasoning_content（respx，SSE）----------


def _sse_chunk(
    delta: dict[str, Any], *, finish: str | None = None, usage: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "cmpl",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL_FLASH,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def _sse_response(*chunks: dict[str, Any]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _tool_delta(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_calls": [
            {
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    }


@respx.mock
async def test_two_tool_rounds_keep_reasoning_content_in_third_request(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder
) -> None:
    respx.post(CHAT_URL).mock(
        side_effect=[
            _sse_response(
                _sse_chunk({"role": "assistant", "content": "", "reasoning_content": "第一步"}),
                _sse_chunk(_tool_delta("call_1", "search_docs", {"query": "asyncio.gather"})),
                _sse_chunk({}, finish="tool_calls"),
            ),
            _sse_response(
                _sse_chunk({"role": "assistant", "content": "", "reasoning_content": "第二步"}),
                _sse_chunk(_tool_delta("call_2", "search_docs", {"query": "异常 传播"})),
                _sse_chunk({}, finish="tool_calls"),
            ),
            _sse_response(
                _sse_chunk(
                    {"role": "assistant", "content": "gather 会立即传播首个异常（见 [1]）。"}
                ),
                _sse_chunk(
                    {},
                    finish="stop",
                    usage={"prompt_tokens": 300, "completion_tokens": 30, "total_tokens": 330},
                ),
            ),
        ]
    )

    qa = build_qa_graph(
        checkpointer=saver,
        model=get_chat_model(api_key=TEST_API_KEY),
        **_deps_kwargs(collection, hashing_embedder),
    )
    _, values = await _run(qa)

    assert len(respx.calls) == 3
    third_payload = json.loads(respx.calls[2].request.content)
    assistants = [m for m in third_payload["messages"] if m["role"] == "assistant"]
    assert len(assistants) == 2
    # ← 判据 5：少了任何一个，DeepSeek 都会在第三轮返回 400
    assert [m.get("reasoning_content") for m in assistants] == ["第一步", "第二步"]
    assert "gather" in values["answer"]


# ---------- ⑥ 检查点往返与状态 schema ----------


async def test_checkpoint_roundtrip_preserves_types(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    from decimal import Decimal

    scripted_model.script = [_ai("见 [1]。"), _ai("见 [1]。")]
    qa = build_qa_graph(
        checkpointer=saver, model=scripted_model, **_deps_kwargs(collection, hashing_embedder)
    )
    await _run(qa)

    _, values = await _run(qa, thread_id="thread-1")  # 第二次读同一 thread 的终态
    assert isinstance(values["chunks"][0], RetrievedChunk)
    assert isinstance(values["usage"].cost_usd, Decimal)


def test_state_is_superset_of_messages_state() -> None:
    from typing import get_type_hints

    from pyassist.agent.state import AppState, QAState

    base = set(get_type_hints(MessagesState, include_extras=True))
    hints = get_type_hints(QAState, include_extras=True)
    assert base <= set(hints)
    # messages 的 reducer 必须仍是 add_messages（追加语义），不是覆盖
    assert (
        hints["messages"].__metadata__
        == get_type_hints(MessagesState, include_extras=True)["messages"].__metadata__
    )
    assert add_messages in hints["messages"].__metadata__
    # 本阶段的四个业务键**一律覆盖**（D7）：它们的 metadata 不是 add_messages
    for key in ("chunks", "citations", "usage", "tool_rounds", "answer"):
        assert add_messages not in get_type_hints(AppState, include_extras=True)[key].__metadata__


# ---------- ⑦ 同 thread 两轮：chunks 覆盖而不是追加 ----------


async def test_second_turn_overwrites_chunks(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    scripted_model.script = [_ai("第一轮（见 [1]）。"), _ai("第二轮（见 [1]）。")]
    qa = build_qa_graph(
        checkpointer=saver, model=scripted_model, **_deps_kwargs(collection, hashing_embedder)
    )

    await _run(qa, "asyncio.gather 异常", thread_id="same")
    _, second = await _run(qa, "JSON 编码", thread_id="same")

    expected = await retriever.retrieve(
        "JSON 编码", top_k=5, collection=collection, embedder=hashing_embedder
    )
    # 整表相等（而不是"不含第一轮的 id"）：追加会让长度翻倍，这条一定红
    assert [chunk.chunk_id for chunk in second["chunks"]] == [chunk.chunk_id for chunk in expected]
    assert second["tool_rounds"] == 0


# ---------- ⑧ _trim_history 组完整性 ----------


def test_trim_history_never_splits_tool_round() -> None:
    first_user = HumanMessage("第一问")
    first_ai = AIMessage(content="答一")

    second_user = HumanMessage("第二问")
    second_call = AIMessage(
        content="", tool_calls=[{"name": "search_docs", "args": {"query": "x"}, "id": "call_1"}]
    )
    second_tool = ToolMessage(content="结果", tool_call_id="call_1")
    second_ai = AIMessage(content="答二")

    trimmed = _trim_history(
        [first_user, first_ai, second_user, second_call, second_tool, second_ai], limit=1
    )
    # 只留最后一组，且组内 tool 往返完整
    assert trimmed == [second_user, second_call, second_tool, second_ai]

    # 尾部缺 ToolMessage 的组整组丢掉，**不**留下"调用无结果"
    incomplete = _trim_history([first_user, first_ai, second_user, second_call], limit=1)
    assert incomplete == [first_user, first_ai]


# ---------- ⑨ 依赖注入：要么全给要么全不给 ----------


def test_partial_dependency_injection_raises_config_error(
    collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    search, fetch, preflight = _deps(collection, hashing_embedder)
    for kwargs in (
        {"search": search},
        {"fetch": fetch},
        {"preflight": preflight},
    ):
        with pytest.raises(ConfigError):
            build_qa_graph(checkpointer=None, model=scripted_model, **kwargs)


# ---------- ⑩ 并行 tool_calls 的递增游标 ----------


async def test_parallel_tool_calls_use_incrementing_index(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    scripted_model.script = [
        _ai(
            "",
            tool_calls=[
                {"name": "search_docs", "args": {"query": "asyncio.gather"}, "id": "call_1"},
                {"name": "search_docs", "args": {"query": "JSON 编码"}, "id": "call_2"},
            ],
        ),
        _ai("见 [1] 与 [6]。"),
    ]
    qa = build_qa_graph(
        checkpointer=saver, model=scripted_model, **_deps_kwargs(collection, hashing_embedder)
    )
    _, values = await _run(qa)

    tool_messages = [m for m in values["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["call_1", "call_2"]
    assert all(m.artifact for m in tool_messages)

    citations: list[Citation] = values["citations"]
    assert [c.index for c in citations] == list(range(1, len(citations) + 1))
    # 两个 search 各自贡献了片段：第二个工具的**起点**恰好接着第一个的片段数
    first_start = int(_markers(tool_messages[0].content)[0])
    second_start = int(_markers(tool_messages[1].content)[0])
    assert second_start == first_start + len(tool_messages[0].artifact)
    assert second_start > first_start


# ---------- ⑪ system 唯一且逐字节稳定 ----------


async def test_model_sees_one_stable_system_message(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    scripted_model.script = [_ai("见 [1]。"), _ai("见 [1]。")]
    qa = build_qa_graph(
        checkpointer=saver, model=scripted_model, **_deps_kwargs(collection, hashing_embedder)
    )

    await _run(qa, thread_id="a")
    await _run(qa, thread_id="b")

    assert len(scripted_model.requests) == 2
    for messages in scripted_model.requests:
        assert isinstance(messages[0], SystemMessage)
        assert sum(isinstance(m, SystemMessage) for m in messages) == 1
        # 片段块是最后一条 HumanMessage（L17）
        assert isinstance(messages[-1], HumanMessage)

    # 两次渲染逐字节相同 —— 前缀缓存的前提（§1）
    assert scripted_model.requests[0][0].content == scripted_model.requests[1][0].content


# ---------- ⑫ tools 节点拿到的是 ToolMessage ----------


async def test_tool_call_yields_tool_message_with_artifact(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    scripted_model.script = [
        _ai("", tool_calls=[{"name": "search_docs", "args": {"query": "asyncio"}, "id": "c1"}]),
        _ai("见 [1]。"),
    ]
    qa = build_qa_graph(
        checkpointer=saver, model=scripted_model, **_deps_kwargs(collection, hashing_embedder)
    )
    _, values = await _run(qa)

    tool_message = next(m for m in values["messages"] if isinstance(m, ToolMessage))
    assert tool_message.artifact
    # 检查点往返后 artifact 是 JSON 形态，但 `tools` 节点把**同一批**片段追加进了
    # `chunks` —— 后者是 dataclass 且被 serde 还原成原类型（L3）。
    assert len(values["chunks"]) == 5 + len(tool_message.artifact)
    assert all(isinstance(chunk, RetrievedChunk) for chunk in values["chunks"])


# ---------- 工具轮次上限（防"无限循环"）----------


async def test_tool_round_limit_appends_note(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    call = {"name": "search_docs", "args": {"query": "asyncio"}, "id": "c"}
    scripted_model.script = [
        _ai("", tool_calls=[{**call, "id": "c1"}]),
        _ai("", tool_calls=[{**call, "id": "c2"}]),
        _ai("", tool_calls=[{**call, "id": "c3"}]),
        _ai("最终答案（见 [1]）。", tool_calls=[{**call, "id": "c4"}]),
    ]
    qa = build_qa_graph(
        checkpointer=saver, model=scripted_model, **_deps_kwargs(collection, hashing_embedder)
    )
    _, values = await _run(qa)

    assert values["tool_rounds"] == 3
    assert "已达到工具调用上限" in values["answer"]
    # 第四轮脚本不该被消费：到上限后走 finalize，不进 tools
    assert scripted_model.script == []


# ---------- 越界标记被剥掉 ----------


async def test_ungrounded_markers_are_stripped(
    saver: Any, collection: Collection, hashing_embedder: HashingEmbedder, scripted_model
) -> None:
    scripted_model.script = [_ai("有依据 [1]，瞎编 [9]。")]
    qa = build_qa_graph(
        checkpointer=saver, model=scripted_model, **_deps_kwargs(collection, hashing_embedder)
    )
    _, values = await _run(qa)

    assert "[9]" not in values["answer"]
    assert "[1]" in values["answer"]
