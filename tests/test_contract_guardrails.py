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

from pyassist.config import QA_CONTEXT_PROMPT_NAME

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


def test_structured_output_constructed_only_in_client() -> None:
    """§2 / §1：`with_structured_output(...)` 只允许在 llm/client.py 出现。

    默认的 `method="function_calling"` 会绑 `tool_choice`，而思考模式不支持它，
    真实调用必回 400。把调用点收进 `get_structured_output` 是防回归的唯一办法 ——
    单轮问答测不出来，只有走到修复/出题/批改/推荐才会炸。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == LLM_CLIENT_PATH:
            continue
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "with_structured_output"
            ):
                offenders.add(_tree_relative(path))
    assert not offenders, (
        f"结构化输出只允许经 llm/client.py::get_structured_output 构造，违例：{sorted(offenders)}"
    )


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

    匹配 `from langgraph.checkpoint... import <具体 Saver 类>` 与 `...Saver(...)` 构造两类。

    **按导入的类名而不是模块前缀判定**（阶段六收紧，SPEC §5.16 #29）：图和 app 工厂
    需要 `from langgraph.checkpoint.base import BaseCheckpointSaver` 做类型标注 ——
    那是**抽象基类**，不能构造、也不指向任何状态库。按前缀判定会把这类纯 typing
    import 一并误伤，逼人用 `type: ignore` 或 `Any`（§5.1 判据 18 禁止）。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == CHECKPOINTER_PATH:
            continue
        for node in ast.walk(module):
            if (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith(CHECKPOINT_MODULE_PREFIX)
                and any(alias.name in CHECKPOINT_CLASS_NAMES for alias in node.names)
            ):
                offenders.add(_tree_relative(path))
            if isinstance(node, ast.Call) and _call_name(node) in CHECKPOINT_CLASS_NAMES:
                offenders.add(_tree_relative(path))
    assert not offenders, f"检查点类只允许在 agent/checkpointer.py 引用，违例：{sorted(offenders)}"


def test_store_classes_only_in_store_module() -> None:
    """§2：Store 类（同步/异步）只在 agent/store.py 被引用。

    判定口径与上面的检查点守护一致（阶段八对齐，理由相同）：**按导入的类名**，
    不按模块前缀。`BaseStore` 是抽象基类，服务层与路由需要它做类型标注 ——
    按前缀判定会把纯 typing import 一并误伤，逼人退回 `Any`。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == STORE_PATH:
            continue
        for node in ast.walk(module):
            if (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith(STORE_MODULE_PREFIX)
                and any(alias.name in STORE_CLASS_NAMES for alias in node.names)
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


# ---------- 阶段四追加（SPEC §4.10）----------
API_ERRORS_PATH = REPO_ROOT / "src" / "pyassist" / "api" / "errors.py"
API_MAIN_PATH = REPO_ROOT / "src" / "pyassist" / "api" / "main.py"
ERRORS_PATH = REPO_ROOT / "src" / "pyassist" / "errors.py"


def test_http_exception_only_raised_in_api_errors() -> None:
    """§4：HTTPException 只许在 api/errors.py 出现（在别处 raise 会让错误格式分叉）。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == API_ERRORS_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) == "HTTPException":
                offenders.add(_tree_relative(path))
    assert not offenders, f"HTTPException 只允许在 api/errors.py 构造，违例：{sorted(offenders)}"


def test_exception_handlers_registered_only_from_api_errors() -> None:
    """§4：唯一映射点 —— add_exception_handler 只许在 api/errors.py 调用。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == API_ERRORS_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) == "add_exception_handler":
                offenders.add(_tree_relative(path))
    assert not offenders, f"异常处理器只允许在 api/errors.py 注册，违例：{sorted(offenders)}"


