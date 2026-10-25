"""SPEC §5.3 的契约测试：拼装、解析、分数、反馈块与两个模型入口。

前三组是纯函数，不需要任何 fake；模型入口用 `ScriptedChatModel` 的
`structured_script`，全程离线。**拼装结果真的被执行一次**（`sys.executable -I`），
因为结果协议的价值全在"跑出来的 stdout 能不能被 parse 回来"——只比对字符串
会让脚注里的缩进错误、`print` 被吞掉这类问题漏到线上。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from fakes.llm import ScriptedChatModel
from pyassist.agent.state import ExerciseSpec, FeedbackIssue
from pyassist.agent.state import TestReport as Report
from pyassist.agent.tools import grading
from pyassist.agent.tools.grading import (
    ExerciseDraft,
    GradeDraft,
    compose_test_script,
    generate_exercise,
    grade_code,
    is_passed,
    parse_test_report,
    render_feedback_block,
    score_of,
)
from pyassist.config import TEST_RESULT_MARKER
from pyassist.errors import ConfigError, LLMBadResponse
from pyassist.sandbox.runner import SandboxResult

SOLUTION = """\
def add(left, right):
    return left + right
"""

TESTS = """\
def test_positive(self):
    self.assertEqual(add(1, 2), 3)

def test_negative(self):
    self.assertEqual(add(-1, -3), -4)

def test_deliberately_failing(self):
    self.assertEqual(add(1, 1), 3)
