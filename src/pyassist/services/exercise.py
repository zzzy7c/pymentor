"""v3 业务流程：出题、提交、落库、错题翻转、记忆更新（SPEC §5.8）。

图的节点**没有持久副作用**；所有写库都在这里发生，且都带固定公式的幂等键：
提交 `attempt:{exercise_id}:{sha256(code)[:16]}`、错题 `mistake:{user_id}:{exercise_id}`。
重放（同一份代码再交一次）会重算结果，但不会多出一行记录。

分数与通过判定**只来自测试报告**（`score_of` / `is_passed`），本文件不参与判断。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.agent.graphs.exercise import ExerciseGraphs
from pyassist.agent.state import (
    AttemptRecord,
    ExerciseSpec,
    FeedbackIssue,
    MistakeRecord,
    TestReport,
)
from pyassist.agent.tools.grading import is_passed, score_of
from pyassist.config import ERROR_SIGNATURE_MAX_CHARS, MISTAKE_LIST_LIMIT
from pyassist.db import repositories
from pyassist.errors import ExerciseNotFound, LLMBadResponse
from pyassist.llm.tracing import UsageSnapshot
from pyassist.logging import get_logger
from pyassist.rag.citations import Citation
from pyassist.services.memory import update_profile
from pyassist.services.usage import db_session_id

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExerciseResult:
    exercise: ExerciseSpec
    usage: UsageSnapshot


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    exercise_id: str
    report: TestReport
    score: int
    is_passed: bool
    summary: str
    issues: list[FeedbackIssue]
    citations: list[Citation]
    usage: UsageSnapshot
    # 路由需要把原始输出回给用户（SPEC §5.12 SubmissionData）。这里直接带上，
    # 避免路由为了三个字段去读一次业务表。
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


async def create_exercise(
    *,
    topic: str,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    exercise_id: str,
    request_id: str,
) -> ExerciseResult:
    """跑生成图（含自校验重试）。生成图**不写库**。"""
    started = time.perf_counter()
    config: RunnableConfig = {"configurable": {"thread_id": exercise_id}}
    state = await graph.ainvoke({"topic": topic}, config)
    exercise = state.get("exercise")
    if not isinstance(exercise, ExerciseSpec):
        raise LLMBadResponse("生成图没有产出题目", details={"stage": "generate"})
    usage = state.get("usage")
    if not isinstance(usage, UsageSnapshot):
        usage = _zero_usage()
    logger.info(
        "exercise.created",
        request_id=request_id,
        exercise_id=exercise_id,
        topic=topic,
        attempts=int(state.get("generation_attempts", 0)),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return ExerciseResult(exercise=exercise, usage=usage)


async def submit_exercise(
    *,
    exercise_id: str,
    code: str,
    graphs: ExerciseGraphs,
    session: AsyncSession,
    user_id: str,
    session_id: str,
    request_id: str,
    store: BaseStore | None = None,
) -> SubmissionResult:
    """跑批改图并把业务记录落库。顺序按 SPEC §5.8 固定，不重排。"""
    started = time.perf_counter()
    normalized_session_id = db_session_id(session_id)
    await repositories.ensure_user(session, user_id=user_id)
    await repositories.create_chat_session(
        session, session_id=normalized_session_id, user_id=user_id
    )
    # 建档先落盘，**不要带着打开写事务去跑图**：检查点走另一条连接写同一个
    # SQLite 文件（§2），业务事务不提交就会一直占着写锁，图那一侧等到超时
    # 直接 `database is locked` —— 而两边都在同一个协程里，谁也等不到谁。
    await repositories.commit(session)

    snapshot = await graphs.generation.aget_state({"configurable": {"thread_id": exercise_id}})
    exercise = snapshot.values.get("exercise")
    if not isinstance(exercise, ExerciseSpec):
        raise ExerciseNotFound(exercise_id)

    submission_thread = f"{exercise_id}:{uuid.uuid4().hex}"
    state = await graphs.grading.ainvoke(
        {"exercise": exercise, "submitted_code": code},
        {"configurable": {"thread_id": submission_thread}},
    )
    report = state.get("report")
    if not isinstance(report, TestReport):
        raise LLMBadResponse("批改图没有产出测试报告", details={"stage": "run_tests"})

    passed = is_passed(report)
    score = score_of(report)
    last = state.get("last_sandbox_result")
    stdout = last.stdout if last is not None else None
    stderr = last.stderr if last is not None else None
    duration_ms = last.duration_ms if last is not None else report.duration_ms
    code_digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]

    await repositories.record_attempt(
        session,
        idempotency_key=f"attempt:{exercise_id}:{code_digest}",
        session_id=normalized_session_id,
        exercise_id=exercise_id,
        topic=exercise.topic,
        submitted_code=code,
        is_passed=passed,
        score=score,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
    )

    if passed:
        await repositories.mark_mistake_resolved(session, user_id=user_id, exercise_id=exercise_id)
    else:
        await repositories.record_mistake(
            session,
            idempotency_key=f"mistake:{user_id}:{exercise_id}",
            user_id=user_id,
            topic=exercise.topic,
            exercise_id=exercise_id,
            error_signature=_error_signature(report),
        )

    await repositories.commit(session)

    if store is not None:
        await _update_memory(session, store, user_id=user_id)

    usage = state.get("usage")
    if not isinstance(usage, UsageSnapshot):
        usage = _zero_usage()
    logger.info(
        "exercise.submitted",
        request_id=request_id,
        exercise_id=exercise_id,
        is_passed=passed,
        score=score,
        passed=report.passed,
        total=report.total,
        code_sha256=code_digest,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return SubmissionResult(
        exercise_id=exercise_id,
        report=report,
        score=score,
        is_passed=passed,
        summary=str(state.get("grade_summary", "")),
        issues=list(state.get("issues", [])),
        citations=list(state.get("citations", [])),
        usage=usage,
        stdout=stdout or "",
        stderr=stderr or "",
        duration_ms=duration_ms,
    )


async def _update_memory(session: AsyncSession, store: BaseStore, *, user_id: str) -> None:
    """记忆是增强：写失败只记日志，绝不改变这次提交的结果（SPEC §5.8 第 8 步）。"""
    try:
        mistakes = await repositories.list_mistakes(
            session, user_id=user_id, only_unresolved=False, limit=MISTAKE_LIST_LIMIT
        )
        attempts, _has_more = await repositories.list_attempts_page(
            session, user_id=user_id, limit=MISTAKE_LIST_LIMIT
        )
        await update_profile(
            store,
            user_id=user_id,
            mistakes=[_to_mistake_record(row) for row in mistakes],
            attempts=[_to_attempt_record(row) for row in attempts],
        )
    except Exception as exc:
        logger.warning("memory.update_failed", user_id=user_id, error=type(exc).__name__)


def _error_signature(report: TestReport) -> str | None:
    for outcome in report.outcomes:
        if outcome.status in {"failed", "error"}:
            message = f"{outcome.name}: {outcome.message}".strip()
            return message[:ERROR_SIGNATURE_MAX_CHARS] or None
    return None


def _to_mistake_record(row: Any) -> MistakeRecord:
    return MistakeRecord(
        id=str(row.id),
        exercise_id=row.exercise_id,
        topic=str(row.topic),
        error_signature=row.error_signature,
        is_resolved=bool(row.is_resolved),
        created_at=row.created_at.isoformat(),
    )


def _to_attempt_record(row: Any) -> AttemptRecord:
    return AttemptRecord(
        exercise_id=str(row.exercise_id),
        topic=str(row.topic),
        is_passed=bool(row.is_passed),
        score=row.score,
        created_at=row.created_at.isoformat(),
    )


def _zero_usage() -> UsageSnapshot:
    from decimal import Decimal

    return UsageSnapshot(
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=0,
        cost_usd=Decimal(0),
        is_off_peak=True,
    )


__all__ = [
    "ExerciseResult",
    "SubmissionResult",
    "create_exercise",
    "submit_exercise",
]