def test_app_error_subclasses_only_in_errors_module() -> None:
    """§3.2：异常树的定义只许在 errors.py（别处只能 raise，不能定义新异常）。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == ERRORS_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef) and any(
                (b.id if isinstance(b, ast.Name) else getattr(b, "attr", None)) == "AppError"
                for b in node.bases
            ):
                offenders.add(_tree_relative(path))
    assert not offenders, f"AppError 子类只允许在 errors.py 定义，违例：{sorted(offenders)}"


def test_json_response_only_in_api_errors() -> None:
    """§4：响应体的唯一构造点是 api/errors.py（router 不得手搓 JSONResponse）。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == API_ERRORS_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) == "JSONResponse":
                offenders.add(_tree_relative(path))
    assert not offenders, f"JSONResponse 只允许在 api/errors.py 构造，违例：{sorted(offenders)}"


def test_fastapi_constructed_only_in_api_main() -> None:
    """§2：应用工厂只有一个（FastAPI(...) 只许在 api/main.py）。"""
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == API_MAIN_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) == "FastAPI":
                offenders.add(_tree_relative(path))
    assert not offenders, f"FastAPI 只允许在 api/main.py 构造，违例：{sorted(offenders)}"


def test_no_swallowed_exceptions() -> None:
    """§4：`except Exception: pass` / `...` 是吞异常，禁止。

    只拦"函数体仅由 pass / ... 构成"的 except 块；`_handle_unexpected` 与 `/health`
    的探活 try **都有函数体**（构造信封 / 置降级 + 打点），不会被误伤。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        for node in ast.walk(module):
            if not isinstance(node, ast.ExceptHandler) or len(node.body) != 1:
                continue
            only = node.body[0]
            swallowed = isinstance(only, ast.Pass) or (
                isinstance(only, ast.Expr)
                and isinstance(only.value, ast.Constant)
                and only.value.value is Ellipsis
            )
            if swallowed:
                offenders.add(_tree_relative(path))
    assert not offenders, f"禁止吞异常（except: pass/...），违例：{sorted(offenders)}"


# ---------- 阶段五追加（SPEC D12）----------
RAG_DIR = REPO_ROOT / "src" / "pyassist" / "rag"
RAG_STORE_PATH = RAG_DIR / "store.py"
RAG_INGEST_PATH = RAG_DIR / "ingest.py"
RAG_RETRIEVER_PATH = RAG_DIR / "retriever.py"
RAG_EMBEDDER_PATH = RAG_DIR / "embedder.py"
RAG_CORPUS_PATH = RAG_DIR / "corpus.py"

# #20：只列具名工厂。裸 `Client(` 太宽 —— 阶段六的 `httpx.Client` 会被误伤。
CHROMA_CLIENT_FACTORIES = frozenset({"PersistentClient", "EphemeralClient", "HttpClient"})
# #21：允许 import chromadb 的两个文件之外的读路径。`corpus.py` **不在**其中（#26 的正面）。
CHROMADB_ALLOWED = frozenset({RAG_STORE_PATH, RAG_INGEST_PATH, RAG_RETRIEVER_PATH})
# #26：解析层不得反向依赖它下面任何一层。
CORPUS_FORBIDDEN_IMPORTS = frozenset(
    {
        "rag.embedder",
        "rag.store",
        "rag.ingest",
        "pyassist.rag.embedder",
        "pyassist.rag.store",
        "pyassist.rag.ingest",
    }
)


def _call_keywords(node: ast.Call) -> set[str]:
    """关键字实参名集合 —— **按签名特征匹配**（D12）。

    `_call_name()` 对 `.upsert(` / `.query(` 这种通用方法名太宽：任何领域层的
    `upsert` / `query` 都会被命中。加一层关键字约束（`ids=` / `query_embeddings=`）
    才真正落在"这是不是 Chroma 那次调用"上。
    """
    return {keyword.arg for keyword in node.keywords if keyword.arg is not None}


def _imported_modules(module: ast.Module) -> Iterator[tuple[str, int]]:
    """模块里出现的 import 模块名与行号。`import a.b` 与 `from a.b import c` 都归一化成 `a.b`。"""
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom):
            yield node.module or "", node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


def test_chroma_client_constructed_only_in_rag_store() -> None:
    """AGENTS.md §2 单点所有权：「写向量库 / 读向量库 / 向量库连接」的唯一归属是 `rag/`。

    Chroma 客户端的构造点收在 `rag/store.py`（SPEC D3：其他文件一律经 `get_client`）。
    开放第二个构造点意味着出现第二个库目录或第二份 `Settings`，
    而 `anonymized_telemetry=False` 漏掉的那一份会静默出网（C2）。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == RAG_STORE_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) in CHROMA_CLIENT_FACTORIES:
                offenders.add(_tree_relative(path))
    assert not offenders, f"Chroma 客户端只允许在 rag/store.py 构造，违例：{sorted(offenders)}"


