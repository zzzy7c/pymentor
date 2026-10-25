"""真实隔离验收（SPEC §5.17 / 判据 6–7）。**默认不跑**：`-m docker` 显式开启。

    docker build -t pyassist-sandbox:1 -f src/pyassist/sandbox/Dockerfile.sandbox .
    uv run pytest -m docker tests/test_sandbox_docker.py

这些用例真的起容器、真的执行用户代码。它们不能被 mock 替代：
"配置里写了 `network_disabled=True`"与"容器里真的连不上网"是两件事。

**daemon 不可用时整组 skip**（不是 fail）：开发者机器上没装 Docker 不该让
`pytest -m docker` 变成一片红；skip 的理由写清楚即可。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import docker
import pytest

from pyassist.config import SANDBOX_IMAGE
from pyassist.sandbox.runner import DockerSandboxRunner, SandboxResult, build_sandbox_runner

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def runner() -> DockerSandboxRunner:
    built = build_sandbox_runner()
    if built is None:
        # 没有 daemon 或没有镜像：这是环境问题，不是代码问题（SPEC S3）。
        pytest.skip(
            f"Docker daemon 未运行，或镜像 {SANDBOX_IMAGE} 不存在；"
            f"先 `docker build -t {SANDBOX_IMAGE} -f src/pyassist/sandbox/Dockerfile.sandbox .`"
        )
    return built


@pytest.fixture(scope="module")
def docker_client() -> Iterator[docker.DockerClient]:
    client = docker.from_env()
    try:
        yield client
    finally:
        client.close()


async def _run(
    runner: DockerSandboxRunner, source: str, *, timeout_seconds: int | None = None
) -> SandboxResult:
    return await runner.run(source, timeout_seconds=timeout_seconds)


# ---------- ① 正常执行 / ② 非零退出 ----------


async def test_normal_execution(runner: DockerSandboxRunner) -> None:
    result = await _run(runner, "print(6 * 7)")

    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.stdout.strip() == "42"
    assert result.duration_ms >= 0


async def test_nonzero_exit_is_a_result(runner: DockerSandboxRunner) -> None:
    result = await _run(runner, "raise SystemExit(3)")

    assert result.status == "error"
    assert result.exit_code == 3


# ---------- ③ 死循环 → timeout ----------


async def test_infinite_loop_is_killed(runner: DockerSandboxRunner) -> None:
    result = await _run(runner, "while True:\n    pass\n", timeout_seconds=1)

    assert result.status == "timeout"
    assert result.exit_code is None
    assert result.duration_ms < 15_000


# ---------- ④ 无网络 ----------


async def test_network_is_disabled(runner: DockerSandboxRunner) -> None:
    source = (
        "import socket\n"
        "try:\n"
        '    socket.create_connection(("1.1.1.1", 80), timeout=2)\n'
        "except OSError as exc:\n"
        '    print("blocked:", type(exc).__name__)\n'
        "else:\n"
        '    raise SystemExit("network is reachable")\n'
    )
    result = await _run(runner, source)

    assert result.status == "ok"
    assert "blocked:" in result.stdout


# ---------- ⑤ 只读根文件系统 ----------


async def test_root_filesystem_is_read_only(runner: DockerSandboxRunner) -> None:
    source = (
        "try:\n"
        '    with open("/work/hacked.txt", "w") as handle:\n'
        '        handle.write("x")\n'
        "except OSError as exc:\n"
        "    print('readonly:', exc.errno)\n"
        "else:\n"
        '    raise SystemExit("root filesystem is writable")\n'
    )
    result = await _run(runner, source)

    assert result.status == "ok"
    assert "readonly:" in result.stdout


# ---------- ⑥ 非 root ----------


async def test_runs_as_unprivileged_user(runner: DockerSandboxRunner) -> None:
    result = await _run(runner, "import os\nprint(os.getuid(), os.getgid())")

    assert result.status == "ok"
    assert result.stdout.strip() == "10001 10001"


# ---------- ⑦ 恶意用例 ----------


async def test_fork_bomb_is_capped_by_pids_limit(runner: DockerSandboxRunner) -> None:
    source = (
        "import os\n"
        "count = 0\n"
        "try:\n"
        "    while count < 200:\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            os._exit(0)\n"
        "        count += 1\n"
        "except OSError as exc:\n"
        '    print(f"stopped after {count}: {type(exc).__name__}")\n'
    )
    result = await _run(runner, source)

    assert "stopped after" in result.stdout
    assert result.status in {"ok", "error", "timeout"}


async def test_rm_rf_does_not_touch_the_host(runner: DockerSandboxRunner) -> None:
    result = await _run(runner, 'import os\nos.system("rm -rf /")\nprint("survived")')

    # 只读根文件系统让 rm 大面积失败；即使部分 tmpfs 被删，容器也只是非零退出。
    assert result.status in {"ok", "error"}
    assert "survived" in result.stdout


async def test_etc_passwd_read_sees_the_container(runner: DockerSandboxRunner) -> None:
    """读 `/etc/passwd` 是**允许**的：只读根文件系统不阻止读。
    安全边界在于它读到的是容器内的文件，且容器里只有 UID 10001 的入口。"""
    result = await _run(
        runner,
        "print(open('/etc/passwd', encoding='utf-8').read())",
    )

    assert result.status == "ok"
    assert "root:" in result.stdout  # 容器镜像自带的 /etc/passwd
    assert "10001:" in result.stdout


# ---------- ⑧ 无残留容器 ----------


async def test_no_containers_leak_after_run(
    runner: DockerSandboxRunner, docker_client: docker.DockerClient
) -> None:
    await _run(runner, "print('bye')")

    leftovers: list[Any] = docker_client.containers.list(
        all=True, filters={"label": "pyassist.role=sandbox"}
    )
    assert leftovers == []
