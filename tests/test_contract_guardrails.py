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
# ChatDeepSeekPatched 是阶段二的唯一模型类，同样只有 client.py 能构造它。
FORBIDDEN_LLM_CONSTRUCTORS = ("ChatDeepSeek", "ChatDeepSeekPatched", "ChatOpenAI")
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
    """§2：除 llm/client.py 外，不得直接构造 ChatDeepSeek / ChatDeepSeekPatched / ChatOpenAI。

    只匹配 `ast.Call`，因此 `deepseek_chat.py` 里的 `class ChatDeepSeekPatched(...)`
    定义（`ast.ClassDef`）不会误伤。
    """
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


# ---------- 阶段三追加（SPEC §4.10）----------
CHECKPOINTER_PATH = REPO_ROOT / "src" / "pyassist" / "agent" / "checkpointer.py"
STORE_PATH = REPO_ROOT / "src" / "pyassist" / "agent" / "store.py"
SESSION_PATH = REPO_ROOT / "src" / "pyassist" / "db" / "session.py"
REPOSITORIES_PATH = REPO_ROOT / "src" / "pyassist" / "db" / "repositories.py"

CHECKPOINT_MODULE_PREFIX = "langgraph.checkpoint"
STORE_MODULE_PREFIX = "langgraph.store"
CHECKPOINT_CLASS_NAMES = frozenset(
    {"SqliteSaver", "AsyncSqliteSaver", "PostgresSaver", "AsyncPostgresSaver"}
)
STORE_CLASS_NAMES = frozenset(
    {"SqliteStore", "AsyncSqliteStore", "PostgresStore", "AsyncPostgresStore"}
)
ENGINE_FACTORY_NAMES = frozenset({"create_engine", "create_async_engine"})


def _call_name(node: ast.Call) -> str | None:
    """`f(...)` 与 `a.b.f(...)` 都取出末段名，其余返回 None。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    return getattr(func, "attr", None)


def test_checkpointer_classes_only_in_checkpointer_module() -> None:
    """§2：检查点类（同步/异步）只在 agent/checkpointer.py 被引用。

    匹配 `from langgraph.checkpoint... import ...` 与 `...Saver(...)` 构造两类。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == CHECKPOINTER_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                CHECKPOINT_MODULE_PREFIX
            ):
                offenders.add(_tree_relative(path))
            if isinstance(node, ast.Call) and _call_name(node) in CHECKPOINT_CLASS_NAMES:
                offenders.add(_tree_relative(path))
    assert not offenders, f"检查点类只允许在 agent/checkpointer.py 引用，违例：{sorted(offenders)}"


def test_store_classes_only_in_store_module() -> None:
    """§2：Store 类（同步/异步）只在 agent/store.py 被引用。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == STORE_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                STORE_MODULE_PREFIX
            ):
                offenders.add(_tree_relative(path))
            if isinstance(node, ast.Call) and _call_name(node) in STORE_CLASS_NAMES:
                offenders.add(_tree_relative(path))
    assert not offenders, f"Store 类只允许在 agent/store.py 引用，违例：{sorted(offenders)}"


def test_engine_constructed_only_in_db_session() -> None:
    """§2：引擎的唯一构造点是 db/session.py（业务路径）。

    `migrations/env.py` 用同步引擎跑 DDL，不在 src/ 扫描面内，天然不受这条约束。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == SESSION_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) in ENGINE_FACTORY_NAMES:
                offenders.add(_tree_relative(path))
    assert not offenders, f"引擎只允许在 db/session.py 构造，违例：{sorted(offenders)}"


def test_db_session_has_no_dialect_wording() -> None:
    """§2 / SPEC §4.1：方言知识集中在 config.py，db/session.py 不得出现方言字样。

    文本扫描（注释与 docstring 也算）—— 这正是"把方言知识挪去 config.py"的可执行形式。
    """
    text = _read_text(SESSION_PATH)
    assert text is not None, "db/session.py 应存在"
    assert "sqlite" not in text.lower(), "db/session.py 不得出现方言字样（含注释）"


def test_raw_sql_only_in_repositories() -> None:
    """§2：裸 SQL 只在 db/repositories.py（`migrations/` 是 DDL，不在扫描面内）。

    匹配 `text(...)` / `sqlalchemy.text(...)` / `....exec_driver_sql(...)` 三类 AST 调用。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == REPOSITORIES_PATH:
            continue
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if _call_name(node) == "text" or (
                isinstance(func, ast.Attribute) and func.attr == "exec_driver_sql"
            ):
                offenders.add(_tree_relative(path))
    assert not offenders, f"裸 SQL 只允许在 db/repositories.py 出现，违例：{sorted(offenders)}"