def test_chromadb_imported_only_by_the_three_rag_modules() -> None:
    """SPEC D12 #21（也是 §2 单点所有权的可执行形式）。

    **`rag/corpus.py` 被这条挡在外面**：解析层是纯的，不碰向量库。
    少了这条，`corpus.py` 里加一句 `import chromadb` 也能让测试全绿，
    直到有人试图单测 `parse_document` 才发现要先起一个向量库。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path in CHROMADB_ALLOWED:
            continue
        for module_name, _lineno in _imported_modules(module):
            if module_name == "chromadb" or module_name.startswith("chromadb."):
                offenders.add(_tree_relative(path))
    assert not offenders, (
        f"chromadb 只允许被 rag/store.py、rag/ingest.py、rag/retriever.py import，"
        f"违例：{sorted(offenders)}"
    )


def test_sentence_transformers_only_in_rag_embedder() -> None:
    """AGENTS.md §2：「向量化文本」的唯一归属是 `rag/embedder.py`。

    构造调用与静态 import 两条都查。**不查动态导入**是有意的 —— 用
    `importlib.import_module` 绕开静态扫描会返回 `Any`，反而先踩上 mypy 的
    `no-any-return`（§5.1 判据 18 禁止 `type: ignore`），所以那条路走不通。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == RAG_EMBEDDER_PATH:
            continue
        for module_name, _lineno in _imported_modules(module):
            if module_name == "sentence_transformers" or module_name.startswith(
                "sentence_transformers."
            ):
                offenders.add(_tree_relative(path))
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) == "SentenceTransformer":
                offenders.add(_tree_relative(path))
    assert not offenders, (
        f"sentence_transformers 与 SentenceTransformer 只允许在 rag/embedder.py 出现，"
        f"违例：{sorted(offenders)}"
    )


def test_chroma_upsert_only_in_rag_ingest() -> None:
    """AGENTS.md §2：「写向量库」的唯一归属是 `rag/ingest.py`。

    匹配 `.upsert(` **且带 `ids=`**。只按方法名匹配会误伤任何领域层的 upsert，
    而"运行时往库里 upsert"恰恰是 §2 明令禁止的（读路径不得写库）。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == RAG_INGEST_PATH:
            continue
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) == "upsert"
                and "ids" in _call_keywords(node)
            ):
                offenders.add(_tree_relative(path))
    assert not offenders, (
        f"Chroma upsert（带 ids=）只允许在 rag/ingest.py 调用，违例：{sorted(offenders)}"
    )


def test_chroma_query_only_in_rag_retriever() -> None:
    """AGENTS.md §2：「读向量库」的唯一归属是 `rag/retriever.py`。

    匹配 `.query(` **且带 `query_embeddings=`**。不加关键字约束会连
    `session.query(...)` 一起误伤 —— 那是 SQLAlchemy 的 1.x 风格，本项目也用不着（§1）。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == RAG_RETRIEVER_PATH:
            continue
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) == "query"
                and "query_embeddings" in _call_keywords(node)
            ):
                offenders.add(_tree_relative(path))
    assert not offenders, (
        f"Chroma query（带 query_embeddings=）只允许在 rag/retriever.py 调用，"
        f"违例：{sorted(offenders)}"
    )


