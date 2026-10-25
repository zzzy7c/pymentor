"""v4 业务流程：读错题 → 跑推荐图 → 返回可核对的推荐（SPEC §5.9 / D7）。

两个确定性分支值得单独说：

- **没有未解决错题不是错误**：不进模型、不检索，直接给出"先练最不熟的主题"。
  新用户第一次打开推荐页就该看到这句话，而不是一个 502。
- **候选集只来自数据库**：客户端不传错题，模型也无权编造 id（图层再校验一遍）。

本文件不写 SQL（`repositories` 唯一出口），也不 import fastapi（错误映射在 `api/`）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from langgraph.store.base import BaseStore
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.agent.graphs.progress import ProgressGraph
from pyassist.agent.state import MistakeRecord
from pyassist.config import EXERCISE_TOPICS, MISTAKE_LIST_LIMIT
from pyassist.db import repositories
from pyassist.llm.tracing import UsageSnapshot
from pyassist.logging import get_logger
from pyassist.rag.citations import Citation
from pyassist.services.memory import load_profile, render_profile_block

logger = get_logger(__name__)

_EMPTY_REASON = "你还没有未解决的错题"


@dataclass(frozen=True, slots=True)
class Recommendation:
    topic: str
    reason: str
    exercise_ids: list[str]
    next_steps: list[str]
    citations: list[Citation]
    usage: UsageSnapshot


async def recommend_for_user(
    *,
    user_id: str,
    graph: ProgressGraph,
    store: BaseStore,
    session: AsyncSession,
    request_id: str,
) -> Recommendation:
    """跑一次推荐。所有写库操作都不在这里 —— 推荐是只读业务。"""
    started = time.perf_counter()
    rows = await repositories.list_mistakes(
        session, user_id=user_id, only_unresolved=True, limit=MISTAKE_LIST_LIMIT
    )
    candidates = _to_records(rows)
    # 检查点走另一条连接写同一个 SQLite 文件（§2）；先结束这条只读事务，
    # 别让图那一侧在写检查点时等一个已经没人用的读快照。
    await repositories.commit(session)

    if not candidates:
        counts = await repositories.count_attempts_by_topic(session, user_id=user_id)
        topic = _least_attempted_topic(counts)
        recommendation = Recommendation(
            topic=topic,
            reason=_EMPTY_REASON,
            exercise_ids=[],
            next_steps=[f"先完成一组 {topic} 主题练习，提交后推荐会按你的错题重新计算"],
            citations=[],
            usage=_zero_usage(),
        )
        logger.info(
            "progress.done",
            request_id=request_id,
            user_id=user_id,
            candidate_count=0,
            cited_ids=[],
            latency_ms=_elapsed_ms(started),
        )
        return recommendation

    profile_block = await _profile_block(store, user_id)
    state = await graph.graph.ainvoke(
        {
            "user_id": user_id,
            "mistakes": candidates,
            "profile_block": profile_block,
            "messages": [],
        },
        {"configurable": {"thread_id": f"progress:{user_id}:{request_id}"}},
    )
    recommendation = Recommendation(
        topic=str(state.get("topic") or _least_attempted_topic({})),
        reason=str(state.get("reason") or ""),
        exercise_ids=[str(item) for item in state.get("exercise_ids") or []],
        next_steps=[str(item) for item in state.get("next_steps") or []],
        citations=_as_citations(state.get("citations")),
        usage=_as_usage(state.get("usage")),
    )
    logger.info(
        "progress.done",
        request_id=request_id,
        user_id=user_id,
        candidate_count=len(candidates),
        cited_ids=recommendation.exercise_ids,
        latency_ms=_elapsed_ms(started),
    )
    return recommendation


async def _profile_block(store: BaseStore, user_id: str) -> str:
    """画像只是增强：Store 读失败按"没有画像"处理，不让推荐变 500（D6）。"""
    try:
        profile = await load_profile(store, user_id)
    except Exception as exc:
        logger.warning("memory.load_failed", user_id=user_id, error=type(exc).__name__)
        return ""
    return render_profile_block(profile)


def _to_records(rows: Any) -> list[MistakeRecord]:
    """ORM 行 → 只读投影，并按 `exercise_id` 去重（保留 DB 的时间倒序）。"""
    seen: set[str] = set()
    candidates: list[MistakeRecord] = []
    for row in rows:
        exercise_id = row.exercise_id
        if exercise_id is None or exercise_id in seen:
            continue
        seen.add(exercise_id)
        candidates.append(
            MistakeRecord(
                id=str(row.id),
                exercise_id=str(exercise_id),
                topic=str(row.topic),
                error_signature=row.error_signature,
                is_resolved=bool(row.is_resolved),
                created_at=row.created_at.isoformat(),
            )
        )
    return candidates


def _least_attempted_topic(counts: dict[str, int]) -> str:
    """四个主题里尝试最少的那个；并列时按 `EXERCISE_TOPICS` 的固定顺序。"""

    def key(topic: str) -> tuple[int, int]:
        return counts.get(topic, 0), EXERCISE_TOPICS.index(topic)

    return min(EXERCISE_TOPICS, key=key)


def _as_citations(value: object) -> list[Citation]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Citation)]


def _as_usage(value: object) -> UsageSnapshot:
    return value if isinstance(value, UsageSnapshot) else _zero_usage()


def _zero_usage() -> UsageSnapshot:
    return UsageSnapshot(
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=0,
        cost_usd=Decimal(0),
        is_off_peak=True,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = ["Recommendation", "recommend_for_user"]
