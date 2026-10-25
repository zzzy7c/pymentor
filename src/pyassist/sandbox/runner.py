"""★ 唯一执行用户代码的地方（AGENTS.md §2 / §4）。

本文件是全项目**唯一**允许 import docker、唯一允许创建容器的地方（守护 #34/#35）。
隔离由内核提供（namespace / cgroup / capability），Python 只做编排 ——
`exec()` / `eval()` / `subprocess` 在 `src/` 里都是禁区（§4）。

契约（SPEC D2）：

- **用户代码的失败不抛异常**：非零退出 = `status="error"`，超时 = `"timeout"`，
  进容器前被拒绝 = `"rejected"`。它们都是**实验结果**，由 `/diagnose` 以 200 返回。
- **只有基础设施故障**（daemon 不可达、镜像缺失、创建容器失败）才 `raise SandboxError`，
  经 `api/errors.py` 映射成 500 —— 那才是"服务端坏了"。
- 阻塞的 docker SDK 调用一律 `asyncio.to_thread`，不得阻塞事件循环（AGENTS.md §3.4）。
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import time
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable

import docker
from docker.client import DockerClient
from docker.errors import DockerException

from pyassist.config import (
    SANDBOX_IMAGE,
    SANDBOX_LIMITS,
    SANDBOX_POLL_INTERVAL_SECONDS,
    SandboxLimits,
)
from pyassist.errors import SandboxError
from pyassist.logging import get_logger

logger = get_logger(__name__)

SandboxStatus = Literal["ok", "error", "timeout", "rejected"]

_LINE_OMITTED_MARKER: Final = "……（已省略 {count} 行）……"
_CHAR_TRUNCATED_MARKER: Final = "\n……（已截断）……\n"
_RUNNING_STATES: Final = frozenset({"created", "running", "restarting", "paused"})
_FINISHED_STATES: Final = frozenset({"exited", "dead"})

# 代码怎么进容器（SPEC 阶段七 U2 的实测结论）。
#
# `read_only=True` 的容器**整台**根文件系统只读，daemon 会直接拒绝 `put_archive`：
# `400 Bad Request ("container rootfs is marked read-only")` —— 挂载点也救不了，
# 因为拒绝发生在解析挂载之前（`tmpfs /tmp` 与匿名卷都试过）。
# 宿主机路径挂载同样不行：生产环境 api 本身在容器里，它创建的临时目录在宿主上不存在
# （ADR 0004 决定 2 第 3 条）。
#
# 因此代码经 **stdin 流**送进去：容器先跑这段固定引导脚本，把 payload 落到可写的
# `tmpfs /tmp`，再 `execv` 到真正的 `python -I -u /tmp/main.py`。
# 协议 = `<源码字节数>\n` + 源码 + 剩余字节（当作程序 stdin）。长度头是必须的：
# 用户代码里出现任何分隔符都不奇怪，只有长度前缀不会歧义。
_BOOTSTRAP_SOURCE: Final = """\
import os, sys

length = int(sys.stdin.buffer.readline() or b"0")
source = sys.stdin.buffer.read(length)
rest = sys.stdin.buffer.read()
with open("/tmp/main.py", "wb") as handle:
    handle.write(source)
if rest:
    with open("/tmp/stdin.txt", "wb") as handle:
        handle.write(rest)
    os.dup2(os.open("/tmp/stdin.txt", os.O_RDONLY), 0)