def test_no_chroma_default_embedding_path_in_src() -> None:
    """SPEC C3 / §2：Chroma 自带嵌入是**第二套向量化出口**，必须掐死。

    两种形态各查一条：省略 `embedding_function` 会让 Chroma 自行下载 ONNX 模型；
    显式传 `query_texts=` 则让它自己算向量 —— 两者都绕过了 `rag/embedder.py`。

    走 AST 关键字检查而**不是文本扫描**：`rag/store.py` 的注释里正大光明地写着
    `embedding_function=None` 和 `DefaultEmbeddingFunction` 来解释为什么必须显式传参，
    文本扫描会把这段说明本身判为违规。
    """
    offenders: list[str] = []
    for path, module in _python_sources():
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if "query_texts" in _call_keywords(node):
                offenders.append(f"{_tree_relative(path)}:{node.lineno} 用了 query_texts=")
            if _call_name(node) == "DefaultEmbeddingFunction":
                offenders.append(
                    f"{_tree_relative(path)}:{node.lineno} 构造了 DefaultEmbeddingFunction"
                )

    assert not offenders, (
        f"Chroma 自带嵌入路径必须关闭（显式 embedding_function=None），违例：{sorted(offenders)}"
    )


def test_rag_corpus_does_not_import_lower_layers() -> None:
    """SPEC D12 #26 / §5.5：`rag/corpus.py` 是**纯分层**。

    它只做下载与解析，不 import `rag.embedder` / `rag.store` / `rag.ingest`。
    这不是洁癖：`parse_document` 是纯函数，所以 §5.1 的离线测试才能不起向量库、
    不加载模型就跑。一旦反向依赖出现，那个性质会**立刻且无声地**消失 ——
    测试仍然能跑，只是每次都得先把 2GB 的模型拉起来。
    """
    module = _parse(RAG_CORPUS_PATH)
    assert module is not None, "rag/corpus.py 应存在且可解析"

    offenders: set[str] = set()
    for module_name, _lineno in _imported_modules(module):
        if module_name in CORPUS_FORBIDDEN_IMPORTS:
            offenders.add(module_name)

    assert not offenders, f"rag/corpus.py 不得 import 下层模块，违例：{sorted(offenders)}"


# ---------- 阶段六追加（SPEC §5.16 #27–#33）----------
AGENT_DIR = REPO_ROOT / "src" / "pyassist" / "agent"
GRAPHS_DIR = AGENT_DIR / "graphs"
REGISTRY_PATH = AGENT_DIR / "registry.py"
QA_GRAPH_PATH = GRAPHS_DIR / "qa.py"

# #27：LangChain 的成品 Agent 会隐藏工具循环，项目明令禁止（AGENTS.md §1）。
# `langgraph.prebuilt` 在本环境里**能 import 成功**（L13），所以必须查。
PREBUILT_AGENT_FACTORIES = frozenset({"create_react_agent", "create_agent"})
PREBUILT_MODULE_PREFIX = "langgraph.prebuilt"

# #32：白名单本身也要冻结 —— 否则"守护 #24"可以被一句 `CHROMADB_ALLOWED |= {...}` 悄悄放宽。
EXPECTED_CHROMADB_ALLOWED = frozenset({RAG_STORE_PATH, RAG_INGEST_PATH, RAG_RETRIEVER_PATH})


def _receiver_name(node: ast.Call) -> str | None:
    """`a.f(...)` 取出接收者末段名（`a` / `obj.a`），其余返回 None。"""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    value = func.value
    if isinstance(value, ast.Name):
        return value.id
    return getattr(value, "attr", None)


def test_no_prebuilt_agent_in_src() -> None:
    """SPEC §5.16 #27：不用 LangChain 的成品 Agent（AGENTS.md §1）。

    构造调用与 `import langgraph.prebuilt` 两条都查。`langgraph.prebuilt` 在当前
    版本里**能正常 import**，所以"import 不成功就不用查"的侥幸不成立。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        for module_name, _lineno in _imported_modules(module):
            if module_name == PREBUILT_MODULE_PREFIX or module_name.startswith(
                f"{PREBUILT_MODULE_PREFIX}."
            ):
                offenders.add(_tree_relative(path))
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) in PREBUILT_AGENT_FACTORIES:
                offenders.add(_tree_relative(path))
    assert not offenders, (
        f"禁止 create_react_agent / create_agent / langgraph.prebuilt，违例：{sorted(offenders)}"
    )


def test_tool_defined_only_in_registry() -> None:
    """SPEC §5.16 #28：`tool(` / `@tool` 只出现在 `agent/registry.py`。

    `@tool` 两种写法都查：`@tool`（裸 `ast.Name`）与 `@tool(...)`（`ast.Call`）。
    漏掉裸装饰器，就等于允许在各 service 里随手定义工具（§2 的典型违例）。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == REGISTRY_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) == "tool":
                offenders.add(_tree_relative(path))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Name) and decorator.id == "tool":
                        offenders.add(_tree_relative(path))
                    if isinstance(decorator, ast.Call) and _call_name(decorator) == "tool":
                        offenders.add(_tree_relative(path))
    assert not offenders, f"tool() 只允许在 agent/registry.py 使用，违例：{sorted(offenders)}"


