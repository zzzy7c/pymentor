"""v3 的两张图（SPEC §5.6）。

离线：`ScriptedChatModel` 的结构化队列给草稿/批改，`FakeSandboxRunner` 只回放
`SandboxResult`，检索用两条固定片段。真正被断言的是**图的控制流**：
自校验不过会不会重出题、达到上限会不会抛、批改是否只拿测试报告说话。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fakes.llm import ScriptedChatModel
from fakes.sandbox import FakeSandboxRunner, result_for
from pyassist.agent.graphs.exercise import build_exercise_graph
from pyassist.agent.state import ExerciseSpec, FeedbackIssue
from pyassist.agent.state import TestCaseSpec as CaseSpec
from pyassist.agent.tools.grading import ExerciseDraft, GradeDraft
from pyassist.agent.tools.grading import TestCaseDraft as DraftTest
from pyassist.config import MAX_EXERCISE_ATTEMPTS, TEST_RESULT_MARKER
from pyassist.errors import ConfigError, LLMBadResponse
from pyassist.rag.retriever import RetrievedChunk

_DOC_URL = "https://docs.python.org/3.12/tutorial/datastructures.html"


def _draft(title: str = "列表求和", solution: str | None = None) -> ExerciseDraft:
    return ExerciseDraft(
        title=title,
        prompt="实现 add",
        starter_code="def add(left, right):\n    ...\n",
        reference_solution=solution or "def add(left, right):\n    return left + right\n",
        tests=[
            DraftTest(
                name="test_add",
                source="def test_add(self):\n    self.assertEqual(add(1, 2), 3)",
            )
        ],
    )


def _report_stdout(*, passed: int, failed: int = 0, total: int | None = None) -> str:
    records: list[dict[str, Any]] = [
        {"name": f"test_passed_{index}", "status": "passed", "message": "", "duration_ms": 1}
        for index in range(passed)
    ]
    records.extend(
        {"name": f"test_failed_{index}", "status": "failed", "message": "boom", "duration_ms": 1}
        for index in range(failed)
    )
    return TEST_RESULT_MARKER + json.dumps(
        {
            "total": total if total is not None else passed + failed,
            "records": records,
            "duration_ms": 2,
        }
    )


def _exercise() -> ExerciseSpec:
    return ExerciseSpec(
        exercise_id="ex-1",
        topic="list",
        title="列表求和",
        prompt="实现 add",
        starter_code="def add(left, right):\n    ...\n",
        reference_solution="def add(left, right):\n    return left + right\n",
        tests=[
            CaseSpec(
                name="test_add",
                source="def test_add(self):\n    self.assertEqual(add(1, 2), 3)",
            )
        ],
        self_check_attempts=1,
    )


def _config(thread_id: str = "ex-1") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def test_generation_passes_on_first_self_check() -> None:
    model = ScriptedChatModel(structured_script=[_draft()])
    runner = FakeSandboxRunner([result_for("", stdout=_report_stdout(passed=2))])
    graphs = build_exercise_graph(checkpointer=None, model=model, runner=runner)

    state = await graphs.generation.ainvoke({"topic": "list"}, config=_config("ex-1"))

    assert state["exercise"].exercise_id == "ex-1"
    assert state["exercise"].title == "列表求和"
    assert state["generation_attempts"] == 1
    assert state["self_check_report"].is_complete is True
    assert state["error_message"] is None
    assert len(state["sandbox_results"]) == 1
    assert state["usage"].prompt_tokens == 40
    assert len(runner.calls) == 1


async def test_generation_retries_after_failed_self_check() -> None:
    # 两次草稿的答案必须**不同**，否则同一段代码 + 同一组测试会命中沙箱缓存
    model = ScriptedChatModel(
        structured_script=[
            _draft("坏题", solution="def add(left, right):\n    return left\n"),
            _draft("好题", solution="def add(left, right):\n    return left + right\n"),
        ]
    )
    runner = FakeSandboxRunner(
        [
            result_for("", stdout=_report_stdout(passed=0, failed=1)),
            result_for("", stdout=_report_stdout(passed=1)),
        ]
    )
    graphs = build_exercise_graph(checkpointer=None, model=model, runner=runner)

    state = await graphs.generation.ainvoke({"topic": "dict"}, config=_config())

    assert state["generation_attempts"] == 2
    assert state["exercise"].title == "好题"
    assert len(runner.calls) == 2
    # 第二次生成必须看到第一次的失败摘要，否则重试只是重新掷骰子
    second_request = model.structured_requests[1]
    assert "上一次自校验未通过" in str(second_request[-1].content)


async def test_generation_gives_up_after_max_attempts() -> None:
    model = ScriptedChatModel(
        structured_script=[
            _draft(
                f"坏题{index}",
                solution=f"def add(left, right):\n    return left + {index}\n",
            )
            for index in range(MAX_EXERCISE_ATTEMPTS)
        ]
    )
    runner = FakeSandboxRunner(
        [result_for("", stdout=_report_stdout(passed=0, failed=1))] * MAX_EXERCISE_ATTEMPTS
    )
    graphs = build_exercise_graph(checkpointer=None, model=model, runner=runner)

    with pytest.raises(LLMBadResponse) as caught:
        await graphs.generation.ainvoke({"topic": "list"}, config=_config())

    assert caught.value.details["stage"] == "self_check"
    assert len(runner.calls) == MAX_EXERCISE_ATTEMPTS


async def test_generation_requires_thread_id() -> None:
    model = ScriptedChatModel(structured_script=[_draft()])
    graphs = build_exercise_graph(checkpointer=None, model=model, runner=FakeSandboxRunner([]))

    with pytest.raises(ConfigError):
        await graphs.generation.ainvoke({"topic": "list"}, config={"configurable": {}})


async def test_build_requires_runner() -> None:
    with pytest.raises(ConfigError):
        build_exercise_graph(checkpointer=None)


def _chunks(prefix: str) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=f"{prefix}#{index}",
            text="列表是可变序列。",
            doc_url=_DOC_URL,
            source_url=f"{_DOC_URL}#lists",
            title="Data Structures",
            section="More on Lists",
            score=0.9 - index / 100,
        )
        for index in range(2)
    ]


async def test_grading_runs_tests_then_grades_without_letting_model_score() -> None:
    model = ScriptedChatModel(
        structured_script=[
            GradeDraft(
                summary="边界用例失败",
                issues=[FeedbackIssue(where="test_add", why="返回值少了 1", hint="检查加法实现")],
                doc_queries=["list slicing", "unittest assertEqual", "third ignored"],
            )
        ]
    )
    runner = FakeSandboxRunner([result_for("", stdout=_report_stdout(passed=1, failed=1))])
    queries: list[tuple[str, int]] = []

    async def search(query: str, top_k: int) -> list[RetrievedChunk]:
        queries.append((query, top_k))
        return _chunks(f"q{len(queries)}")

    graphs = build_exercise_graph(checkpointer=None, model=model, runner=runner, search=search)

    state = await graphs.grading.ainvoke(
        {"exercise": _exercise(), "submitted_code": "def add(a, b):\n    return a\n"},
        config=_config("submission-1"),
    )

    assert state["report"].is_complete is True
    assert (state["report"].passed, state["report"].failed) == (1, 1)
    assert state["grade_summary"] == "边界用例失败"
    assert state["issues"][0].hint == "检查加法实现"
    # 模型给的 3 条 query 只用前 2 条（GRADE_MAX_DOC_QUERIES）
    assert [query for query, _ in queries] == ["list slicing", "unittest assertEqual"]
    assert all(top_k == 3 for _, top_k in queries)
    assert len(state["citations"]) == 4
    assert "**修改建议**" in state["answer"]
    assert "**来源**" in state["answer"]
    assert "score" not in state


async def test_grading_records_incomplete_report_as_failed_feedback() -> None:
    model = ScriptedChatModel(
        structured_script=[GradeDraft(summary="代码在顶层就崩了", doc_queries=[])]
    )
    runner = FakeSandboxRunner([result_for("", status="error", stderr="Terminated")])
    graphs = build_exercise_graph(
        checkpointer=None, model=model, runner=runner, search=_unused_search
    )

    state = await graphs.grading.ainvoke(
        {"exercise": _exercise(), "submitted_code": "raise SystemExit(1)"},
        config=_config("submission-2"),
    )

    assert state["report"].is_complete is False
    assert state["report"].total == 0
    assert state["citations"] == []
    assert state["last_sandbox_result"].status == "error"
    # 报告不完整也仍然给反馈，但绝不出现"通过"字样
    assert state["grade_summary"] == "代码在顶层就崩了"


async def test_grading_requires_exercise_and_code() -> None:
    model = ScriptedChatModel()
    graphs = build_exercise_graph(
        checkpointer=None,
        model=model,
        runner=FakeSandboxRunner([]),
        search=_unused_search,
    )

    with pytest.raises(LLMBadResponse):
        await graphs.grading.ainvoke(
            {"exercise": None, "submitted_code": "x = 1"}, config=_config("s")
        )


async def _unused_search(query: str, top_k: int) -> list[RetrievedChunk]:
    raise AssertionError(f"这条用例不该检索：{query!r} / {top_k}")
