"""v3 的判分材料：测试脚本拼装、结果解析、分数公式、两个模型入口（SPEC §5.3）。

**纯函数在前，模型调用在后。** 前四个函数不碰网络、不碰 docker，测试可以离线覆盖；
`generate_exercise` / `grade_code` 只负责结构化输出与用量记账。

三条不可协商的规则：

1. **分数确定性**：`is_passed` / `score_of` 只看 `TestReport`；`GradeDraft` 里
   **没有分数字段**（D5）。模型说"很好"不算通过。
2. **测试结果协议只有一个**：标记来自 `config.TEST_RESULT_MARKER`，只在本文件与
   测试里出现（守护 #41）；标记放在 stdout 末尾，`truncate_output` 保留尾行。
3. **测试只在沙箱里跑**：本文件只把测试拼成字符串，执行仍走 `run_tests` →
   `sandbox/runner.py`（§4 禁止在 src 内 exec/eval）。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from functools import cache
from typing import Annotated

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import InjectedToolArg
from pydantic import BaseModel, Field

from pyassist.agent.state import ExerciseSpec, FeedbackIssue, TestOutcome, TestReport
from pyassist.config import (
    EXERCISE_CONTEXT_PROMPT_NAME,
    EXERCISE_PROMPT_NAME,
    GRADE_CONTEXT_PROMPT_NAME,
    GRADE_MAX_DOC_QUERIES,
    GRADE_PROMPT_NAME,
    MAX_EXERCISE_PROMPT_CHARS,
    MAX_REFERENCE_SOLUTION_CHARS,
    MAX_STARTER_CODE_CHARS,
    MAX_TEST_METHOD_CHARS,
    MAX_TESTS_PER_EXERCISE,
    OUTPUT_LANGUAGE,
    TEST_RESULT_MARKER,
)
from pyassist.errors import ConfigError, LLMBadResponse
from pyassist.llm.client import get_structured_output
from pyassist.llm.prompts import PROMPT_DIR, load_prompt
from pyassist.llm.tracing import UsageSnapshot, extract_usage, log_llm_response, model_id_of
from pyassist.logging import get_logger
from pyassist.sandbox.runner import SandboxResult

logger = get_logger(__name__)

_METHOD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_MESSAGE_CHARS = 4_000

_TEST_SECTION_HEADER = "class ExerciseTests(unittest.TestCase):"

# **逐字模板**（SPEC §5.3）。退出码恒为 0：跑完了与过了是两件事，由报告表达后者。
_RESULT_FOOTER = """if __name__ == "__main__":
    import json as _json
    import time as _time
    import unittest as _unittest

    class _PyassistResult(_unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.records = []
            self._started = {}

        def startTest(self, test):
            self._started[test.id()] = _time.perf_counter()
            super().startTest(test)

        def _record(self, test, status, message):
            started = self._started.pop(test.id(), None)
            elapsed = 0 if started is None else int((_time.perf_counter() - started) * 1000)
            self.records.append({
                "name": test.id().split(".")[-1],
                "status": status,
                "message": str(message)[-4000:],
                "duration_ms": elapsed,
            })

        def addSuccess(self, test):
            super().addSuccess(test)
            self._record(test, "passed", "")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._record(test, "failed", self._exc_info_to_string(err, test))

        def addError(self, test, err):
            super().addError(test, err)
            self._record(test, "error", self._exc_info_to_string(err, test))

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._record(test, "skipped", reason)

    _suite = _unittest.defaultTestLoader.loadTestsFromTestCase(ExerciseTests)
    _result = _PyassistResult()
    _started = _time.perf_counter()
    _suite.run(_result)
    _payload = {
        "total": _result.testsRun,
        "records": _result.records,
        "duration_ms": int((_time.perf_counter() - _started) * 1000),
    }
    print("__PYASSIST_RESULT__" + _json.dumps(_payload, ensure_ascii=False))"""


class TestCaseDraft(BaseModel):
    """生成草稿里的一条测试。字段与 `TestCaseSpec` 一一对应。"""

    name: str
    source: str


class ExerciseDraft(BaseModel):
    """`generate_exercise` 的结构化输出。**没有 exercise_id**：id 由服务层生成。"""

    title: str
    prompt: str
    starter_code: str
    reference_solution: str
    tests: list[TestCaseDraft]


class GradeDraft(BaseModel):
    """`grade_code` 的结构化输出。**没有分数字段**（SPEC D5）。"""

    summary: str
    issues: list[FeedbackIssue] = Field(default_factory=list)
    doc_queries: list[str] = Field(default_factory=list)


def compose_test_script(solution: str, tests_source: str) -> str:
    """用户代码 + 测试方法 + 结果脚注 → **一个** `main.py`（D4 / S1）。

    沙箱只写入一个文件、且 `python -I` 不加载同目录模块，所以必须单文件拼接。
    测试方法顶层没有 `class`，这里统一缩进进 `ExerciseTests`。
    """
    indented = "\n".join(
        f"    {line}" if line.strip() else "" for line in tests_source.strip().splitlines()
    )
    return (
        f"{solution.rstrip()}\n\n"
        f"# ---- tests ----\n"
        f"import unittest\n\n"
        f"{_TEST_SECTION_HEADER}\n"
        f"{indented}\n\n\n"
        f"{_RESULT_FOOTER}\n"
    )


def parse_test_report(result: SandboxResult) -> TestReport:
    """从沙箱结果解析测试报告（D4）。**任何不完整都落到 `is_complete=False`。**"""
    if result.status != "ok":
        return _empty_report()

    marker_line = _last_marker_line(result.stdout)
    if marker_line is None:
        return _empty_report()

    try:
        payload = json.loads(marker_line)
    except (json.JSONDecodeError, TypeError):
        return _empty_report()
    if not isinstance(payload, dict):
        return _empty_report()

    records = payload.get("records")
    if not isinstance(records, list):
        return _empty_report()

    outcomes: list[TestOutcome] = []
    counts: dict[str, int] = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("name")
        status = record.get("status")
        if not isinstance(name, str) or status not in counts:
            continue
        message = record.get("message")
        duration = record.get("duration_ms")
        outcomes.append(
            TestOutcome(
                name=name,
                status=status,
                message=message if isinstance(message, str) else "",
                duration_ms=duration if isinstance(duration, int) else 0,
            )
        )
        counts[status] += 1

    total = payload.get("total")
    duration_ms = payload.get("duration_ms")
    return TestReport(
        total=total if isinstance(total, int) else len(outcomes),
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["error"],
        skipped=counts["skipped"],
        outcomes=outcomes,
        duration_ms=duration_ms if isinstance(duration_ms, int) else result.duration_ms,
        is_complete=True,
    )


def score_of(report: TestReport) -> int:
    """通过率取整（D5）。空报告 = 0 分，不做除零。"""
    if report.total <= 0:
        return 0
    return round(100 * report.passed / report.total)


def is_passed(report: TestReport) -> bool:
    """**唯一**的通过与判定（D5）。`is_complete=False` 永远不通过。"""
    return (
        report.is_complete
        and report.total > 0
        and report.passed == report.total
        and report.failed == 0
        and report.errors == 0
    )


def render_feedback_block(issues: Sequence[FeedbackIssue]) -> str:
    """确定性渲染反馈块。空列表返回 `""`（前端据此不渲染空壳）。"""
    if not issues:
        return ""
    lines = ["**修改建议**"]
    for position, issue in enumerate(issues, start=1):
        where = issue.where.strip() or "（未指明位置）"
        why = issue.why.strip()
        hint = issue.hint.strip()
        lines.append(f"{position}. `{where}`：{why} → {hint}")
    return "\n".join(lines)


def failed_outcomes(report: TestReport) -> list[TestOutcome]:
    """失败/错误的用例，按原顺序。给模型与渲染用（通过的用例不必塞进 prompt）。"""
    return [outcome for outcome in report.outcomes if outcome.status in {"failed", "error"}]


async def generate_exercise(
    topic: str,
    *,
    attempt_feedback: str = "",
    model: Annotated[BaseChatModel | None, InjectedToolArg] = None,
) -> tuple[ExerciseDraft, UsageSnapshot]:
    """生成一道题的草稿。返回 `(草稿, 用量)`；**id 不在这里生成**。"""
    if model is None:
        # 构图/注入错误，模型不可能触发；抛 ConfigError 而不是让 AttributeError 变 500。
        raise ConfigError("generate_exercise 缺少模型注入")

    model_id = model_id_of(model)
    structured = get_structured_output(model, ExerciseDraft)
    messages = _generation_messages(topic, attempt_feedback)

    started = time.perf_counter()
    result = await structured.ainvoke(messages)
    draft, usage = _unwrap_structured(result, model_id=model_id, stage="generate")
    if not isinstance(draft, ExerciseDraft):
        raise LLMBadResponse("生成结果不是合法的结构化输出", details={"stage": "generate"})
    log_llm_response(usage, model_id=model_id, latency_ms=_elapsed_ms(started))
    validated = _validate_draft(draft)
    return validated, usage


async def grade_code(
    code: str,
    *,
    exercise: Annotated[ExerciseSpec | None, InjectedToolArg] = None,
    report: Annotated[TestReport | None, InjectedToolArg] = None,
    model: Annotated[BaseChatModel | None, InjectedToolArg] = None,
) -> tuple[GradeDraft, UsageSnapshot]:
    """根据测试报告生成反馈。模型**不产出分数**（D5）。"""
    if exercise is None or report is None or model is None:
        raise ConfigError("grade_code 缺少 exercise / report / model 注入")

    model_id = model_id_of(model)
    structured = get_structured_output(model, GradeDraft)
    messages = _grading_messages(code, exercise, report)

    started = time.perf_counter()
    result = await structured.ainvoke(messages)
    draft, usage = _unwrap_structured(result, model_id=model_id, stage="grade")
    if not isinstance(draft, GradeDraft):
        raise LLMBadResponse("批改结果不是合法的结构化输出", details={"stage": "grade"})
    log_llm_response(usage, model_id=model_id, latency_ms=_elapsed_ms(started))
    return _validate_grade(draft), usage


def _last_marker_line(stdout: str) -> str | None:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if candidate.startswith(TEST_RESULT_MARKER):
            return candidate[len(TEST_RESULT_MARKER) :]
    return None


def _empty_report() -> TestReport:
    return TestReport(
        total=0,
        passed=0,
        failed=0,
        errors=0,
        skipped=0,
        outcomes=[],
        duration_ms=0,
        is_complete=False,
    )


def _validate_draft(draft: ExerciseDraft) -> ExerciseDraft:
    title = draft.title.strip()
    prompt = draft.prompt.strip()
    starter = draft.starter_code.strip()
    solution = draft.reference_solution.strip()
    if not title or not prompt or not solution:
        raise LLMBadResponse("练习草稿缺少必填内容", details={"stage": "generate"})
    if len(prompt) > MAX_EXERCISE_PROMPT_CHARS or len(starter) > MAX_STARTER_CODE_CHARS:
        raise LLMBadResponse("练习草稿超出长度上限", details={"stage": "generate"})
    if len(solution) > MAX_REFERENCE_SOLUTION_CHARS:
        raise LLMBadResponse("参考答案超出长度上限", details={"stage": "generate"})
    if not 1 <= len(draft.tests) <= MAX_TESTS_PER_EXERCISE:
        raise LLMBadResponse(
            "测试条数不在允许范围内",
            details={"stage": "generate", "test_count": len(draft.tests)},
        )

    tests: list[TestCaseDraft] = []
    for test in draft.tests:
        name = test.name.strip()
        source = test.source.strip()
        if not _METHOD_NAME_RE.match(name):
            raise LLMBadResponse("测试方法名不合法", details={"stage": "generate"})
        if not source or len(source) > MAX_TEST_METHOD_CHARS:
            raise LLMBadResponse("测试方法源码为空或超长", details={"stage": "generate"})
        tests.append(TestCaseDraft(name=name, source=source))

    return ExerciseDraft(
        title=title,
        prompt=prompt,
        starter_code=starter,
        reference_solution=solution,
        tests=tests,
    )


def _validate_grade(draft: GradeDraft) -> GradeDraft:
    summary = draft.summary.strip()
    if not summary:
        raise LLMBadResponse("批改结果缺少 summary", details={"stage": "grade"})
    queries = [query.strip() for query in draft.doc_queries if query.strip()]
    return GradeDraft(
        summary=summary,
        issues=list(draft.issues)[:MAX_TESTS_PER_EXERCISE],
        doc_queries=queries[:GRADE_MAX_DOC_QUERIES],
    )


def _unwrap_structured(
    result: object, *, model_id: str, stage: str
) -> tuple[object, UsageSnapshot]:
    """`include_raw=True` 的结果 → `(parsed, usage)`（S5）。"""
    if not isinstance(result, dict):
        raise LLMBadResponse("结构化输出返回了非 dict 对象", details={"stage": stage})
    raw = result.get("raw")
    parsed = result.get("parsed")
    usage = extract_usage(raw, model_id=model_id) if isinstance(raw, AIMessage) else _zero_usage()
    if parsed is None:
        raise LLMBadResponse(
            "结构化输出解析失败",
            details={"stage": stage, "parsing_error": str(result.get("parsing_error"))},
        )
    return parsed, usage


def _zero_usage() -> UsageSnapshot:
    from decimal import Decimal

    return UsageSnapshot(
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=0,
        cost_usd=Decimal(0),
        is_off_peak=True,
    )


def _system_message(prompt_name: str) -> BaseMessage:
    rendered = load_prompt(prompt_name).format_messages(history=[], output_language=OUTPUT_LANGUAGE)
    return rendered[0]


@cache
def _exercise_context_template() -> ChatPromptTemplate:
    return _human_template(EXERCISE_CONTEXT_PROMPT_NAME)


@cache
def _grade_context_template() -> ChatPromptTemplate:
    return _human_template(GRADE_CONTEXT_PROMPT_NAME)


def _human_template(name: str) -> ChatPromptTemplate:
    """`*_context.jinja2` 的独立加载器。**不走 `load_prompt()`**（同 #37）。"""
    path = PROMPT_DIR / f"{name}.jinja2"
    return ChatPromptTemplate.from_messages(
        [("human", path.read_text(encoding="utf-8"))], template_format="jinja2"
    )


def _generation_messages(topic: str, attempt_feedback: str) -> list[BaseMessage]:
    return [
        _system_message(EXERCISE_PROMPT_NAME),
        *_exercise_context_template().format_messages(
            topic=topic, attempt_feedback=attempt_feedback
        ),
    ]


def _grading_messages(code: str, exercise: ExerciseSpec, report: TestReport) -> list[BaseMessage]:
    failures = "\n\n".join(
        f"- {outcome.name} ({outcome.status})：{outcome.message}"
        for outcome in failed_outcomes(report)
    )
    return [
        _system_message(GRADE_PROMPT_NAME),
        *_grade_context_template().format_messages(
            title=exercise.title,
            topic=exercise.topic,
            prompt=exercise.prompt,
            code=code,
            report_total=report.total,
            report_passed=report.passed,
            report_failed=report.failed,
            report_errors=report.errors,
            report_skipped=report.skipped,
            failures=failures[:_MAX_MESSAGE_CHARS],
        ),
    ]


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "ExerciseDraft",
    "GradeDraft",
    "TestCaseDraft",
    "compose_test_script",
    "failed_outcomes",
    "generate_exercise",
    "grade_code",
    "is_passed",
    "parse_test_report",
    "render_feedback_block",
    "score_of",
]