def test_checkpoint_classes_not_referenced_by_graphs_or_routers() -> None:
    """SPEC §5.16 #29：检查点类只在 `agent/checkpointer.py`（#14 的阶段六延伸）。

    #14 查的是 `ImportFrom` 与构造调用；这里补上**属性引用**（`checkpoint.SqliteSaver`
    这类 `ast.Attribute`）—— 图与路由若拿得到类对象，就能绕开唯一构造点自己建一份状态库。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == CHECKPOINTER_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Attribute) and node.attr in CHECKPOINT_CLASS_NAMES:
                offenders.add(_tree_relative(path))
    assert not offenders, f"检查点类只允许在 agent/checkpointer.py 引用，违例：{sorted(offenders)}"


def test_stream_writer_only_in_graphs() -> None:
    """SPEC §5.16 #30：`get_stream_writer(` 只在 `agent/graphs/**` 调用（L5）。

    节点外调用 `get_stream_writer()` 必抛 —— 它依赖 LangGraph 的运行时上下文。
    放在路由或 service 里，写的时候不报错，跑起来才炸。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        for node in ast.walk(module):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) == "get_stream_writer"
                and not path.is_relative_to(GRAPHS_DIR)
            ):
                offenders.add(_tree_relative(path))
    assert not offenders, (
        f"get_stream_writer() 只允许在 agent/graphs/** 调用，违例：{sorted(offenders)}"
    )


