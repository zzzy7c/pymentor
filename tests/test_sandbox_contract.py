"""沙箱契约（SPEC §5.17）：纯函数、缓存、容器参数、拒绝线。

这些都**不碰真实 Docker**：记录型 fake client 只验证"我们传了什么"。
真实隔离（无网络 / 只读 / 非 root / pids / 恶意代码）在 `test_sandbox_docker.py`，
用 `-m docker` 单独跑 —— 默认 `pytest` 不需要 daemon。
"""

from __future__ import annotations

import socket
from typing import Any

import docker
import pytest

from fakes.sandbox import FakeSandboxRunner, result_for
from pyassist.agent.tools.sandbox_tools import execute_cached, run_code
from pyassist.config import SandboxLimits
from pyassist.errors import ConfigError, SandboxError
from pyassist.sandbox.runner import (
    _BOOTSTRAP_SOURCE,
    DockerSandboxRunner,
    SandboxResult,
    sandbox_key,
    truncate_output,
)

# ---------- truncate_output（D3）----------


def test_truncate_output_keeps_head_and_tail_lines() -> None:
    text = "\n".join(f"line-{index}" for index in range(300))
    truncated, is_truncated, total_lines = truncate_output(
        text, head_lines=2, tail_lines=2, max_chars=10_000
    )

    assert is_truncated is True
    assert total_lines == 300
    lines = truncated.splitlines()
    assert lines[0] == "line-0"
    assert lines[1] == "line-1"
    assert "已省略 296 行" in lines[2]
    assert lines[-2] == "line-298"
    assert lines[-1] == "line-299"


def test_truncate_output_under_limit_is_unchanged() -> None:
    text = "a\nb\nc"
    truncated, is_truncated, total_lines = truncate_output(
        text, head_lines=5, tail_lines=5, max_chars=10_000
    )
    assert truncated == text
    assert is_truncated is False
    assert total_lines == 3


def test_truncate_output_enforces_char_cap() -> None:
    text = "x" * 5_000
    truncated, is_truncated, total_lines = truncate_output(
        text, head_lines=100, tail_lines=100, max_chars=100
    )
    assert len(truncated) <= 100
    assert "已截断" in truncated
    assert is_truncated is True
    assert total_lines == 1


# ---------- sandbox_key（D4）----------


def test_sandbox_key_is_stable_and_sensitive() -> None:
    base = sandbox_key("print(1)")
    assert base == sandbox_key("print(1)")
    assert base != sandbox_key("print(2)")
    assert base != sandbox_key("print(1)", stdin="input")
    assert base != sandbox_key("print(1)", limits=SandboxLimits(timeout_seconds=11))


# ---------- execute_cached ----------


async def test_execute_cached_returns_hit_without_calling_runner() -> None:
    cached = result_for("print(1)", stdout="1\n")
    runner = FakeSandboxRunner()  # 脚本为空：一旦被调用就会 AssertionError
    result = await execute_cached(
        "print(1)",
        runner=runner,
        cache={cached.idempotency_key: cached},
    )
    assert result is cached
    assert runner.calls == []


async def test_run_code_requires_injected_runner() -> None:
    with pytest.raises(ConfigError):
        await run_code("print(1)")


# ---------- 拒绝线：不创建容器 ----------


async def test_empty_code_is_rejected_without_container() -> None:
    client = _FakeClient(_FakeContainer())
    runner = DockerSandboxRunner(client=client)  # type: ignore[arg-type]

    result = await runner.run("   \n")

    assert isinstance(result, SandboxResult)
    assert result.status == "rejected"
    assert result.rejected_reason == "代码为空"
    assert result.exit_code is None
    assert client.containers.created == []


async def test_oversized_code_is_rejected_without_container() -> None:
    client = _FakeClient(_FakeContainer())
    runner = DockerSandboxRunner(
        client=client,  # type: ignore[arg-type]
        limits=SandboxLimits(max_code_bytes=16),
    )

    result = await runner.run("x = 1\n" * 10)

    assert result.status == "rejected"
    assert result.rejected_reason is not None
    assert "16" in result.rejected_reason
    assert client.containers.created == []


# ---------- 容器参数表（SPEC §5.2）----------


async def test_runner_passes_every_isolation_parameter() -> None:
    container = _FakeContainer(exit_code=0, stdout=b"hello\n", stderr=b"")
    client = _FakeClient(container)
    runner = DockerSandboxRunner(client=client)  # type: ignore[arg-type]

    result = await runner.run("print('hello')")

    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.idempotency_key == sandbox_key("print('hello')")

    kwargs = client.containers.created[0]
    assert kwargs["image"] == "pyassist-sandbox:1"
    assert kwargs["command"] == ["python", "-I", "-u", "-c", _BOOTSTRAP_SOURCE]
    assert kwargs["working_dir"] == "/work"
    assert kwargs["stdin_open"] is True
    assert kwargs["network_disabled"] is True
    assert kwargs["read_only"] is True
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["memswap_limit"] == "512m"
    assert kwargs["nano_cpus"] == 1_000_000_000
    assert kwargs["pids_limit"] == 64
    assert kwargs["user"] == "10001:10001"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=8m"}  # noqa: S108 - 容器内路径
    assert kwargs["environment"]["HOME"] == "/tmp"  # noqa: S108 - 容器内路径
    assert kwargs["labels"] == {"pyassist.role": "sandbox"}
    # 代码**不经宿主路径**进容器（ADR 0004 决定 2 第 3 条）：只读根文件系统下
    # put_archive 会被 daemon 直接拒绝，挂载宿主目录又依赖 api 与 daemon 同机。
    assert "volumes" not in kwargs

    # 顺序：attach → start → 送代码 → …… → remove（stdin 通道必须先于 start 建立）
    assert container.events[:3] == ["attach", "start", "send"]
    assert container.events[-1] == "remove"
    assert container.stream.is_write_closed is True
    assert container.stream.payload == b"14\nprint('hello')"


