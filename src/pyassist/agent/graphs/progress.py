"""v4 推荐图（SPEC §5.7 / D7）：收集候选 → 结构化推荐 → 校验与引用。

三条纪律：

1. **候选只从状态里的 `mistakes` 来**（服务层从数据库读出），客户端传不进来。
2. **引用 id 的严格校验在这里**：模型不知道哪些 id 真实存在，剥掉非法 id 再用
   最近一条候选兜底 —— 推荐不能是空话（D7 第 1/2 条）。
3. **文档链接只由 `build_citations` 生成**：模型产不出 URL，`NextStepDraft` 里也没有
   这个字段。检索为空不失败，只是这次推荐不带链接（推荐本身仍然成立）。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from pyassist.agent.graphs.qa import _resolve_deps
from pyassist.agent.state import MistakeRecord, ProgressState
from pyassist.agent.tools.docs import Fetcher, Searcher
from pyassist.agent.tools.progress import recommend_next
from pyassist.config import (
    DEFAULT_TOP_K,
    EXERCISE_TOPICS,
    PROGRESS_MODEL_TIER,
)
from pyassist.llm.client import get_chat_model
from pyassist.llm.tracing import UsageSnapshot, add_usage, model_id_of
from pyassist.logging import get_logger
from pyassist.rag.citations import build_citations

logger = get_logger(__name__)

_ZERO_USAGE: Final = UsageSnapshot(
    prompt_tokens=0,
    completion_tokens=0,
    cached_tokens=0,
    cost_usd=Decimal(0),
    is_off_peak=True,
)

_TOPIC_QUERIES: Final[dict[str, str]] = {
    "list": "Python 列表 list 常用操作",
    "dict": "Python 字典 dict 常用操作",
    "decorator": "Python 装饰器 decorator functools.wraps",
    "async": "Python asyncio 异步协程 await",
}
"""检索 query 是**确定性**的：同一主题永远同一个串（可复现、可缓存，D7）。"""


@dataclass(frozen=True, slots=True)
class ProgressGraph:
    """编译好的推荐图 + 与它同源的检索预检（与 `QAGraph` 同构）。"""

    graph: CompiledStateGraph[Any, Any, Any, Any]
    model_id: str
    preflight: Callable[[], Awaitable[None]]


def build_progress_graph(
    *,
    checkpointer: BaseCheckpointSaver[str] | None,
    model: BaseChatModel | None = None,
    search: Searcher | None = None,
    fetch: Fetcher | None = None,
    preflight: Callable[[], Awaitable[None]] | None = None,
) -> ProgressGraph:
    """拼图并编译。检索依赖必须**成套**注入（`_resolve_deps` 在构图期校验）。"""
    searcher, _fetcher, preflight_fn = _resolve_deps(search, fetch, preflight)
    chat_model = model if model is not None else get_chat_model(tier=PROGRESS_MODEL_TIER)
    model_id = model_id_of(chat_model)

    async def collect(state: ProgressState) -> dict[str, Any]:
        candidates = _dedupe(state.get("mistakes") or [])
        logger.info(
            "progress.collect",
            user_id=state.get("user_id"),
            candidate_count=len(candidates),
        )
        return {
            "mistakes": candidates,
            "chunks": [],
            "citations": [],
            "usage": _ZERO_USAGE,
            "tool_rounds": 0,
            "answer": "",
            "messages": [],
            "topic": None,
            "reason": "",
            "exercise_ids": [],
            "next_steps": [],
        }

    async def recommend(state: ProgressState) -> dict[str, Any]:
        started = time.perf_counter()
        draft, usage = await recommend_next(
            user_id=state["user_id"],
            mistakes=state["mistakes"],
            profile_block=state.get("profile_block", ""),
            model=chat_model,
        )
        logger.info(
            "progress.recommend",
            user_id=state["user_id"],
            topic=draft.topic,
            draft_ids=len(draft.exercise_ids),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return {
            "topic": draft.topic,
            "reason": draft.reason,
            "exercise_ids": draft.exercise_ids,
            "next_steps": draft.next_steps,
            "usage": add_usage(state["usage"], usage),
        }

    async def finalize(state: ProgressState) -> dict[str, Any]:
        candidates = state["mistakes"]
        if not candidates:
            # 生产路径由 `services/progress.py` 在进图前短路（§5.9）；这里只是让
            # 空状态下的图仍然自洽，不调模型、不检索。
            topic = state.get("topic") or EXERCISE_TOPICS[0]
            reason = state.get("reason") or "你还没有未解决的错题"
            return {"topic": topic, "reason": reason, "answer": reason}

        by_id = {
            mistake.exercise_id: mistake
            for mistake in candidates
            if mistake.exercise_id is not None
        }
        raw_ids = list(state.get("exercise_ids") or [])
        valid = _dedupe_ids([item for item in raw_ids if item in by_id])
        invalid = [item for item in raw_ids if item not in by_id]
        if invalid:
            logger.warning(
                "progress.invalid_refs",
                user_id=state["user_id"],
                invalid_ids=invalid,
            )
        if not valid:
            # D7 第 2 条：剥完为空就用候选集里最近一条补上（候选按时间倒序）。
            fallback = candidates[0].exercise_id
            if fallback is not None:
                valid = [fallback]

        topic = _resolve_topic(state.get("topic"), valid, by_id)
        reason = _append_evidence(state.get("reason", ""), valid, by_id)

        chunks = await searcher(_topic_query(topic), DEFAULT_TOP_K)
        if not chunks:
            logger.info("progress.retrieve_empty", user_id=state["user_id"], topic=topic)
        citations = build_citations(chunks)
        logger.info(
            "progress.finalize",
            user_id=state["user_id"],
            topic=topic,
            cited_ids=valid,
            citations=len(citations),
        )
        return {
            "topic": topic,
            "reason": reason,
            "exercise_ids": valid,
            "citations": list(citations),
            "chunks": list(chunks),
            "answer": reason,
        }

    builder = StateGraph(ProgressState)
    builder.add_node("collect", collect)
    builder.add_node("recommend", recommend)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "collect")
    builder.add_conditional_edges(
        "collect",
        _has_candidates,
        {"recommend": "recommend", "finalize": "finalize"},
    )
    builder.add_edge("recommend", "finalize")
    builder.add_edge("finalize", END)

    compiled = builder.compile(checkpointer=checkpointer)
    return ProgressGraph(graph=compiled, model_id=model_id, preflight=preflight_fn)


def _has_candidates(state: ProgressState) -> Literal["recommend", "finalize"]:
    return "recommend" if state.get("mistakes") else "finalize"


def _dedupe(mistakes: list[MistakeRecord]) -> list[MistakeRecord]:
    """按 `exercise_id` 去重，保留 DB 给出的时间倒序；没有 id 的错题无法被引用，跳过。"""
    seen: set[str] = set()
    candidates: list[MistakeRecord] = []
    for mistake in mistakes:
        if mistake.exercise_id is None or mistake.exercise_id in seen:
            continue
        seen.add(mistake.exercise_id)
        candidates.append(mistake)
    return candidates


def _dedupe_ids(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in ids:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _resolve_topic(
    draft_topic: str | None, valid_ids: list[str], by_id: dict[str, MistakeRecord]
) -> str:
    """`topic` 必须在候选主题里（prompt 的硬要求）；模型跑偏时用首条引用的主题兜底。"""
    candidate_topics = {mistake.topic for mistake in by_id.values()}
    if draft_topic and draft_topic in candidate_topics:
        return draft_topic
    if valid_ids:
        return by_id[valid_ids[0]].topic
    return EXERCISE_TOPICS[0]


def _append_evidence(reason: str, valid_ids: list[str], by_id: dict[str, MistakeRecord]) -> str:
    """确定性"依据"块（D7 第 4 条）。模型写的理由在前，机器核对的证据在后。"""
    if not valid_ids:
        return reason.strip()
    lines = [
        f"- 你在 {exercise_id}（{by_id[exercise_id].topic}）上未通过" for exercise_id in valid_ids
    ]
    block = "依据：\n" + "\n".join(lines)
    head = reason.strip()
    return f"{head}\n\n{block}" if head else block


def _topic_query(topic: str) -> str:
    return _TOPIC_QUERIES.get(topic, f"Python {topic}")


__all__ = ["ProgressGraph", "build_progress_graph"]