def test_no_sync_graph_stream_in_src() -> None:
    """SPEC §5.16 #31：同步 `graph.stream(` 在 `src/` 内零出现（§2 流式约定）。

    只匹配"接收者名含 graph"的 `.stream(`：`astream` 是不同名字，天然不命中；
    而 `session.stream(...)` 这类无关调用也不会被误伤。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        for node in ast.walk(module):
            if not isinstance(node, ast.Call) or _call_name(node) != "stream":
                continue
            receiver = _receiver_name(node)
            if receiver is not None and "graph" in receiver.lower():
                offenders.add(f"{_tree_relative(path)}:{node.lineno}")
    assert not offenders, (
        f"async 路由里必须用 await graph.astream(...)，禁止同步 graph.stream(...)，"
        f"违例：{sorted(offenders)}"
    )


def test_chromadb_whitelist_is_not_widened() -> None:
    """SPEC §5.16 #32：`chromadb` 的 import 白名单**只减不增**。

    阶段六一度想把 `Collection` 注入 `agent/tools/docs.py`；后来改成注入
    `Searcher`/`Fetcher` 可调用对象（§5.3），`agent/` 对 Chroma 零知识。
    这条冻结常量本身：#24 的扫描只保证"没越界"，越界的门是这道 `|=` 式的修改。
    """
    assert CHROMADB_ALLOWED == EXPECTED_CHROMADB_ALLOWED, (
        "chromadb 白名单不得放宽（只允许 rag/store.py、rag/ingest.py、rag/retriever.py）"
    )

    offenders: set[str] = set()
    for path, module in _python_sources():
        if path in CHROMADB_ALLOWED:
            continue
        for module_name, _lineno in _imported_modules(module):
            if module_name == "chromadb" or module_name.startswith("chromadb."):
                offenders.add(_tree_relative(path))
    assert not offenders, f"chromadb 白名单之外出现 import，违例：{sorted(offenders)}"


def test_qa_context_template_never_loaded_via_load_prompt() -> None:
    """SPEC §5.16 #33：`qa_context.jinja2` 只能经 `_context_template()` 读（L17）。

    `load_prompt()` 强制 `system` 角色 + 必填 `history`：套在 `qa_context` 上会
    静默产出**第二条 system**，插进历史中间 —— 前缀缓存破裂且不报错。
    真正兜底的是 `test_graph_qa` 的消息序列断言，这条只是让违规在 AST 层就红。
    """
    module = _parse(QA_GRAPH_PATH)
    assert module is not None, "agent/graphs/qa.py 应存在且可解析"

    offenders: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or _call_name(node) != "load_prompt":
            continue
        args = [*node.args, *(keyword.value for keyword in node.keywords)]
        for arg in args:
            literal = isinstance(arg, ast.Constant) and arg.value == QA_CONTEXT_PROMPT_NAME
            named = isinstance(arg, ast.Name) and arg.id == "QA_CONTEXT_PROMPT_NAME"
            if literal or named:
                offenders.append(f"{_tree_relative(QA_GRAPH_PATH)}:{node.lineno}")

    assert not offenders, (
        f"qa_context 模板不得经 load_prompt() 加载（用 _context_template()），"
        f"违例：{sorted(offenders)}"
    )


# ---------- 阶段七追加（SPEC §5.16 #34–#37）----------
SANDBOX_DIR = REPO_ROOT / "src" / "pyassist" / "sandbox"
SANDBOX_RUNNER_PATH = SANDBOX_DIR / "runner.py"
CONFIG_PATH = REPO_ROOT / "src" / "pyassist" / "config.py"
DIAGNOSE_GRAPH_PATH = GRAPHS_DIR / "diagnose.py"

# #37：human 角色的诊断模板。它们只能经 diagnose.py 的本地 loader 读取。
DIAGNOSE_TEMPLATE_NAMES = frozenset({"diagnose_context", "diagnose_repair"})
DIAGNOSE_TEMPLATE_CONSTANTS = frozenset(
    {"DIAGNOSE_CONTEXT_PROMPT_NAME", "DIAGNOSE_REPAIR_PROMPT_NAME"}
)


def test_docker_imported_only_by_sandbox_runner() -> None:
    """#34：`docker` 的 import 只在 `sandbox/runner.py`（§2「执行用户代码」唯一归属）。

    任何别处拿到 docker SDK，就意味着存在第二条执行用户代码的路径 ——
    而那条路径不会带上 runner 的隔离参数表。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == SANDBOX_RUNNER_PATH:
            continue
        for module_name, _lineno in _imported_modules(module):
            if module_name == "docker" or module_name.startswith("docker."):
                offenders.add(_tree_relative(path))
    assert not offenders, f"docker 只允许在 sandbox/runner.py import，违例：{sorted(offenders)}"


def test_containers_created_only_by_sandbox_runner() -> None:
    """#35：`containers.create(...)` / `containers.run(...)` 只在 `sandbox/runner.py`。

    防的是"第二轮沙箱实现"：service 里顺手 `client.containers.run(...)`
    会绕过 `_create_kwargs()` 的整张隔离参数表，而且测试完全看得见却没人拦。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == SANDBOX_RUNNER_PATH:
            continue
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) not in {"create", "run"}:
                continue
            if _receiver_name(node) == "containers":
                offenders.add(f"{_tree_relative(path)}:{node.lineno}")
    assert not offenders, f"容器只允许在 sandbox/runner.py 创建，违例：{sorted(offenders)}"


def test_sandbox_limits_constructed_only_in_config() -> None:
    """#36：`SandboxLimits(...)` 的构造只在 `config.py`。

    限额是安全边界的一部分。若某条路径能自己构造一份 `SandboxLimits`，
    它就能悄悄把 `is_network_enabled=True` / `memory_mb=8192` 带进容器，
    而 runner 仍然"按配置"执行 —— 配置只有一个来源，边界才只有一个。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == CONFIG_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) == "SandboxLimits":
                offenders.add(f"{_tree_relative(path)}:{node.lineno}")
    assert not offenders, f"SandboxLimits 只允许在 config.py 构造，违例：{sorted(offenders)}"


