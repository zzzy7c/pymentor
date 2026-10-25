"""`POST /chat` 的线上字节与断开语义（SPEC §5.17，判据 7–8 / 14 / 14b）。

这些断言全部是**字节级**的，不是"事件对象里有什么"。原因：SSE 的坑都在线上——
`\r\n\r\n` vs `\n\n`、被代理缓冲、客户端断开后生成器拒绝退出。
它们每一个都"本地看起来跑通了"。

⑦–⑩ 直接测 `_sse_stream`：经过 HTTP 层就观察不到"生成器被 aclose 之后
上游有没有收尾"，而那是 L19 / L20 两个死锁的落点。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from chromadb.api.models.Collection import Collection
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from conftest import create_business_tables
from fakes.embedder import HashingEmbedder
from fakes.llm import ScriptedChatModel
from pyassist.agent.state import QaEvent
from pyassist.api.main import create_app
from pyassist.api.routers.chat import _sse_stream
from pyassist.errors import CorpusMissing
from pyassist.rag import retriever
from pyassist.rag.retriever import RetrievedChunk
from pyassist.services.qa import stream_answer

_QUESTION = "asyncio.gather 里某个任务抛异常会怎样？"


def _ai(content: str) -> AIMessage:
    return AIMessage(
        content=content,
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


def _deps(
    collection: Collection,
    embedder: HashingEmbedder,
    *,
    search: Callable[[str, int], Awaitable[list[RetrievedChunk]]] | None = None,
    preflight: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    async def default_search(query: str, top_k: int) -> list[RetrievedChunk]:
        return await retriever.retrieve(
            query, top_k=top_k, collection=collection, embedder=embedder
        )

    async def fetch(doc_url: str) -> list[RetrievedChunk]:
        return await retriever.fetch_document(doc_url, collection=collection)

    async def default_preflight() -> None:
        await retriever.assert_index_ready(collection=collection, embedder=embedder)

    return {
        "search": search or default_search,
        "fetch": fetch,
        "preflight": preflight or default_preflight,
    }


def _app(
    tmp_database_url: str,
    collection: Collection,
    embedder: HashingEmbedder,
    model: ScriptedChatModel,
    **deps: Any,
):
    create_business_tables(tmp_database_url)
    return create_app(
        database_url=tmp_database_url,
        rate_limit_enabled=False,
        chat_model=model,
        **_deps(collection, embedder, **deps),
    )


async def _collect(agen: AsyncIterator[Any]) -> list[Any]:
    return [item async for item in agen]


# ---------- ①② 头与线上字节 ----------


def test_headers_and_wire_format(qa_client, scripted_model) -> None:
    scripted_model.script = [_ai("答案（见 [1]）。")]

    response = qa_client.post("/chat", json={"question": _QUESTION})

    assert response.status_code == 200
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"

    body = response.content
    assert b"event: token\ndata: " in body
    assert b"event: citations\ndata: " in body
    assert b"event: done\ndata: " in body
    assert b"\r\n" not in body  # 库默认分隔符是 \r\n\r\n（L11）
    assert body.endswith(b"\n\n")


def test_sse_frames_are_json_with_expected_keys(qa_client, scripted_model) -> None:
    scripted_model.script = [_ai("答案（见 [1]）。")]
    body = qa_client.post("/chat", json={"question": _QUESTION}).text

    frames = [frame for frame in body.split("\n\n") if frame]
    done_frame = next(frame for frame in frames if frame.startswith("event: done"))
    done = json.loads(done_frame.split("data: ", 1)[1])
    assert set(done) == {"request_id", "usage"}
    assert set(done["usage"]) == {
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cost_usd",
        "is_off_peak",
    }
    assert done["usage"]["prompt_tokens"] == 10


# ---------- ③ 心跳 ----------


class _SlowModel(ScriptedChatModel):
    """先睡一会儿再吐字：给心跳一个必须出现的窗口。"""

    async def _astream(
        self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        await asyncio.sleep(0.3)
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            yield chunk


def test_heartbeat_arrives_on_slow_graph(
    monkeypatch, tmp_database_url, collection, hashing_embedder
) -> None:
    monkeypatch.setattr("pyassist.api.routers.chat.SSE_HEARTBEAT_SECONDS", 0.05)
    model = _SlowModel()
    model.script = [_ai("慢答案（见 [1]）。")]

    with TestClient(_app(tmp_database_url, collection, hashing_embedder, model)) as client:
        body = client.post("/chat", json={"question": _QUESTION}).content

    assert b": heartbeat\n\n" in body
    # 心跳不能吃掉内容：答案仍然完整
    assert b"event: token" in body
    assert body.endswith(b"\n\n")


# ---------- ④ 不泄露 LangGraph 原始事件 ----------


def test_no_raw_langgraph_event_names(qa_client, scripted_model) -> None:
    scripted_model.script = [_ai("答案（见 [1]）。")]
    body = qa_client.post("/chat", json={"question": _QUESTION}).content

    for raw in (b"on_chat_model_stream", b"on_tool_start", b"on_tool_end", b"updates", b"custom"):
        assert raw not in body


# ---------- ⑤ 空检索 ----------


def test_empty_retrieval_streams_error_and_done_only(
    tmp_database_url, collection, hashing_embedder
) -> None:
    async def empty_search(query: str, top_k: int) -> list[RetrievedChunk]:
        return []

    model = ScriptedChatModel()
    model.script = [_ai("不该出现")]
    app = _app(tmp_database_url, collection, hashing_embedder, model, search=empty_search)

    with TestClient(app) as client:
        body = client.post("/chat", json={"question": _QUESTION}).content

    assert b"event: error" in body
    assert b'"code": "RETRIEVAL_EMPTY"' in body
    assert b"event: done" in body
    assert b"event: token" not in body


# ---------- ⑥ 索引未建：503 真信封 ----------


def test_index_missing_returns_real_envelope_not_a_stream(
    tmp_database_url, collection, hashing_embedder, scripted_model
) -> None:
    async def missing() -> None:
        raise CorpusMissing("索引未建立")

    app = _app(tmp_database_url, collection, hashing_embedder, scripted_model, preflight=missing)

    with TestClient(app) as client:
        response = client.post("/chat", json={"question": _QUESTION})

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    # 流式头不该出现在非流式错误上
    assert "x-accel-buffering" not in response.headers
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CORPUS_MISSING"


# ---------- ⑦ 中途 aclose：不抛 GeneratorExit 相关错误，上游被收尾 ----------


async def test_aclose_mid_stream_closes_upstream() -> None:
    closed = asyncio.Event()

    async def source() -> AsyncIterator[QaEvent]:
        try:
            index = 0
            while True:
                yield {"type": "token", "text": str(index)}
                index += 1
        finally:
            closed.set()

    stream = _sse_stream(source(), queue=asyncio.Queue(maxsize=2), heartbeat_seconds=10)
    first = await asyncio.wait_for(anext(stream), timeout=1)
    assert first.event == "token"

    await asyncio.wait_for(stream.aclose(), timeout=1)  # L19：不得抛 RuntimeError
    await asyncio.wait_for(closed.wait(), timeout=1)  # 上游图任务确实被停掉


# ---------- ⑧ 队列满 + 断开：不许死锁 ----------


async def test_aclose_with_full_queue_does_not_deadlock() -> None:
    async def source() -> AsyncIterator[QaEvent]:
        index = 0
        while True:
            yield {"type": "token", "text": str(index)}
            index += 1

    # maxsize=1：生产者在第二次 put 时被背压卡住，正是哨兵死锁的触发条件（L20）
    stream = _sse_stream(source(), queue=asyncio.Queue(maxsize=1), heartbeat_seconds=10)
    await asyncio.wait_for(anext(stream), timeout=1)
    await asyncio.sleep(0.05)  # 让生产者填满队列
    await asyncio.wait_for(stream.aclose(), timeout=1)


# ---------- ⑨ 非 AppError：error + done，且不泄露原文 ----------


async def test_unexpected_crash_still_emits_error_and_done() -> None:
    class _BoomGraph:
        async def astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            raise ValueError("secret-internal-detail")
            yield  # pragma: no cover

    frames = await _collect(
        _sse_stream(
            # 鸭子类型的图替身：本用例只关心 `astream` 抛非 AppError 时的事件形状
            stream_answer(_QUESTION, graph=_BoomGraph(), thread_id="t", request_id="r"),
            queue=asyncio.Queue(maxsize=8),
            heartbeat_seconds=10,
        )
    )
    body = b"".join(frame.encode() for frame in frames)

    assert b"event: error" in body
    assert b"INTERNAL_ERROR" in body
    assert b"event: done" in body
    assert b"secret-internal-detail" not in body


# ---------- ⑩ 空流：立刻结束，且不发心跳 ----------


async def test_empty_stream_ends_without_heartbeat() -> None:
    async def empty() -> AsyncIterator[QaEvent]:
        return
        yield  # pragma: no cover

    frames = await asyncio.wait_for(
        _collect(_sse_stream(empty(), queue=asyncio.Queue(maxsize=8), heartbeat_seconds=10)),
        timeout=1,
    )
    assert frames == []


@pytest.mark.parametrize("path", ["/chat"])
def test_chat_rejects_empty_question(path: str, qa_client) -> None:
    """空问题在**开流前**变成 422 真信封，而不是流到一半才炸（D6）。"""
    response = qa_client.post(path, json={"question": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
