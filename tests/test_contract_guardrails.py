"""契约守护：把 AGENTS.md §2 / §4 的禁区长进 CI（SPEC §5.6）。

扫描范围**只覆盖会真正发出去的代码与配置面**，刻意排除 `AGENTS.md` / `ROADMAP.md` /
`SPEC.md` —— 契约与路线图必须在正文里点名已停用模型才能讲清楚历史，把它们纳入扫描
只会让守护测试与契约自相矛盾。

安全细节：断言失败信息只打印**文件路径与命中的规则名**，绝不回显文件内容 ——
扫描面包含 `.env`，回显即泄漏 key。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [REPO_ROOT / "src", REPO_ROOT / "tests"]
SCAN_FILES = [
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / ".env.example",
    REPO_ROOT / ".env",
    REPO_ROOT / "pyproject.toml",
]

# 用拼接构造，避免守护测试自己被自己命中。
FORBIDDEN_MODEL_IDS = ("deepseek-" + "chat", "deepseek-" + "reasoner")

# 生成式 AI 集成包的构造点：全项目只允许出现于 llm/client.py（§2 单点所有权）。
FORBIDDEN_LLM_CONSTRUCTORS = ("ChatDeepSeek", "ChatOpenAI")
LLM_CLIENT_PATH = REPO_ROOT / "src" / "pyassist" / "llm" / "client.py"

FORBIDDEN_MODULE_NAMES = ("utils.py", "helpers.py", "common.py")

_SKIP_DIR_PARTS = {"__pycache__", ".venv", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}


def _is_scannable(path: Path) -> bool:
    return path.is_file() and not (_SKIP_DIR_PARTS & set(path.parts))


def _iter_tree_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if _is_scannable(path):
            yield path


def _scannable_files() -> Iterator[Path]:
    """扫描面：SCAN_DIRS 下的文件 + SCAN_FILES（存在才纳入）。"""
    for directory in SCAN_DIRS:
        if directory.exists():
            yield from _iter_tree_files(directory)
    for path in SCAN_FILES:
        if _is_scannable(path):
            yield path


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _src_python_files() -> Iterator[Path]:
    src = REPO_ROOT / "src"
    if not src.exists():
        return
    yield from _iter_tree_files(src)
    # 只保留 .py
    # （上面已过滤隐藏/缓存目录；此处按后缀收窄）


def _parse(path: Path) -> ast.Module | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return ast.parse(text, filename=str(path))
    except SyntaxError:
        return None


def _python_sources() -> Iterator[tuple[Path, ast.Module]]:
    for path in _src_python_files():
        if path.suffix != ".py":
            continue
        module = _parse(path)
        if module is not None:
            yield path, module


def _tree_relative(path: Path) -> str:
    """只给出相对路径，避免把绝对路径或内容带进断言信息。"""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def test_no_deprecated_model_ids_in_scanned_surface() -> None:
    """§1 / §4：已停用模型名不得出现在会真正发出去的代码与配置里。"""
    offenders: list[str] = []
    for path in _scannable_files():
        text = _read_text(path)
        if text is None:
            continue
        if any(model_id in text for model_id in FORBIDDEN_MODEL_IDS):
            # 只记路径，不回显命中内容（扫描面含 .env）。
            offenders.append(_tree_relative(path))
    assert not offenders, f"发现已停用模型名，命中文件：{offenders}"


def test_no_requirements_txt() -> None:
    """§1：唯一依赖来源是 pyproject.toml，禁止 requirements.txt。"""
    offenders = [
        _tree_relative(path) for path in REPO_ROOT.rglob("requirements.txt") if _is_scannable(path)
    ]
    assert not offenders, f"不应存在 requirements.txt：{offenders}"


def test_no_transit_modules_under_src() -> None:
    """§2：禁止 utils.py / helpers.py / common.py 这类转口模块（会变成第二所有者）。"""
    src = REPO_ROOT / "src"
    offenders = [
        _tree_relative(path) for path in src.rglob("*.py") if path.name in FORBIDDEN_MODULE_NAMES
    ]
    assert not offenders, f"src/ 下不应存在转口模块：{offenders}"


def test_no_exec_eval_or_subprocess_in_src() -> None:
    """§4：禁止在 src/ 内 exec()/eval() 执行用户代码，禁止 subprocess / os.system 顶替沙箱。

    必须用 AST 而非 grep：注释里写"禁止用 subprocess 顶替沙箱"是完全合法的。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        for node in ast.walk(module):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in {"exec", "eval"}:
                    offenders.add(f"{_tree_relative(path)}:exec/eval")
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"system", "popen"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    offenders.add(f"{_tree_relative(path)}:os.{func.attr}")
            if isinstance(node, ast.Import) and any(
                alias.name.split(".")[0] == "subprocess" for alias in node.names
            ):
                offenders.add(f"{_tree_relative(path)}:import subprocess")
            if (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.split(".")[0] == "subprocess"
            ):
                offenders.add(f"{_tree_relative(path)}:from subprocess")
    assert not offenders, f"src/ 内命中禁区：{sorted(offenders)}"


def test_no_direct_llm_construction_outside_client() -> None:
    """§2：除 llm/client.py 外，不得直接构造 ChatDeepSeek / ChatOpenAI。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == LLM_CLIENT_PATH:
            continue
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in FORBIDDEN_LLM_CONSTRUCTORS:
                offenders.add(_tree_relative(path))
    assert not offenders, f"模型实例只允许在 llm/client.py 构造，违例：{sorted(offenders)}"


def test_no_memory_saver_in_src() -> None:
    """§2：禁止 MemorySaver（重启即丢），检查点必须落同一个库。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        for node in ast.walk(module):
            if isinstance(node, ast.Name) and node.id == "MemorySaver":
                offenders.add(_tree_relative(path))
            if isinstance(node, ast.Attribute) and node.attr == "MemorySaver":
                offenders.add(_tree_relative(path))
    assert not offenders, f"src/ 内不应出现 MemorySaver：{sorted(offenders)}"


def test_basesettings_defined_only_in_config() -> None:
    """§2：BaseSettings 子类只允许在 config.py 里定义（唯一配置入口）。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        for node in ast.walk(module):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", None)
                if base_name == "BaseSettings":
                    offenders.add(_tree_relative(path))
    assert offenders == {"src/pyassist/config.py"}, (
        f"BaseSettings 子类应只在 config.py 定义，实际：{sorted(offenders)}"
    )


def test_structlog_configured_only_in_logging_module() -> None:
    """§2：structlog.configure 的唯一调用点是 pyassist/logging.py。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "configure"
                and isinstance(func.value, ast.Name)
                and func.value.id == "structlog"
            ):
                offenders.add(_tree_relative(path))
    assert offenders == {"src/pyassist/logging.py"}, (
        f"structlog.configure 应只在 logging.py 调用，实际：{sorted(offenders)}"
    )
