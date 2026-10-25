"""v4 推荐图（SPEC §5.7 / D7）：id 校验、空候选短路、引用确定性。

离线：结构化输出走 `ScriptedChatModel.structured_script`，检索是两条固定片段。
被钉住的是**图的判断**：模型编的 id 会不会被剥掉、剥完为空怎么兜底、
空候选时模型到底有没有被调用。
"""

from __future__ import annotations

from typing import Any

import pytest

from fakes.llm import ScriptedChatModel
from pyassist.agent.graphs.progress import _topic_query, build_progress_graph
from pyassist.agent.state import MistakeRecord
from pyassist.agent.tools.progress import NextStepDraft
from pyassist.config import EXERCISE_TOPICS
from pyassist.rag.retriever import RetrievedChunk

_DOC_URL = "https://docs.python.org/3.12/tutorial/datastructures.html"


def _mistake(
    exercise_id: str, *, topic: str = "list", created_at: str = "2026-10-01T12:00:00+00:00"
) -> MistakeRecord:
    return MistakeRecord(
        id=f"m-{exercise_id}",
        exercise_id=exercise_id,
        topic=topic,
        error_signature="test_add: boom",
        is_resolved=False,
        created_at=created_at,
    )


def _chunks() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=f"c{index}",
            text="列表是可变序列。",
            doc_url=_DOC_URL,
            source_url=f"{_DOC_URL}#lists",
            title="Data Structures",
            section="More on Lists",
            score=0.9 - index / 100,
        )
        for index in range(2)
    ]


class _Search:
    def __init__(self, chunks: list[RetrievedChunk] | None = None) -> None:
        self.chunks = _chunks() if chunks is None else chunks
        self.queries: list[tuple[str, int]] = []

    async def __call__(self, query: str, top_k: int) -> list[RetrievedChunk]:
        self.queries.append((query, top_k))
        return list(self.chunks)


async def _fetch(url: str) -> str:
    return "<html></html>"


async def _preflight() -> None:
    return None


def _config() -> dict[str, Any]:
    return {"configurable": {"thread_id": "progress-test"}}


async def _run(
    draft: NextStepDraft | None,
    *,
    mistakes: list[MistakeRecord],
    search: _Search | None = None,
) -> tuple[dict[str, Any], ScriptedChatModel, _Search]:
    model = ScriptedChatModel(structured_script=[] if draft is None else [draft])
    searcher = search or _Search()
    graph = build_progress_graph(
        checkpointer=None,
        model=model,
        search=searcher,
        fetch=_fetch,
        preflight=_preflight,
    )
    state = await graph.graph.ainvoke(
        {
            "user_id": "local",
            "mistakes": mistakes,
            "profile_block": "",
            "messages": [],
        },
        _config(),
    )
    return state, model, searcher


async def test_valid_refs_are_kept_and_citations_come_from_search() -> None:
    state, _model, searcher = await _run(
        NextStepDraft(
            topic="list",
            reason="列表边界处理还不稳",
            exercise_ids=["ex_1", "ex_2"],
            next_steps=["重做 ex_1", "阅读列表文档"],
        ),
        mistakes=[_mistake("ex_1"), _mistake("ex_2", created_at="2026-09-30T12:00:00+00:00")],
    )

    assert state["exercise_ids"] == ["ex_1", "ex_2"]
    assert state["topic"] == "list"
    assert "依据：" in state["reason"]
    assert "- 你在 ex_1（list）上未通过" in state["reason"]
    assert [citation.chunk_id for citation in state["citations"]] == ["c0", "c1"]
    assert [citation.url for citation in state["citations"]] == [
        f"{_DOC_URL}#lists",
        f"{_DOC_URL}#lists",
    ]
    assert searcher.queries == [("Python 列表 list 常用操作", 5)]
    assert state["usage"].prompt_tokens == 40


async def test_invalid_refs_are_stripped() -> None:
    state, _model, _search = await _run(
        NextStepDraft(
            topic="list",
            reason="先练列表",
            exercise_ids=["ex_1", "ex_made_up"],
            next_steps=["重做 ex_1"],
        ),
        mistakes=[_mistake("ex_1")],
    )

    assert state["exercise_ids"] == ["ex_1"]
    assert "ex_made_up" not in state["reason"]


async def test_all_invalid_refs_fall_back_to_most_recent_candidate() -> None:
    state, _model, _search = await _run(
        NextStepDraft(
            topic="list",
            reason="先练列表",
            exercise_ids=["ex_nope"],
            next_steps=["重做一题"],
        ),
        mistakes=[
            _mistake("ex_new", created_at="2026-10-02T12:00:00+00:00"),
            _mistake("ex_old", created_at="2026-10-01T12:00:00+00:00"),
        ],
    )

    assert state["exercise_ids"] == ["ex_new"]
    assert "- 你在 ex_new（list）上未通过" in state["reason"]


async def test_empty_candidates_never_call_the_model() -> None:
    state, model, searcher = await _run(None, mistakes=[])

    assert model.structured_requests == []
    assert searcher.queries == []
    assert state["exercise_ids"] == []
    assert state["citations"] == []
    assert state["reason"] == "你还没有未解决的错题"
    assert state["topic"] in EXERCISE_TOPICS


async def test_topic_outside_candidates_falls_back_to_cited_exercise_topic() -> None:
    state, _model, _search = await _run(
        NextStepDraft(
            topic="async",
            reason="练异步",
            exercise_ids=["ex_dict"],
            next_steps=["重做 ex_dict"],
        ),
        mistakes=[_mistake("ex_dict", topic="dict")],
    )

    assert state["topic"] == "dict"
    assert "- 你在 ex_dict（dict）上未通过" in state["reason"]


async def test_duplicate_candidates_are_deduped() -> None:
    state, _model, searcher = await _run(
        NextStepDraft(
            topic="list",
            reason="先练列表",
            exercise_ids=["ex_1"],
            next_steps=["重做 ex_1"],
        ),
        mistakes=[_mistake("ex_1"), _mistake("ex_1")],
    )

    assert state["exercise_ids"] == ["ex_1"]
    assert len(searcher.queries) == 1


async def test_empty_search_still_returns_a_recommendation() -> None:
    state, _model, _search = await _run(
        NextStepDraft(
            topic="list",
            reason="先练列表",
            exercise_ids=["ex_1"],
            next_steps=["重做 ex_1"],
        ),
        mistakes=[_mistake("ex_1")],
        search=_Search(chunks=[]),
    )

    assert state["citations"] == []
    assert state["exercise_ids"] == ["ex_1"]


@pytest.mark.parametrize("topic", EXERCISE_TOPICS)
def test_every_topic_has_a_deterministic_query(topic: str) -> None:
    assert _topic_query(topic) == _topic_query(topic)
    assert topic in _topic_query(topic)