async def test_nonzero_exit_is_a_result_not_an_exception() -> None:
    container = _FakeContainer(exit_code=1, stdout=b"", stderr=b"boom\n")
    runner = DockerSandboxRunner(client=_FakeClient(container))  # type: ignore[arg-type]

    result = await runner.run("raise SystemExit(1)")

    assert result.status == "error"
    assert result.exit_code == 1
    assert "boom" in result.stderr


async def test_timeout_kills_container_and_returns_timeout() -> None:
    container = _FakeContainer(exit_code=None, finish=False)
    runner = DockerSandboxRunner(client=_FakeClient(container))  # type: ignore[arg-type]

    result = await runner.run("while True: pass", timeout_seconds=0)

    assert result.status == "timeout"
    assert result.exit_code is None
    assert "kill" in container.events
    assert container.events[-1] == "remove"


async def test_docker_failure_raises_sandbox_error() -> None:
    client = _FakeClient(_FakeContainer(), create_error=docker.errors.APIError("boom"))
    runner = DockerSandboxRunner(client=client)  # type: ignore[arg-type]

    with pytest.raises(SandboxError):
        await runner.run("print(1)")


async def test_source_and_stdin_share_one_stdin_stream() -> None:
    """协议 = `<源码字节数>\\n` + 源码 + 程序 stdin。长度头让二者不必靠分隔符区分。"""
    container = _FakeContainer(exit_code=0, stdout=b"", stderr=b"")
    runner = DockerSandboxRunner(client=_FakeClient(container))  # type: ignore[arg-type]

    await runner.run("print(input())", stdin="42\n")

    assert container.stream.payload == b"14\nprint(input())42\n"
    assert container.stream.is_write_closed is True


def test_bootstrap_source_compiles() -> None:
    """引导脚本是 `python -c` 的实参。写成语法错误的话，容器会以一个
    "用户代码报错"的形态退出（stderr 里是 `<string>` 的 SyntaxError），
    容易被误判成用户的问题 —— 所以离线先编译一遍。"""
    compile(_BOOTSTRAP_SOURCE, "<bootstrap>", "exec")


async def test_stdin_write_failure_is_a_sandbox_error() -> None:
    """代码送不进去 = 沙箱坏了（服务端错误），不能当成用户代码的 `status="error"`。"""
    container = _FakeContainer(send_error=BrokenPipeError(32, "Broken pipe"))
    runner = DockerSandboxRunner(client=_FakeClient(container))  # type: ignore[arg-type]

    with pytest.raises(SandboxError):
        await runner.run("print(1)")
    assert container.events[-1] == "remove"


# ---------- fake client ----------


class _FakeStream:
    def __init__(self, container: _FakeContainer, *, send_error: OSError | None = None) -> None:
        self.payload = b""
        self.is_write_closed = False
        self.timeout: float | None = None
        self._container = container
        self._send_error = send_error

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def sendall(self, data: bytes) -> None:
        if self._send_error is not None:
            raise self._send_error
        self._container.events.append("send")
        self.payload += data

    def shutdown(self, how: int) -> None:
        assert how == socket.SHUT_WR
        self.is_write_closed = True
        self._container.events.append("shutdown")

    def close(self) -> None:
        self._container.events.append("close")


class _FakeContainer:
    def __init__(
        self,
        *,
        exit_code: int | None = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        finish: bool = True,
        send_error: OSError | None = None,
    ) -> None:
        self.attrs: dict[str, Any] = {"State": {"Status": "running", "ExitCode": None}}
        self.events: list[str] = []
        self.stream = _FakeStream(self, send_error=send_error)
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._finish = finish

    def attach_socket(self, params: dict[str, Any]) -> _FakeStream:
        assert params == {"stdin": 1, "stream": 1}
        self.events.append("attach")
        return self.stream

    def start(self) -> None:
        self.events.append("start")

    def reload(self) -> None:
        self.events.append("reload")
        if self._finish:
            self.attrs["State"] = {"Status": "exited", "ExitCode": self._exit_code}

    def kill(self) -> None:
        self.events.append("kill")
        self.attrs["State"]["Status"] = "exited"

    def logs(self, *, stdout: bool = False, stderr: bool = False) -> bytes:
        return self._stdout if stdout else self._stderr

    def remove(self, *, force: bool = False) -> None:
        assert force is True
        self.events.append("remove")


class _FakeContainers:
    def __init__(self, container: _FakeContainer, create_error: Exception | None) -> None:
        self.created: list[dict[str, Any]] = []
        self._container = container
        self._create_error = create_error

    def create(self, **kwargs: Any) -> _FakeContainer:
        if self._create_error is not None:
            raise self._create_error
        self.created.append(kwargs)
        return self._container


class _FakeClient:
    def __init__(self, container: _FakeContainer, create_error: Exception | None = None) -> None:
        self.containers = _FakeContainers(container, create_error)