def test_diagnose_human_templates_never_loaded_via_load_prompt() -> None:
    """#37：`diagnose_context` / `diagnose_repair` 不得经 `load_prompt()` 加载。

    与 #33 同理：`load_prompt()` 产出**第二条 system**，它会插在工具轮历史中间 ——
    前缀缓存破裂、"system 恒在最前"同时失效，而且运行时不报错。
    """
    module = _parse(DIAGNOSE_GRAPH_PATH)
    assert module is not None, "agent/graphs/diagnose.py 应存在且可解析"

    offenders: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or _call_name(node) != "load_prompt":
            continue
        args = [*node.args, *(keyword.value for keyword in node.keywords)]
        for arg in args:
            literal = isinstance(arg, ast.Constant) and arg.value in DIAGNOSE_TEMPLATE_NAMES
            named = isinstance(arg, ast.Name) and arg.id in DIAGNOSE_TEMPLATE_CONSTANTS
            if literal or named:
                offenders.append(f"{_tree_relative(DIAGNOSE_GRAPH_PATH)}:{node.lineno}")

    assert not offenders, (
        f"diagnose_context / diagnose_repair 不得经 load_prompt() 加载，违例：{sorted(offenders)}"
    )


# ---------- 阶段八追加（SPEC §5.20 #38–#45）----------
FRONTEND_DIR = REPO_ROOT / "frontend"
FRONTEND_CLIENT_PATH = FRONTEND_DIR / "api_client.py"
ROUTERS_DIR = REPO_ROOT / "src" / "pyassist" / "api" / "routers"
GRADING_PATH = REPO_ROOT / "src" / "pyassist" / "agent" / "tools" / "grading.py"
USAGE_SERVICE_PATH = REPO_ROOT / "src" / "pyassist" / "services" / "usage.py"
DB_MODELS_PATH = REPO_ROOT / "src" / "pyassist" / "db" / "models.py"

# 不调模型、只读业务表的 router。新增豁免必须改这里，评审能看见。
QUOTA_EXEMPT_ROUTERS = frozenset({"history", "stats"})
FROZEN_TABLES = frozenset({"users", "sessions", "exercise_attempts", "mistakes", "token_ledger"})
EXERCISE_DATA_FIELDS = frozenset({"exercise_id", "topic", "title", "prompt", "starter_code"})


def _iter_python_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    yield from sorted(root.glob("*.py"))


def test_frontend_never_imports_pyassist_guardrail() -> None:
    """#38：前端 AST 里没有 `pyassist` 导入，也没有动态导入。

    与 `test_frontend_contract.py` 的同名断言重复是**故意的**：守护清单要能
    单独跑一遍就看到全部 #38–#45，不必记"哪条在哪个文件"。
    """
    offenders: list[str] = []
    for path in _iter_python_files(FRONTEND_DIR):
        module = _parse(path)
        if module is None:
            offenders.append(f"{_tree_relative(path)}: 无法解析")
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{_tree_relative(path)}: {alias.name}"
                    for alias in node.names
                    if alias.name == "pyassist" or alias.name.startswith("pyassist.")
                )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module == "pyassist" or node.module.startswith("pyassist."):
                    offenders.append(f"{_tree_relative(path)}: from {node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                is_dynamic = (isinstance(func, ast.Name) and func.id == "__import__") or (
                    isinstance(func, ast.Attribute) and func.attr == "import_module"
                )
                if is_dynamic:
                    offenders.append(f"{_tree_relative(path)}: 动态导入")
    assert not offenders, f"前端不得 import pyassist：{sorted(offenders)}"


def test_exercise_data_never_exposes_tests_or_solution() -> None:
    """#39：`ExerciseData` 字段集合**恰好**五项（SPEC D2）。

    多出 `tests` / `reference_solution` 就是把隐藏测试下发给客户端，
    用户照着断言写死值即可满分。
    """
    from pyassist.api.schemas import ExerciseData

    assert set(ExerciseData.model_fields) == EXERCISE_DATA_FIELDS