"""


def _sandbox_result(
    stdout: str, *, status: Literal["ok", "error", "timeout", "rejected"] = "ok"
) -> SandboxResult:
    return SandboxResult(
        status=status,
        exit_code=0 if status == "ok" else None,
        stdout=stdout,
        stderr="",
        duration_ms=12,
        is_truncated=False,
        stdout_lines=len(stdout.splitlines()),
        stderr_lines=0,
    )


def _report_payload(
    *,
    total: int,
    records: list[dict[str, object]],
    duration_ms: int = 7,
) -> str:
    return TEST_RESULT_MARKER + json.dumps(
        {"total": total, "records": records, "duration_ms": duration_ms},
        ensure_ascii=False,
    )


def _exercise() -> ExerciseSpec:
    return ExerciseSpec(
        exercise_id="exercise-1",
        topic="list",
        title="列表求和",
        prompt="实现 add",
        starter_code="def add(left, right):\n    ...\n",
        reference_solution=SOLUTION,
        tests=[],
        self_check_attempts=0,
    )


# ---------- compose_test_script ----------


def test_compose_puts_solution_first_and_indents_tests() -> None:
    script = compose_test_script(SOLUTION, TESTS)

    assert script.startswith(SOLUTION.rstrip())
    assert "\nclass ExerciseTests(unittest.TestCase):\n" in script
    assert "\n    def test_positive(self):\n        self.assertEqual(add(1, 2), 3)\n" in script
    assert TEST_RESULT_MARKER in script
    assert script.rstrip().endswith("ensure_ascii=False))")


def test_compose_rejects_nothing_but_preserves_blank_lines() -> None:
    script = compose_test_script(
        SOLUTION, "def test_a(self):\n    pass\n\ndef test_b(self):\n    pass\n"
    )
    # 空行不补缩进，否则方法之间会出现 4 空格的行（无害但会破坏逐字模板的可读性）
    assert "\n\n    def test_b(self):\n" in script


def test_composed_script_actually_reports(tmp_path: Path) -> None:
    """真正跑一次：3 条用例，2 通过 1 失败，标记在 stdout 末尾。"""
    script_path = tmp_path / "main.py"
    script_path.write_text(compose_test_script(SOLUTION, TESTS), encoding="utf-8")

    # 被测脚本是本文件里的静态夹具，不是用户输入；`-I` 还隔离了环境与同目录模块。
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    last_line = completed.stdout.strip().splitlines()[-1]
    assert last_line.startswith(TEST_RESULT_MARKER)

    report = parse_test_report(_sandbox_result(completed.stdout, status="ok"))
    assert report.is_complete is True
    assert (report.total, report.passed, report.failed, report.errors) == (3, 2, 1, 0)
    # unittest 按方法名字典序执行，不按源码顺序
    statuses = {outcome.name: outcome.status for outcome in report.outcomes}
    assert statuses == {
        "test_deliberately_failing": "failed",
        "test_negative": "passed",
        "test_positive": "passed",
    }
    assert [outcome.name for outcome in report.outcomes][-1] == "test_positive"
    failing = next(o for o in report.outcomes if o.name == "test_deliberately_failing")
    assert "AssertionError" in failing.message  # traceback 被保留
    assert score_of(report) == 67
    assert is_passed(report) is False


# ---------- parse_test_report ----------


def test_parse_report_counts_and_preserves_messages() -> None:
    stdout = "\n".join(
        [
            "noise before",
            _report_payload(
                total=4,
                records=[
                    {"name": "test_a", "status": "passed", "message": "", "duration_ms": 1},
                    {"name": "test_b", "status": "failed", "message": "boom", "duration_ms": 2},
                    {"name": "test_c", "status": "error", "message": "kaboom", "duration_ms": 3},
                    {"name": "test_d", "status": "skipped", "message": "later", "duration_ms": 0},
                ],
            ),
        ]
    )

    report = parse_test_report(_sandbox_result(stdout))

    assert report.is_complete is True
    assert (report.total, report.passed, report.failed, report.errors, report.skipped) == (
        4,
        1,
        1,
        1,
        1,
    )
    assert report.duration_ms == 7
    assert report.outcomes[1].message == "boom"


@pytest.mark.parametrize(
    ("stdout", "status"),
    [
        ("", "ok"),
        ("no marker here", "ok"),
        (TEST_RESULT_MARKER + "{not json", "ok"),
        (TEST_RESULT_MARKER + '{"records": "nope"}', "ok"),
        (_report_payload(total=1, records=[]), "timeout"),
        (_report_payload(total=1, records=[]), "error"),
        (_report_payload(total=1, records=[]), "rejected"),
    ],
)
def test_parse_report_incomplete_is_never_passed(
    stdout: str, status: Literal["ok", "error", "timeout", "rejected"]
) -> None:
    report = parse_test_report(_sandbox_result(stdout, status=status))

    assert report.is_complete is False
    assert report.total == 0
    assert report.passed == 0
    assert is_passed(report) is False
    assert score_of(report) == 0


def test_parse_report_ignores_unknown_record_status() -> None:
    stdout = _report_payload(
        total=2,
        records=[
            {"name": "test_a", "status": "passed", "message": "", "duration_ms": 1},
            {"name": "test_b", "status": "mystery", "message": "", "duration_ms": 0},
        ],
    )

    report = parse_test_report(_sandbox_result(stdout))

    assert report.is_complete is True
    assert len(report.outcomes) == 1
    assert report.total == 2  # 以沙箱报告为准，不从 outcomes 反推
    assert is_passed(report) is False


# ---------- score / pass ----------


def test_score_and_pass_rules() -> None:
    complete = Report(
        total=4,
        passed=4,
        failed=0,
        errors=0,
        skipped=0,
        outcomes=[],
        duration_ms=1,
        is_complete=True,
    )
    skipped = Report(
        total=4,
        passed=3,
        failed=0,
        errors=0,
        skipped=1,
        outcomes=[],
        duration_ms=1,
        is_complete=True,
    )

    assert score_of(complete) == 100
    assert is_passed(complete) is True
    assert score_of(skipped) == 75
    # 跳过的用例不算通过：通过的条数必须等于总数（D5）
    assert is_passed(skipped) is False


def test_is_passed_requires_complete_even_when_counts_look_green() -> None:
    dishonest = Report(
        total=2,
        passed=2,
        failed=0,
        errors=0,
        skipped=0,
        outcomes=[],
        duration_ms=1,
        is_complete=False,
    )
    assert is_passed(dishonest) is False


# ---------- render_feedback_block ----------


def test_render_feedback_is_empty_for_no_issues() -> None:
    assert render_feedback_block([]) == ""


def test_render_feedback_is_deterministic_and_numbered() -> None:
    issues = [
        FeedbackIssue(where="test_a", why="边界没处理", hint="加上空列表判断"),
        FeedbackIssue(where="", why="返回值类型错", hint="返回 list"),
    ]

    rendered = render_feedback_block(issues)

    assert rendered == (
        "**修改建议**\n"
        "1. `test_a`：边界没处理 → 加上空列表判断\n"
        "2. `（未指明位置）`：返回值类型错 → 返回 list"
    )
    assert render_feedback_block(issues) == rendered


# ---------- generate_exercise / grade_code ----------


async def test_generate_exercise_returns_draft_and_usage() -> None:
    draft = ExerciseDraft(
        title="  反转列表  ",
        prompt="写一个函数反转列表",
        starter_code="def reverse(values):\n    ...\n",
        reference_solution="def reverse(values):\n    return values[::-1]\n",
        tests=[
            grading.TestCaseDraft(
                name="test_empty",
                source="def test_empty(self):\n    self.assertEqual(reverse([]), [])",
            )
        ],
    )
    model = ScriptedChatModel(structured_script=[draft])

    result, usage = await generate_exercise("list", model=model)

    assert result.title == "反转列表"  # 首尾空白被收紧
    assert result.tests[0].name == "test_empty"
    assert usage.prompt_tokens == 40
    assert usage.completion_tokens == 20
    assert usage.cost_usd > Decimal(0)
    # prompt 的渲染顺序固定：system 在最前，且只有一个 system
    request = model.structured_requests[0]
    assert type(request[0]).__name__ == "SystemMessage"
    assert sum(1 for message in request if type(message).__name__ == "SystemMessage") == 1


async def test_generate_exercise_rejects_illegal_method_name() -> None:
    draft = ExerciseDraft(
        title="题目",
        prompt="描述",
        starter_code="",
        reference_solution="x = 1\n",
        tests=[grading.TestCaseDraft(name="test with space", source="def test(self): pass")],
    )
    model = ScriptedChatModel(structured_script=[draft])

    with pytest.raises(LLMBadResponse) as caught:
        await generate_exercise("list", model=model)

    assert caught.value.details == {"stage": "generate"}


async def test_generate_exercise_rejects_empty_test_list() -> None:
    draft = ExerciseDraft(
        title="题目",
        prompt="描述",
        starter_code="",
        reference_solution="x = 1\n",
        tests=[],
    )
    model = ScriptedChatModel(structured_script=[draft])

    with pytest.raises(LLMBadResponse):
        await generate_exercise("dict", model=model)


async def test_generate_exercise_rejects_blank_reference_solution() -> None:
    draft = ExerciseDraft(
        title="题目",
        prompt="描述",
        starter_code="",
        reference_solution="   ",
        tests=[grading.TestCaseDraft(name="test_a", source="def test_a(self): pass")],
    )
    model = ScriptedChatModel(structured_script=[draft])

    with pytest.raises(LLMBadResponse):
        await generate_exercise("decorator", model=model)


async def test_grade_code_has_no_score_and_trims_doc_queries() -> None:
    draft = GradeDraft(
        summary="有一条用例失败",
        issues=[FeedbackIssue(where="test_failing", why="预期 3 得到 2", hint="检查返回值")],
        doc_queries=["  list append  ", "", "unittest assertEqual", "third"],
    )
    model = ScriptedChatModel(structured_script=[draft])
    report = Report(
        total=3,
        passed=2,
        failed=1,
        errors=0,
        skipped=0,
        outcomes=[],
        duration_ms=5,
        is_complete=True,
    )

    result, usage = await grade_code(
        "def add(a, b):\n    return a - b\n",
        exercise=_exercise(),
        report=report,
        model=model,
    )

    assert result.summary == "有一条用例失败"
    assert result.doc_queries == ["list append", "unittest assertEqual"]
    assert not hasattr(result, "score")
    assert usage.prompt_tokens == 40


async def test_grade_code_requires_injection() -> None:
    model = ScriptedChatModel()
    report = Report(
        total=1,
        passed=1,
        failed=0,
        errors=0,
        skipped=0,
        outcomes=[],
        duration_ms=1,
        is_complete=True,
    )

    with pytest.raises(ConfigError):
        await grade_code("x = 1", exercise=None, report=report, model=model)
