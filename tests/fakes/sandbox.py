"""沙箱替身（SPEC §5.15 / D2）。

**绝不执行任何代码**：`run` 只按脚本弹出一条 `SandboxResult`。真实隔离由
`tests/test_sandbox_docker.py`（`-m docker`）与 `sandbox/runner.py` 的契约测试覆盖。

`result_for` 的 `idempotency_key` 用生产同款 `sandbox_key()` 生成 —— 图里的缓存键
必须与真实 runner 的键一致，否则 `verify` 第二次查缓存永远 miss，
"同一段代码只跑一次"在测试里就是假的。
"""

from __future__ import annotations

from collections.abc import Sequence

from pyassist.sandbox.runner import SandboxResult, SandboxStatus, sandbox_key


class FakeSandboxRunner:
    """按脚本返回结果的 runner。脚本耗尽即失败 —— 静默返回成功会掩盖多余的执行。"""

    def __init__(self, script: Sequence[SandboxResult] | None = None) -> None:
        self.script: list[SandboxResult] = list(script or [])
        self.calls: list[tuple[str, str, int | None]] = []

    async def run(
        self, source: str, *, stdin: str = "", timeout_seconds: int | None = None
    ) -> SandboxResult:
        self.calls.append((source, stdin, timeout_seconds))
        if not self.script:
            raise AssertionError("FakeSandboxRunner 脚本已耗尽：用例没有为这次执行准备结果")
        return self.script.pop(0)


def result_for(
    source: str,
    *,
    status: SandboxStatus = "ok",
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    reason: str | None = None,
) -> SandboxResult:
    """按生产同款键构造一条结果，供图与工具测试使用。"""
    if exit_code is None:
        exit_code = 0 if status == "ok" else (1 if status == "error" else None)
    return SandboxResult(
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
        is_truncated=False,
        stdout_lines=len(stdout.splitlines()),
        stderr_lines=len(stderr.splitlines()),
        rejected_reason=reason,
        idempotency_key=sandbox_key(source),
    )
