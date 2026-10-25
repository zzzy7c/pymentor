"""`run_code` / `run_tests` 的**实现**（SPEC §5.4 / §5.5）。

模型可见参数只有 `code`；`runner` 与 `cache` 由 `tools` 节点注入 —— 模型没有能力
指定"在哪个沙箱跑"，也没有能力跳过缓存。未注入 runner 是**构图错误**，
抛 `ConfigError` 而不是让 `AttributeError` 被包成 500。

两个工具共用 `execute_cached`：同一段代码在一个 thread 里只跑一次（D4）。
沙箱执行本身没有持久副作用，所以节点重试是安全的。
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Annotated

from langchain_core.tools import InjectedToolArg

from pyassist.agent.state import ExerciseSpec, TestReport
from pyassist.agent.tools.grading import compose_test_script, parse_test_report
from pyassist.config import SANDBOX_LIMITS
from pyassist.errors import ConfigError
from pyassist.logging import get_logger
from pyassist.sandbox.runner import SandboxResult, SandboxRunner, sandbox_key

logger = get_logger(__name__)


async def execute_cached(
    source: str,
    *,
    runner: SandboxRunner,
    cache: MutableMapping[str, SandboxResult],
    stdin: str = "",
    timeout_seconds: int | None = None,
) -> SandboxResult:
    """先查图状态缓存，未命中才起容器。`verify` 节点也走这里。

    未命中时把结果**写回** cache：调用方（`self_check` / 批改图的 `run_tests` 节点）
    要在工具返回后仍能拿到原始 `SandboxResult`，而工具契约只返回 `(文本, 报告)`。
    """
    key = sandbox_key(source, stdin=stdin)
    hit = cache.get(key)
    if hit is not None:
        logger.info("sandbox.cache_hit", idempotency_key=key)
        return hit
    result = await runner.run(source, stdin=stdin, timeout_seconds=timeout_seconds)
    cache[key] = result
    return result


async def run_code(
    code: str,
    *,
    runner: Annotated[SandboxRunner | None, InjectedToolArg] = None,
    cache: Annotated[MutableMapping[str, SandboxResult] | None, InjectedToolArg] = None,
) -> tuple[str, SandboxResult]:
    """在隔离沙箱里执行一段完整的 Python 代码，返回退出码与标准输出/错误。

    用它验证修复后的代码是否真的能跑通（而不是"看起来对"）。代码必须自包含：
    沙箱无网络、只读根文件系统、有运行时限。用户代码报错会作为结果返回，不是工具故障。
    """
    if runner is None:
        # 构图/注入错误：模型不可能触发这一支，所以它是明确的服务端 bug 信号。
        raise ConfigError("run_code 缺少沙箱注入")
    result = await execute_cached(code, runner=runner, cache=cache if cache is not None else {})
    return _render(result), result


async def run_tests(
    code: str,
    *,
    exercise: Annotated[ExerciseSpec | None, InjectedToolArg] = None,
    runner: Annotated[SandboxRunner | None, InjectedToolArg] = None,
    cache: Annotated[MutableMapping[str, SandboxResult] | None, InjectedToolArg] = None,
) -> tuple[str, TestReport]:
    """在沙箱里跑这道题自带的 `unittest` 用例，返回 `(可读文本, 解析后的报告)`。

    `exercise` 由节点注入：模型既看不到题目里藏着的测试，也不能替换它们 ——
    否则"给自己出题再自己改判"就成了。
    """
    if exercise is None or runner is None:
        raise ConfigError("run_tests 缺少 exercise / runner 注入")

    tests_source = "\n\n".join(test.source for test in exercise.tests)
    script = compose_test_script(code, tests_source)
    result = await execute_cached(script, runner=runner, cache=cache if cache is not None else {})
    report = parse_test_report(result)
    return _render_report(report, result), report


def _render_report(report: TestReport, result: SandboxResult) -> str:
    """给模型看的摘要。**不塞完整 traceback**（SPEC §5.4），并受 `max_output_chars` 约束。"""
    if not report.is_complete:
        text = (
            f"status={result.status} 未能解析测试结果"
            f"（exit_code={result.exit_code}，duration_ms={result.duration_ms}）"
        )
        if result.stderr:
            text += f"\n--- stderr ---\n{result.stderr}"
        return text[: SANDBOX_LIMITS.max_output_chars]

    lines = [
        f"total={report.total} passed={report.passed} failed={report.failed} "
        f"errors={report.errors} skipped={report.skipped} duration_ms={report.duration_ms}"
    ]
    for outcome in report.outcomes:
        if outcome.status in {"failed", "error"}:
            lines.append(f"- {outcome.name} ({outcome.status})")
            if outcome.message:
                lines.append(outcome.message[-800:])
    return "\n".join(lines)[: SANDBOX_LIMITS.max_output_chars]


def _render(result: SandboxResult) -> str:
    """`SandboxResult` → 模型可读文本。字段与 §5.5 的示例逐字一致。"""
    exit_code = "?" if result.exit_code is None else str(result.exit_code)
    lines = [f"status={result.status} exit_code={exit_code} duration_ms={result.duration_ms}"]
    if result.rejected_reason:
        lines.append(f"rejected_reason={result.rejected_reason}")
    lines.append("--- stdout ---")
    lines.append(result.stdout)
    lines.append("--- stderr ---")
    lines.append(result.stderr)
    if result.is_truncated:
        lines.append(f"（输出已截断，完整输出保留头尾各 {SANDBOX_LIMITS.output_head_lines} 行）")
    return "\n".join(lines)