def test_score_logic_lives_only_in_grading_tool() -> None:
    """#40：`score_of` / `is_passed` 的实现只在 `agent/tools/grading.py`（D5）。

    service/router 若自己再判一次"过没过"，两份判定迟早不一致，
    而报表只会展示其中一个。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == GRADING_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.FunctionDef) and node.name in {"score_of", "is_passed"}:
                offenders.add(f"{_tree_relative(path)}:{node.lineno}")
    assert not offenders, f"分数/通过判定只允许在 grading.py 实现，违例：{sorted(offenders)}"


def test_test_result_marker_has_a_single_protocol() -> None:
    """#41：`TEST_RESULT_MARKER` 只出现在 grading.py、config.py 与 tests/。

    config.py 是常量的家（唯一来源），grading.py 是唯一消费者；别处出现
    就意味着第二个结果协议正在长出来。
    """
    allowed = {GRADING_PATH, CONFIG_PATH}
    offenders: set[str] = set()
    for path in _iter_tree_files(REPO_ROOT / "src"):
        if path.suffix != ".py" or path in allowed:
            continue
        text = _read_text(path) or ""
        if "TEST_RESULT_MARKER" in text:
            offenders.add(_tree_relative(path))
    assert not offenders, (
        f"TEST_RESULT_MARKER 只允许在 grading.py / config.py / tests 中出现，"
        f"违例：{sorted(offenders)}"
    )


def test_every_model_calling_router_requires_quota() -> None:
    """#42：除 history/stats 外，每个 router 都必须挂 `Depends(require_quota)`（D8）。

    新路由漏挂配额不会报错，只会让成本控制静默失效 —— 所以用结构性断言兜住。
    """
    offenders: list[str] = []
    for path in _iter_python_files(ROUTERS_DIR):
        if path.stem in QUOTA_EXEMPT_ROUTERS:
            continue
        text = _read_text(path) or ""
        if "APIRouter(" not in text:
            continue
        if "Depends(require_quota)" not in text:
            offenders.append(_tree_relative(path))
    assert not offenders, f"会调模型的 router 必须挂 require_quota，违例：{sorted(offenders)}"


def test_token_ledger_written_only_by_usage_service() -> None:
    """#43：`append_token_usage(...)` 的调用点只允许 `services/usage.py`（D8）。

    账本是配额的唯一依据。旁路写一笔，`/stats` 与限流就会各说各话。
    """
    offenders: set[str] = set()
    for path, module in _python_sources():
        if path == USAGE_SERVICE_PATH:
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.Call) and _call_name(node) == "append_token_usage":
                offenders.add(f"{_tree_relative(path)}:{node.lineno}")
    assert not offenders, f"账本只允许经 services/usage.py 写入，违例：{sorted(offenders)}"


def test_frontend_paths_are_in_openapi_guardrail() -> None:
    """#44：`frontend/api_client.py` 里的路径字面量 ⊆ `app.openapi()["paths"]`（D11）。

    这是运行时断言，不是 grep：导入 app 才能证明路由真的挂上了。
    """
    from pyassist.api.main import create_app

    module = _parse(FRONTEND_CLIENT_PATH)
    assert module is not None, "frontend/api_client.py 应存在且可解析"
    literals = {
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/")
        and node.value != "/"
    }
    app = create_app(
        database_url="sqlite+aiosqlite:///:memory:",
        rate_limit_enabled=False,
    )
    openapi_paths = set(app.openapi()["paths"])

    assert literals, "api_client.py 里应至少有 health 路径"
    assert literals <= openapi_paths, f"前端路径不在 OpenAPI 中：{sorted(literals - openapi_paths)}"


def test_database_tables_are_frozen() -> None:
    """#45：`db/models.py` 的 `__tablename__` 集合冻结为现有五张表（§1.2）。

    新增表需要走契约评审；偷偷加表通常意味着"第二份对话历史"或"第二套账本"。
    """
    module = _parse(DB_MODELS_PATH)
    assert module is not None, "db/models.py 应存在且可解析"
    tables: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            targets = {target.id for target in statement.targets if isinstance(target, ast.Name)}
            if "__tablename__" not in targets:
                continue
            value = statement.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                tables.add(value.value)
    assert tables == FROZEN_TABLES, (
        f"表集合已偏离冻结清单：多出 {sorted(tables - FROZEN_TABLES)}，"
        f"缺少 {sorted(FROZEN_TABLES - tables)}"
    )
