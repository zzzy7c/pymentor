"""v3 的两张图（SPEC §5.6）：出题自校验，与提交后的跑测试 + 批改。

**图里没有"模型自己宣布通过"的路径。** 通过与否只来自沙箱里的 `unittest` 报告
（`is_passed`），模型只负责出题与解释；`grade_code` 的草稿里根本没有分数字段（D5）。

两张图都用一个 `thread_id`：生成图的是 `exercise_id`（题目身份不另立一套），
批改图的是 `submission:<uuid>`。检查点由调用方注入，节点不做持久化副作用的写入。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from pyassist.agent.state import (
    ExerciseSpec,
    ExerciseState,
    FeedbackIssue,
    GradingState,
    TestCaseSpec,
)
from pyassist.agent.tools.docs import Searcher, _default_search
from pyassist.agent.tools.grading import (
    generate_exercise,
    grade_code,
    is_passed,
    render_feedback_block,
)
from pyassist.agent.tools.sandbox_tools import run_tests
from pyassist.config import (
    EXERCISE_MODEL_TIER,
    GRADE_DOC_TOP_K,
    GRADE_MAX_DOC_QUERIES,
    GRADE_MODEL_TIER,
    MAX_EXERCISE_ATTEMPTS,
)
from pyassist.errors import ConfigError, LLMBadResponse
from pyassist.llm.client import get_chat_model
from pyassist.llm.tracing import UsageSnapshot, add_usage, model_id_of
from pyassist.logging import get_logger
from pyassist.rag.citations import Citation, build_citations, render_reference_block
from pyassist.rag.retriever import RetrievedChunk
from pyassist.sandbox.runner import SandboxResult, SandboxRunner

logger = get_logger(__name__)

_ZERO_USAGE = UsageSnapshot(
    prompt_tokens=0,
    completion_tokens=0,
    cached_tokens=0,
    cost_usd=Decimal(0),
    is_off_peak=True,
)


@dataclass(frozen=True, slots=True)
class ExerciseGraphs:
    """两张编译好的图 + 两侧模型 id（账本计价不能记到另一个模型头上）。"""

    generation: CompiledStateGraph[Any, Any, Any, Any]
    grading: CompiledStateGraph[Any, Any, Any, Any]
    model_id: str
    grading_model_id: str


def build_exercise_graph(
    *,
    checkpointer: BaseCheckpointSaver[str] | None,
    model: BaseChatModel | None = None,
    runner: SandboxRunner | None = None,
    search: Searcher | None = None,
    grading_model: BaseChatModel | None = None,
) -> ExerciseGraphs:
    """拼两张图。**`runner` 必填**：没有沙箱，自校验与批改都无从谈起。

    `search` 只影响批改图的文档引用（生成完全不需要检索）。生产省略即用全局
    检索；测试注入假 searcher，避免为了引用去建向量库。
    """
    if runner is None:
        raise ConfigError("练习图缺少沙箱 runner")

    generation_model = model if model is not None else get_chat_model(tier=EXERCISE_MODEL_TIER)
    if grading_model is None:
        grading_model = model if model is not None else get_chat_model(tier=GRADE_MODEL_TIER)
    searcher = search if search is not None else _default_search
    model_id = model_id_of(generation_model)
    grading_model_id = model_id_of(grading_model)

    async def generate(state: ExerciseState, config: RunnableConfig) -> dict[str, Any]:
        exercise_id = _thread_id(config)
        draft, usage = await generate_exercise(
            state["topic"],
            attempt_feedback=state.get("generation_feedback", ""),
            model=generation_model,
        )
        spec = ExerciseSpec(
            exercise_id=exercise_id,
            topic=state["topic"],
            title=draft.title,
            prompt=draft.prompt,
            starter_code=draft.starter_code,
            reference_solution=draft.reference_solution,
            tests=[TestCaseSpec(name=test.name, source=test.source) for test in draft.tests],
            self_check_attempts=int(state.get("generation_attempts", 0)) + 1,
        )
        return {
            # AppState 的五个键在入口节点一次补齐（SPEC §5.2）
            "chunks": [],
            "citations": [],
            "usage": add_usage(state.get("usage", _ZERO_USAGE), usage),
            "tool_rounds": 0,
            "answer": "",
            "exercise": spec,
            "self_check_report": None,
            "generation_attempts": spec.self_check_attempts,
            "error_message": None,
        }

    async def self_check(state: ExerciseState) -> dict[str, Any]:
        spec = state.get("exercise")
        if spec is None:
            raise LLMBadResponse("自校验时没有题目", details={"stage": "self_check"})
        results = dict(state.get("sandbox_results", {}))
        writer = get_stream_writer()
        writer({"type": "tool", "phase": "start", "name": "run_tests"})
        started = time.perf_counter()
        try:
            text, report = await run_tests(
                spec.reference_solution,
                exercise=spec,
                runner=runner,
                cache=results,
            )
        finally:
            writer({"type": "tool", "phase": "end", "name": "run_tests"})
        logger.info(
            "exercise.self_check",
            exercise_id=spec.exercise_id,
            total=report.total,
            passed=report.passed,
            is_complete=report.is_complete,
            latency_ms=_elapsed_ms(started),
        )
        return {
            "self_check_report": report,
            "sandbox_results": results,
            "generation_feedback": "" if is_passed(report) else _feedback(text),
        }

    async def finalize_generation(state: ExerciseState) -> dict[str, Any]:
        return {"error_message": None}

    generation = StateGraph(ExerciseState)
    generation.add_node("generate", generate)
    generation.add_node("self_check", self_check)
    generation.add_node("finalize", finalize_generation)
    generation.add_edge(START, "generate")
    generation.add_edge("generate", "self_check")
    generation.add_conditional_edges(
        "self_check",
        _next_generation_step,
        {"generate": "generate", "finalize": "finalize"},
    )
    generation.add_edge("finalize", END)
    compiled_generation = generation.compile(checkpointer=checkpointer)

    async def run_tests_node(state: GradingState) -> dict[str, Any]:
        spec = state.get("exercise")
        code = state.get("submitted_code", "")
        if spec is None or not code:
            raise LLMBadResponse("批改缺少题目或代码", details={"stage": "run_tests"})
        results = dict(state.get("sandbox_results", {}))
        writer = get_stream_writer()
        writer({"type": "tool", "phase": "start", "name": "run_tests"})
        started = time.perf_counter()
        try:
            _text, report = await run_tests(code, exercise=spec, runner=runner, cache=results)
        finally:
            writer({"type": "tool", "phase": "end", "name": "run_tests"})
        logger.info(
            "exercise.run_tests",
            exercise_id=spec.exercise_id,
            total=report.total,
            passed=report.passed,
            is_complete=report.is_complete,
            latency_ms=_elapsed_ms(started),
        )
        return {
            "chunks": [],
            "citations": [],
            "usage": state.get("usage", _ZERO_USAGE),
            "tool_rounds": 0,
            "answer": "",
            "report": report,
            "sandbox_results": results,
            "last_sandbox_result": _latest_result(results),
            "doc_queries": [],
            "issues": [],
            "grade_summary": "",
        }

    async def grade(state: GradingState) -> dict[str, Any]:
        spec = state.get("exercise")
        report = state.get("report")
        if spec is None or report is None:
            raise LLMBadResponse("批改缺少题目或测试报告", details={"stage": "grade"})

        draft, usage = await grade_code(
            state["submitted_code"],
            exercise=spec,
            report=report,
            model=grading_model,
        )
        chunks: list[RetrievedChunk] = []
        for query in draft.doc_queries[:GRADE_MAX_DOC_QUERIES]:
            found = await searcher(query, GRADE_DOC_TOP_K)
            chunks.extend(found)
        citations = build_citations(chunks)
        logger.info(
            "exercise.grade",
            exercise_id=spec.exercise_id,
            issue_count=len(draft.issues),
            doc_queries=list(draft.doc_queries),
            doc_ids=[chunk.chunk_id for chunk in chunks],
            citations=len(citations),
        )
        return {
            "chunks": chunks,
            "citations": citations,
            "usage": add_usage(state.get("usage", _ZERO_USAGE), usage),
            "grade_summary": draft.summary,
            "issues": list(draft.issues),
            "doc_queries": list(draft.doc_queries),
        }

    async def finalize_grading(state: GradingState) -> dict[str, Any]:
        return {
            "answer": _render_answer(
                state.get("grade_summary", ""), state.get("issues", []), state["citations"]
            )
        }

    grading = StateGraph(GradingState)
    grading.add_node("run_tests", run_tests_node)
    grading.add_node("grade", grade)
    grading.add_node("finalize", finalize_grading)
    grading.add_edge(START, "run_tests")
    grading.add_edge("run_tests", "grade")
    grading.add_edge("grade", "finalize")
    grading.add_edge("finalize", END)
    compiled_grading = grading.compile(checkpointer=checkpointer)

    return ExerciseGraphs(
        generation=compiled_generation,
        grading=compiled_grading,
        model_id=model_id,
        grading_model_id=grading_model_id,
    )


def _next_generation_step(state: ExerciseState) -> Literal["generate", "finalize"]:
    """自校验的**唯一**分支（D5）。达到上限就抛出，而不是把坏题交给用户。"""
    report = state.get("self_check_report")
    if report is not None and is_passed(report):
        return "finalize"
    if int(state.get("generation_attempts", 0)) < MAX_EXERCISE_ATTEMPTS:
        return "generate"
    raise LLMBadResponse(
        "生成的练习未能通过自校验",
        details={"stage": "self_check", "attempts": int(state.get("generation_attempts", 0))},
    )


def _thread_id(config: RunnableConfig) -> str:
    configurable = config.get("configurable") or {}
    value = configurable.get("thread_id")
    if not isinstance(value, str) or not value:
        raise ConfigError("生成图缺少 thread_id：它同时是 exercise_id")
    return value


def _latest_result(results: dict[str, SandboxResult]) -> SandboxResult | None:
    return next(reversed(results.values()), None) if results else None


def _feedback(text: str) -> str:
    return f"上一次自校验未通过：\n{text}"


def _render_answer(
    summary: str, issues: Sequence[FeedbackIssue], citations: Sequence[Citation]
) -> str:
    parts = [summary.strip()]
    block = render_feedback_block(issues)
    if block:
        parts.append(block)
    references = render_reference_block(citations)
    if references:
        parts.append(references)
    return "\n\n".join(part for part in parts if part)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = ["ExerciseGraphs", "build_exercise_graph"]