os.execv(sys.executable, [sys.executable, "-I", "-u", "/tmp/main.py"])
"""

# 送代码的 socket 写超时。**与执行预算无关**：`timeout_seconds=0` 是合法入参
# （模型可以传 0，语义是"别跑了"），若拿它当写超时，`sendall` 会立刻失败并变成 500
# `SANDBOX_ERROR`，而正确结果是 `status="timeout"`。65KB 过本机 socket 是瞬时的。
_STDIN_WRITE_TIMEOUT_SECONDS: Final = 5.0


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """一次执行的完整结果。字段名是图状态与 API 契约之间的共同词汇（SPEC D2）。

    `idempotency_key` 由 `sandbox_key()` 生成：图状态里用它做缓存键，
    日志里用它统计"同一段代码被跑了两次"的重复率（§8 R6）。
    """

    status: SandboxStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    is_truncated: bool
    stdout_lines: int
    stderr_lines: int
    rejected_reason: str | None = None
    idempotency_key: str = ""


def truncate_output(
    text: str, *, head_lines: int, tail_lines: int, max_chars: int
) -> tuple[str, bool, int]:
    """返回 `(截断后的文本, 是否截断, 截断前总行数)`。纯函数，可离线测（D3）。

    顺序**不可调换**：先按行（保住"哪一段被省略"的语义），再按字符
    （防止单行超长把检查点撑爆）。`is_truncated` 是两次判断的**或**，
    行数是**截断前**的 —— 调用方要靠它判断"输出到底有多少"。
    """
    lines = text.splitlines()
    total_lines = len(lines)
    is_truncated = False

    if total_lines > head_lines + tail_lines:
        omitted = total_lines - head_lines - tail_lines
        kept = [*lines[:head_lines], _LINE_OMITTED_MARKER.format(count=omitted)]
        if tail_lines > 0:
            kept.extend(lines[-tail_lines:])
        text = "\n".join(kept)
        is_truncated = True

    if len(text) > max_chars:
        # 头尾对半分，并给标记本身留出预算 —— 结果长度**不超过** max_chars。
        budget = max(0, max_chars - len(_CHAR_TRUNCATED_MARKER))
        head_chars = budget // 2
        tail_chars = budget - head_chars
        tail = text[-tail_chars:] if tail_chars > 0 else ""
        text = text[:head_chars] + _CHAR_TRUNCATED_MARKER + tail
        is_truncated = True

    return text, is_truncated, total_lines


def sandbox_key(source: str, *, stdin: str = "", limits: SandboxLimits = SANDBOX_LIMITS) -> str:
    """同一份代码（含 stdin 与限额）→ 同一个键（D4）。

    限额进键是必须的：同一段代码在"10 秒 / 512MB"与"30 秒 / 1GB"下是两次不同的实验，
    共用一个键会让第二次命中第一次的 `timeout`，模型永远看不到它其实能跑完。
    实现里 `\\x00` 分隔，避免 `"ab"+"c"` 与 `"a"+"bc"` 撞键。
    """
    parts = [
        SANDBOX_IMAGE,
        str(limits.timeout_seconds),
        str(limits.memory_mb),
        str(limits.cpus),
        str(limits.pids_limit),
        str(limits.tmpfs_size_mb),
        stdin,
        source,
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


@runtime_checkable
class SandboxRunner(Protocol):
    """图与工具只依赖这个协议，不依赖 docker。测试用 `FakeSandboxRunner` 顶替。

    `@runtime_checkable` 不是装饰品：`tool(run_code)` 的 `InjectedToolArg` 注解会让
    pydantic 为参数建 `is-instance` 校验器，而普通 Protocol **不能**用 `isinstance` 检查
    （`TypeError: 'cls' must be valid as the first argument to 'isinstance'`，T3 实测）。
    """

    async def run(
        self, source: str, *, stdin: str = "", timeout_seconds: int | None = None
    ) -> SandboxResult: ...


class DockerSandboxRunner:
    """Docker 实现。**容器参数全部来自 `SANDBOX_LIMITS`**，本类不另设旋钮。"""

    def __init__(
        self,
        *,
        client: DockerClient,
        image: str = SANDBOX_IMAGE,
        limits: SandboxLimits = SANDBOX_LIMITS,
    ) -> None:
        self._client = client
        self._image = image
        self._limits = limits

    async def run(
        self, source: str, *, stdin: str = "", timeout_seconds: int | None = None
    ) -> SandboxResult:
        # 阻塞的 SDK 调用必须离开事件循环（AGENTS.md §3.4）：
        # 一个 10 秒的沙箱任务若直接在协程里跑，会拖垮同一进程里所有 SSE 连接。
        return await asyncio.to_thread(self._run_sync, source, stdin, timeout_seconds)

    # ---------- 以下是同步实现，只在 to_thread 里执行 ----------

    def _run_sync(self, source: str, stdin: str, timeout_seconds: int | None) -> SandboxResult:
        started = time.perf_counter()
        limits = self._limits
        key = sandbox_key(source, stdin=stdin, limits=limits)

        rejection = self._rejection_reason(source)
        if rejection is not None:
            logger.info(
                "sandbox.rejected",
                idempotency_key=key,
                reason=rejection,
                duration_ms=_elapsed_ms(started),
            )
            return SandboxResult(
                status="rejected",
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=_elapsed_ms(started),
                is_truncated=False,
                stdout_lines=0,
                stderr_lines=0,
                rejected_reason=rejection,
                idempotency_key=key,
            )

        timeout = limits.timeout_seconds if timeout_seconds is None else timeout_seconds
        container = None
        stdin_stream = None
        timed_out = False
        exit_code: int | None = None
        stdout = ""
        stderr = ""
        try:
            container = self._client.containers.create(**self._create_kwargs())
            # 必须先 attach、再 start：stdin 通道只对"已 attach 的会话"存在。
            stdin_stream = container.attach_socket(params={"stdin": 1, "stream": 1})
            container.start()
            self._feed_source(stdin_stream, source, stdin)
            timed_out = self._wait_until_exit(container, timeout)
            exit_code = None if timed_out else self._exit_code(container)
            stdout = _decode(container.logs(stdout=True, stderr=False))
            stderr = _decode(container.logs(stdout=False, stderr=True))
        except DockerException as exc:
            # 基础设施故障才是服务端错误（D2）。用户代码永远不会走到这里 ——
            # 它要么正常退出，要么 timeout 被杀，两者都返回 SandboxResult。
            raise SandboxError(f"沙箱不可用：{exc}") from exc
        except OSError as exc:
            # stdin 写入阶段没有 DockerException，只有 socket 层的 OSError（管道断掉 / 写超时）。
            # 代码送不进容器 = 沙箱坏了，同样是服务端错误，不能让它裸奔成 500 INTERNAL_ERROR。
            raise SandboxError(f"沙箱不可用：{exc}") from exc
        finally:
            if stdin_stream is not None:
                _close_quietly(stdin_stream)
            if container is not None:
                self._remove(container, key)

        stdout, stdout_truncated, stdout_lines = truncate_output(
            stdout,
            head_lines=limits.output_head_lines,
            tail_lines=limits.output_tail_lines,
            max_chars=limits.max_output_chars,
        )
        stderr, stderr_truncated, stderr_lines = truncate_output(
            stderr,
            head_lines=limits.output_head_lines,
            tail_lines=limits.output_tail_lines,
            max_chars=limits.max_output_chars,
        )
        status: SandboxStatus = "timeout" if timed_out else ("ok" if exit_code == 0 else "error")
        result = SandboxResult(
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=_elapsed_ms(started),
            is_truncated=stdout_truncated or stderr_truncated,
            stdout_lines=stdout_lines,
            stderr_lines=stderr_lines,
            idempotency_key=key,
        )
        logger.info(
            "sandbox.exec",
            idempotency_key=key,
            status=status,
            exit_code=exit_code,
            duration_ms=result.duration_ms,
            is_truncated=result.is_truncated,
        )
        return result

    def _rejection_reason(self, source: str) -> str | None:
        """进容器前的拒绝线。**不建容器**，因此没有 docker 调用的失败面。"""
        if not source.strip():
            return "代码为空"
        size = len(source.encode("utf-8"))
        if size > self._limits.max_code_bytes:
            return f"代码超过 {self._limits.max_code_bytes} 字节"
        return None

    def _create_kwargs(self) -> dict[str, Any]:
        """容器参数表（SPEC §5.2）。**每一项都对应一条契约**，不要在这里加"优化"。"""
        limits = self._limits
        kwargs: dict[str, Any] = {
            "image": self._image,
            # 镜像里的 CMD 只是占位：真实入口是引导脚本，它从 stdin 收代码后 exec 到 main.py。
            "command": ["python", "-I", "-u", "-c", _BOOTSTRAP_SOURCE],
            "working_dir": "/work",
            "stdin_open": True,  # 代码经 stdin 送进来（见 `_BOOTSTRAP_SOURCE` 上方注释）
            "network_disabled": not limits.is_network_enabled,
            "read_only": limits.is_rootfs_read_only,
            "mem_limit": f"{limits.memory_mb}m",
            "memswap_limit": f"{limits.memory_mb}m",  # 不许拿 swap 绕过内存上限
            "nano_cpus": int(limits.cpus * 1_000_000_000),
            "pids_limit": limits.pids_limit,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "tmpfs": {"/tmp": f"rw,noexec,nosuid,size={limits.tmpfs_size_mb}m"},
            "environment": {
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            },
            "labels": {"pyassist.role": "sandbox"},
        }
        if limits.is_non_root:
            kwargs["user"] = "10001:10001"
        return kwargs

    def _feed_source(self, stream: Any, source: str, stdin: str) -> None:
        """把 `[长度头] + 源码 + stdin` 写进容器的 stdin，然后关掉写端。

        关闭写端不是可选的：引导脚本要靠 EOF 才知道 `rest`（程序 stdin）到头了。
        `settimeout` 同样是保险 —— 容器若因故障不再读 stdin，`sendall` 会在
        `_STDIN_WRITE_TIMEOUT_SECONDS` 内失败，而不是把这个线程永久挂住
        （它跑在 `to_thread` 里，挂住就是泄漏一个线程）。
        """
        settimeout = getattr(stream, "settimeout", None)
        if settimeout is not None:
            settimeout(_STDIN_WRITE_TIMEOUT_SECONDS)
        encoded = source.encode("utf-8")
        payload = f"{len(encoded)}\n".encode() + encoded
        if stdin:
            payload += stdin.encode("utf-8")
        _send_and_close_write(stream, payload)

    def _wait_until_exit(self, container: Any, timeout: int) -> bool:
        """轮询到退出或超时。超时返回 True（并已经 kill）。"""
        deadline = time.monotonic() + timeout
        while True:
            container.reload()
            state = container.attrs.get("State") or {}
            status = state.get("Status")
            if status in _FINISHED_STATES:
                return False
            if status not in _RUNNING_STATES and status is not None:
                # 未知状态：当作已结束，让 exit_code 去表达真实结果。
                return False
            if time.monotonic() >= deadline:
                self._kill(container)
                return True
            time.sleep(SANDBOX_POLL_INTERVAL_SECONDS)

    def _exit_code(self, container: Any) -> int:
        state = container.attrs.get("State") or {}
        code = state.get("ExitCode")
        return code if isinstance(code, int) else -1

    def _kill(self, container: Any) -> None:
        try:
            container.kill()
        except DockerException as exc:
            # 已经退出 / 已被删除时 kill 会 409。这不是主流程故障，但要留痕。
            logger.warning("sandbox.kill_failed", error=type(exc).__name__)

    def _remove(self, container: Any, key: str) -> None:
        try:
            container.remove(force=True)
        except DockerException as exc:
            # 只记日志，**不覆盖**主结果（SPEC §5.2 第 7 步）。这里不能 `except: pass`。
            logger.warning("sandbox.remove_failed", idempotency_key=key, error=type(exc).__name__)


def build_sandbox_runner() -> DockerSandboxRunner | None:
    """启动期探测：daemon 不可达 / 镜像不存在 → 返回 None（调用方降级并打日志）。

    返回 `None` 是**正常启动路径**：v1 问答不依赖沙箱，一个没装 Docker 的开发机
    不应该连 `/chat` 都起不来。`/diagnose` 会因此返回 500 `CONFIG_ERROR`。
    """
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(SANDBOX_IMAGE)
    except DockerException as exc:
        logger.error(
            "sandbox.check",
            is_available=False,
            image=SANDBOX_IMAGE,
            error=type(exc).__name__,
        )
        return None
    logger.info("sandbox.check", is_available=True, image=SANDBOX_IMAGE)
    return DockerSandboxRunner(client=client)


def probe_sandbox() -> tuple[bool, str | None]:
    """健康检查用的只读探测：能 ping 到 daemon 且镜像在位。

    与 `build_sandbox_runner()` 的区别是**不构造 runner、不保连接**：
    `/health` 每次调用都新建客户端会泄漏连接池，所以这里用完即弃（由 SDK 回收）。
    """
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(SANDBOX_IMAGE)
    except DockerException as exc:
        return False, str(exc)
    return True, None


def _send_and_close_write(stream: Any, payload: bytes) -> None:
    """写 payload，然后**只关写端**（半关闭）—— 容器侧要靠 EOF 判断输入到头了。

    `attach_socket()` 的返回值随传输层变形：Unix socket 给的是包了一层的 `Socket`
    （原始套接字在 `_sock`），Windows npipe 直接就是 `NpipeSocket` 自己。
    两种都要能写、能半关闭，所以先探测 `_sock`。
    """
    target = getattr(stream, "_sock", None) or stream
    target.sendall(payload)
    shutdown = getattr(target, "shutdown", None)
    if shutdown is not None:
        shutdown(socket.SHUT_WR)


def _close_quietly(stream: Any) -> None:
    """关闭 attach 会话。失败只记日志 —— 容器已经跑完，不能让它覆盖主结果。"""
    try:
        stream.close()
    except OSError as exc:
        logger.warning("sandbox.stdin_close_failed", error=type(exc).__name__)


def _decode(raw: bytes | str) -> str:
    """日志字节 → 文本。用户输出里出现坏字节是常态，`replace` 比抛异常合理。"""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
