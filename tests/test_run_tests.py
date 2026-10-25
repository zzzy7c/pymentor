"""`run_tests`（SPEC §5.4）的工具级契约。

不碰 Docker：`FakeSandboxRunner` 只回放结果，真正断言的是三件事：
拼进去的脚本包含题目自带的用例、报告来自 `parse_test_report`、缓存键与 `run_code` 同源。
"""

from __future__ import annotations

import pytest

from fakes.sandbox import FakeSandboxRunner, result_for
from pyassist.agent.state import ExerciseSpec
from pyassist.agent.state import TestCaseSpec as CaseSpec
from pyassist.agent.tools.grading import compose_test_script
from pyassist.agent.tools.sandbox_tools import run_tests
from pyassist.config import TEST_RESULT_MARKER
from pyassist.errors import ConfigError
from pyassist.sandbox.runner import sandbox_key

SOLUTION = "def add(left, right):\n    return left + right\n"

TESTS_SOURCE = (
    "def test_add(self):\n"
    "    self.assertEqual(add(1, 2), 3)\n\n"
    "def test_add_negative(self):\n"
    "    self.assertEqual(add(-1, -2), -3)\n"
)


def _exercise() -> ExerciseSpec:
    return ExerciseSpec(
        exercise_id="exercise-1",
        topic="list",
        title="加法",
        prompt="实现 add",
        starter_code="def add(left, right):\n    ...\n",
        reference_solution=SOLUTION,
        tests=[CaseSpec(name="test_add", source=TESTS_SOURCE)],
        self_check_attempts=0,
    )


def _stdout() -> str:
    import json

    return TEST_RESULT_MARKER + json.dumps(
        {
            "total": 2,
            "records": [
                {"name": "test_add", "status": "passed", "message": "", "duration_ms": 1},
                {"name": "test_add_negative", "status": "passed", "message": "", "duration_ms": 1},
            ],
            "duration_ms": 2,
        }
    )


async def test_run_tests_composes_and_parses() -> None:
    runner = FakeSandboxRunner([result_for(SOLUTION, stdout=_stdout())])

    text, report = await run_tests(SOLUTION, exercise=_exercise(), runner=runner)

    assert report.is_complete is True
    assert (report.total, report.passed) == (2, 2)
    assert "total=2 passed=2" in text
    assert len(runner.calls) == 1
    source, stdin, timeout = runner.calls[0]
    assert SOLUTION in source
    assert source == compose_test_script(SOLUTION, TESTS_SOURCE)
    assert TEST_RESULT_MARKER in source
    assert stdin == ""
    assert timeout is None  # 不新增超时旋钮，沿用 SANDBOX_LIMITS


async def test_run_tests_reuses_cache() -> None:
    # 缓存键由**拼装后的整份脚本**决定，与 runner 真实执行时用的键同源
    composed = compose_test_script(SOLUTION, TESTS_SOURCE)
    cache = {sandbox_key(composed): result_for(composed, stdout=_stdout())}

    runner = FakeSandboxRunner()  # 脚本为空：若真的执行会 AssertionError
    text, report = await run_tests(SOLUTION, exercise=_exercise(), runner=runner, cache=cache)

    assert report.total == 2
    assert "total=2" in text
    assert runner.calls == []


async def test_run_tests_reports_unparseable_execution() -> None:
    runner = FakeSandboxRunner(
        [result_for("print(1)", status="error", stdout="", stderr="NameError: name 'x'")]
    )

    text, report = await run_tests("print(1)", exercise=_exercise(), runner=runner)

    assert report.is_complete is False
    assert "未能解析测试结果" in text
    assert "NameError" in text


async def test_run_tests_requires_injection() -> None:
    with pytest.raises(ConfigError):
        await run_tests("x = 1", exercise=None, runner=FakeSandboxRunner())
    with pytest.raises(ConfigError):
        await run_tests("x = 1", exercise=_exercise(), runner=None)
