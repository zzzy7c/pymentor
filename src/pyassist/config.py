"""唯一配置入口（§2）。其他模块一律从此处取配置，不得另建 settings 模块。

分工（SPEC §4 T4）：
- 随代码评审变更的（模型 ID、价格、沙箱上限）是模块常量；
- 随部署环境变更的（API key、DB URL、日志级别）进 `Settings`。
二者都在本文件，仍然只有一个配置入口。
"""

from __future__ import annotations

from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from pyassist.errors import ConfigError

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DATA_DIR: Final = REPO_ROOT / "data"

# ---------- 模型 ID：全项目只允许出现在这里（§1 / §2）----------
MODEL_FLASH: Final = "deepseek-flash"
MODEL_PRO: Final = "deepseek-v4-pro"
EXPECTED_MODEL_IDS: Final[frozenset[str]] = frozenset({MODEL_FLASH, MODEL_PRO})

# ---------- 端点 ----------
DEEPSEEK_API_BASE_URL: Final = "https://api.deepseek.com"


# ---------- 单价表：USD / 1M tokens（§1）。仅用于量级判断与成本告警 ----------
class PriceTier(BaseModel):
    """单模型六个单价。显式存峰值/非峰值两列，避免在计价处做浮点乘法。"""

    cache_hit_off_peak: Decimal
    cache_hit_peak: Decimal
    input_miss_off_peak: Decimal
    input_miss_peak: Decimal
    output_off_peak: Decimal
    output_peak: Decimal


# 数值来自 AGENTS.md §1 价格表；以官网定价页为准，改动只在这里。
PRICE_TABLE: Final[dict[str, PriceTier]] = {
    MODEL_FLASH: PriceTier(
        cache_hit_off_peak=Decimal("0.003"),
        cache_hit_peak=Decimal("0.006"),
        input_miss_off_peak=Decimal("0.15"),
        input_miss_peak=Decimal("0.30"),
        output_off_peak=Decimal("0.60"),
        output_peak=Decimal("1.20"),
    ),
    MODEL_PRO: PriceTier(
        cache_hit_off_peak=Decimal("0.022"),
        cache_hit_peak=Decimal("0.044"),
        input_miss_off_peak=Decimal("0.66"),
        input_miss_peak=Decimal("1.32"),
        output_off_peak=Decimal("1.98"),
        output_peak=Decimal("3.96"),
    ),
}

# ---------- 错峰窗口：UTC 周一至周五（§1）----------
# 纯数据。判断"现在是否错峰"的函数属于记账逻辑，放阶段二 llm/tracing.py。
PEAK_WINDOWS_UTC: Final[tuple[tuple[dt_time, dt_time], ...]] = (
    (dt_time(1, 0), dt_time(4, 0)),
    (dt_time(6, 0), dt_time(10, 0)),
)


# ---------- 沙箱上限（§5.3）。数值待实测校准，见 SPEC 待定项 4 ----------
class SandboxLimits(BaseModel):
    timeout_seconds: int = 10
    memory_mb: int = 512
    cpus: float = 1.0
    output_head_lines: int = 100  # 超长输出截断：保留头尾（§2 检查点膨胀）
    output_tail_lines: int = 100
    is_network_enabled: bool = False
    is_rootfs_read_only: bool = True
    is_non_root: bool = True
    # ---------- 阶段七新增（SPEC §5.1）。既有字段一个都不删，实例仍然只有一个 ----------
    pids_limit: int = 64  # fork 炸弹的硬闸门
    tmpfs_size_mb: int = 8  # /tmp：只读根文件系统下唯一可写处
    max_code_bytes: int = 65_536  # 进容器前的拒绝线（status="rejected"）
    max_output_chars: int = 12_000  # 截断后的字符上限（检查点膨胀的第二道闸）


SANDBOX_LIMITS: Final = SandboxLimits()


# ---------- RAG（阶段五，§1 / §5.2）----------
CORPUS_DIR: Final = DATA_DIR / "corpus"
CHROMA_DIR: Final = DATA_DIR / "chroma"

COLLECTION_NAME: Final = "python_docs"
DISTANCE_SPACE: Final[Literal["cosine"]] = "cosine"
"""C5/C7：不显式指定就是 l2，`score = 1 - distance` 会**静默**变成垃圾。

注解成 `Literal["cosine"]` 而非裸 `str`：chromadb 的 `Space` 就是
`Literal["cosine", "l2", "ip"]`，这样 `rag/store.py` 里那句 `configuration=` 能直接
过 mypy，不必用 `cast` 或 `type: ignore` 抹平（判据 18 不许后者）。
"""

HNSW_SYNC_THRESHOLD: Final = 1_000_000
"""累积这么多条就把**整个 HNSW 索引重写一遍**到磁盘。Chroma 的默认值是 1000。

本项目把它抬到不可能触发的高度，因为它正是 2026-10-13 那次事故的开关：

> 全量 ingest（17366 条 / 1024 维 / 4.8 小时）跑完打印了 `ingest.done`，
> 但重开集合报 `Error loading hnsw index`。落盘现场是：段目录里只有一个
> `index_metadata.pickle`（`total_elements_added: 16384`），HNSW 的 `*.bin` 一个都没有，
> 而 WAL 已被清到 16384 之前。**前 16384 条向量只存在于那个已退出进程的内存里。**

抬高阈值的收益不是"少写几次盘"，而是**换一种失败模式**：

| | 阈值 1000（默认） | 阈值 1e6（本项目） |
|---|---|---|
| 写入期间 | 每 1000 条一次全量重写，每次都是一次"写坏"的机会 | 只写 WAL（事务性），索引不落盘 |
| 写坏后 | WAL 已被清空 → **不可恢复，只能重新嵌入几小时** | WAL 完好，现场是"没有索引文件" |
| 恢复 | 无 | 删掉那个段目录，读路径会从 WAL 现场重建 |

所以本项目的持久层是 **WAL**，HNSW 索引只是它的缓存。实测见
`experiments/repro_index_durability.py`（硬退出后重开仍是 count=20000 且检索完整）。
**这条一旦被改回小值，上面那个"不可恢复"的失败模式就回来了。**
"""

HNSW_BATCH_SIZE: Final = 100
"""索引写入器的批大小。Chroma 硬性要求 `batch_size <= sync_threshold` ——
显式写出来是为了让这条约束在配置里可见（把 `HNSW_SYNC_THRESHOLD` 改小到 100 以下会立刻报错）。"""

DEFAULT_TOP_K: Final = 5  # §5.2 的 Recall@5
RETRIEVAL_MIN_SCORE: Final = 0.0
"""不过滤。阈值必须等阶段六跑分后回填（SPEC §10.5），现在瞎猜只会把召回压没。"""

CHUNK_MAX_CHARS: Final = 1000
CHUNK_OVERLAP_CHARS: Final = 150
CHUNKER_VERSION: Final = "2"
"""分块算法一改就 bump：它进索引元数据，是"库里混着两代切法"的判定依据（SPEC D7）。

**"2" 的由来**（2026-10-13）：`"1"` 只对 `<pre>` 做超长拆分，正文块（`<p>`/`<li>`/`<dt>`/`<dd>`）
没有上界 —— Python 官方文档里最大的一块是 47723 字符（`contents.html` 的嵌套 `<li>` 被
`get_text(" ")` 整棵子树展平）。这类块既产出垃圾向量，又让嵌入期的注意力掩码
（内存 O(batch × seq²)）引爆 OOM。`"2"` 起所有块都在聚合前受 `CHUNK_MAX_CHARS` 约束。
"""

# 单条文本送进嵌入模型的 token 上限。**这是内存的硬闸门，不是调优旋钮。**
#
# 注意力掩码的内存是 O(batch × seq²)：bge-m3 自带的 `max_seq_length` 是 8192，
# 配 `ingest` 的 batch 就是 256 × 8192² × 4B ≈ 68GB —— 必然 OOM（2026-10-13 实测崩在
# `_prepare_4d_attention_mask_for_sdpa`，`not enough memory: 4899708032 bytes`）。
#
# 1024 的依据（本机 3.12 文档全量实测，修复分块之后）：
# - chunk 长度分布 p50=254 / p90=336 / **max=860** token；
# - 512 会截断 48 条，1024 一条都不截 —— 所以取 1024，让闸门只挡事故、不挡正常数据。
EMBEDDING_MAX_SEQ_TOKENS: Final = 1024

# 向量模型：DeepSeek 不提供 embedding 接口（§1），故必须另选。
# 换模型 = 改这一行 + 删 data/chroma 重跑 ingest（§4「重建 = 删目录」）。
DEFAULT_EMBEDDING_MODEL_ID: Final = "BAAI/bge-m3"

# 语料来源（SPEC §2.4 / §2.5）。版本锁定 3.12：与 §1 的运行时一致，评估可复现。
DOCS_VERSION: Final = "3.12"
DOCS_BASE_URL: Final = f"https://docs.python.org/{DOCS_VERSION}/"
DOCS_DOWNLOAD_PAGE_URL: Final = f"{DOCS_BASE_URL}download.html"
# 两步解析的回退项（SPEC §2.4）。T2 已核实：该 URL 返回 200 / application/zip / 51.7MB
# （2026-10-13 实测）。但上游改版不可控，故 fetch 仍先读 download.html 抽链接。
DOCS_ARCHIVE_URL_FALLBACK: Final = f"{DOCS_BASE_URL}archives/python-{DOCS_VERSION}-docs-html.zip"

PEPS_BASE_URL: Final = "https://peps.python.org/"
PEP_INDEX_URL: Final = f"{PEPS_BASE_URL}api/peps.json"
PEP_NUMBERS: Final = (8, 20, 257, 484, 585, 604, 634, 695)
"""固定清单：覆盖"风格 + 类型系统 + match 语法"三个与学习强相关的方向（SPEC §10.4）。"""

CORPUS_EXCLUDE_DIRS: Final = frozenset({"_sources", "_static", "_modules", "_images"})
CORPUS_EXCLUDE_FILES: Final = frozenset(
    {"genindex.html", "py-modindex.html", "search.html", "404.html"}
)

# 抓取与嵌入都是"机器占用实打实"的长任务：显式超时，别让一次网络抖动挂死一个 CLI。
CORPUS_FETCH_TIMEOUT_SECONDS: Final = 60.0

# 评估集（§4 冻结件：只能新增，禁止修改已有用例）
EVAL_DATASETS_DIR: Final = REPO_ROOT / "src" / "pyassist" / "evals" / "datasets"
QA_DATASET_PATH: Final = EVAL_DATASETS_DIR / "qa.jsonl"
EXERCISES_DATASET_PATH: Final = EVAL_DATASETS_DIR / "exercises.jsonl"
"""v3 夹具（SPEC §5.18）。**只增不改**：改了答案再跑分，等于改考卷给自己判分。"""


# ---------- 阶段六：图与流式（SPEC §5.1）----------
SSE_HEARTBEAT_SECONDS: Final = 15
"""心跳间隔。AGENTS.md §2 点名"每 ~15 秒一次"，故它是**契约值**，不是调优旋钮。
测试把它 monkeypatch 到 0.05 秒（库自带心跳带时间戳、不可断言，所以必须自己发）。"""

MAX_TOOL_ROUNDS: Final = 3
"""一次问答里最多几轮工具调用。到顶就 finalize，并在答案末尾声明"可能有未覆盖内容"。
取 3 而不是 5：`retrieve` 已经先检索过一次，再 3 轮已覆盖"再查一次 / 读整页 / 补一次"。"""

MAX_HISTORY_MESSAGES: Final = 12
"""喂给模型的历史**组数**上限（一组 = 一条 HumanMessage 及其后的 AI / Tool 消息）。
按组裁剪而不是按条裁剪，是为了绝不把 `AIMessage(tool_calls)` 与它的 `ToolMessage` 拆开（D8）。"""

MAX_GROUNDING_RETRIES: Final = 0
"""引用越界时的重生成次数。本阶段取 0（只剥标记，不重生成），常量留给阶段七的诊断图（D4）。"""

READ_DOC_MAX_CHARS: Final = 8_000
"""`read_doc` 回给模型的整页文本上限。一页文档几万字符，全塞进上下文会顶掉检索片段，
也会让检查点膨胀（AGENTS.md §2）。超限截断并声明"已截断"。"""

QA_PROMPT_NAME: Final = "qa"
QA_CONTEXT_PROMPT_NAME: Final = "qa_context"

OUTPUT_LANGUAGE: Final = "中文"
"""`qa.jinja2` 末行 `回答语言：{{ output_language }}` 的取值。**只在这里写一次** ——
测试里一直传 `"中文"`，但生产路径此前没有来源；阶段六的图第一次真正渲染这个模板。"""


# ---------- 阶段七：沙箱 + v2 错误诊断（SPEC §5.1）----------
SANDBOX_IMAGE: Final = "pyassist-sandbox:1"
"""沙箱镜像 tag。**必须与 `docker-compose.yml` 的 `image:` 一字不差**（守护 #35 只认住址，
不认"看起来一样"）。构建命令写在 `sandbox/Dockerfile.sandbox` 顶部注释里。"""

SANDBOX_POLL_INTERVAL_SECONDS: Final = 0.05
"""轮询容器状态的间隔。**不用 `container.wait(timeout=)`**：它在超时后会把容器留在
running 状态，而我们要的是"杀掉并拿到日志"（SPEC D2）。"""

DIAGNOSE_MODEL_TIER: Final[Literal["pro"]] = "pro"
"""诊断走强推理档（D11）。图里只认 `tier`，真正映射到模型 ID 的只有 `llm/client.py`。"""

MAX_REPAIR_ATTEMPTS: Final = 2
"""修复-验证循环的上限。到顶即 finalize，把最后一次失败结果如实返回。"""

QA_TOOL_NAMES: Final[tuple[str, ...]] = ("search_docs", "read_doc")
DIAGNOSE_TOOL_NAMES: Final[tuple[str, ...]] = (
    "search_docs",
    "read_doc",
    "explain_error",
    "run_code",
)
"""两张图各自能看到的工具子集（D7）。**工具定义仍然只有 `agent/registry.py` 一处** ——
这里只是"允许清单"，`tool_specs(names)` 从同一份定义里筛。"""

DIAGNOSE_PROMPT_NAME: Final = "diagnose"
DIAGNOSE_CONTEXT_PROMPT_NAME: Final = "diagnose_context"
DIAGNOSE_REPAIR_PROMPT_NAME: Final = "diagnose_repair"
"""后两个是 **human 角色**模板，只能经 `ChatPromptTemplate.from_messages` 加载，
不走 `load_prompt()`（后者强制要求 system + history 占位，见守护 #37）。"""

# 评估集（§4 冻结件：只能新增，禁止修改已有用例）
ERRORS_DATASET_PATH: Final = EVAL_DATASETS_DIR / "errors.jsonl"
MIN_ERROR_RECORDS: Final = 20
ERROR_TYPE_ACCURACY_TARGET: Final = 0.85
FIX_PASS_RATE_TARGET: Final = 0.70


# ---------- 阶段八：v3 / v4 / 配额（SPEC §5.1）----------
EXERCISE_TOPICS: Final[tuple[Literal["list", "dict", "decorator", "async"], ...]] = (
    "list",
    "dict",
    "decorator",
    "async",
)
"""四个练习主题（AGENTS.md §5.4）。枚举只在这里定义，图与路由都从这里取。"""

MAX_EXERCISE_ATTEMPTS: Final = 2
"""生成自校验回环上限。到顶仍不过就返回 502，不下发坏题（SPEC D1 / R2）。"""

EXERCISE_MODEL_TIER: Final[Literal["pro"]] = "pro"
GRADE_MODEL_TIER: Final[Literal["flash"]] = "flash"
PROGRESS_MODEL_TIER: Final[Literal["pro"]] = "pro"

EXERCISE_PROMPT_NAME: Final = "exercise"
EXERCISE_CONTEXT_PROMPT_NAME: Final = "exercise_context"
GRADE_PROMPT_NAME: Final = "grade"
GRADE_CONTEXT_PROMPT_NAME: Final = "grade_context"
PROGRESS_PROMPT_NAME: Final = "progress"
PROGRESS_CONTEXT_PROMPT_NAME: Final = "progress_context"
"""后三个 `*_context` 是 **human 角色**模板，只能经各自图里的本地 loader 加载（同 #37）。"""

EXERCISE_TOOL_NAMES: Final[tuple[str, ...]] = (
    "generate_exercise",
    "run_tests",
    "grade_code",
)
PROGRESS_TOOL_NAMES: Final[tuple[str, ...]] = ("recommend_next",)
"""两张新图的允许清单。工具定义仍只在 `agent/registry.py`（D7 沿用）。"""

DEFAULT_USER_ID: Final = "local"
"""阶段八没有鉴权（D9）。README 必须写明这不是安全边界。"""

MAX_SESSION_ID_CHARS: Final = 32
"""`sessions.id` 的列宽。契约允许 64，落库前经 `db_session_id` 归一（D9）。"""

HISTORY_PAGE_SIZE: Final = 20
MAX_HISTORY_PAGE_SIZE: Final = 100

DEFAULT_DAILY_TOKEN_LIMIT: Final = 500_000
"""`Settings.daily_token_limit` 为 `None` 时用它；显式设 `0` = 关闭熔断（仅本地调试）。"""

ERROR_SIGNATURE_MAX_CHARS: Final = 256
MAX_EXERCISE_CODE_CHARS: Final = 8_000
MAX_TESTS_PER_EXERCISE: Final = 12
MAX_TEST_METHOD_CHARS: Final = 4_000
MAX_EXERCISE_PROMPT_CHARS: Final = 6_000
MAX_STARTER_CODE_CHARS: Final = 2_000
MAX_REFERENCE_SOLUTION_CHARS: Final = 8_000
MISTAKE_LIST_LIMIT: Final = 50
PROFILE_NAMESPACE: Final = "profiles"
PROFILE_SUMMARY_KEY: Final = "summary"
PROFILE_RECENT_EXERCISE_LIMIT: Final = 5

GRADE_DOC_TOP_K: Final = 3
GRADE_MAX_DOC_QUERIES: Final = 2
"""批改阶段的确定性检索口径（SPEC §5.6）。模型只给 query，不给 URL。"""

TEST_RESULT_MARKER: Final = "__PYASSIST_RESULT__"
"""测试结果协议的唯一标记（SPEC §5.3）。只允许出现在 `grading.py` 与测试里（守护 #41）。"""


# ---------- 数据库 ----------
# 唯一的库文件路径常量。检查点、业务表、Store 都指向它（SPEC §2）。
DB_PATH: Final = DATA_DIR / "app.db"

# 同一路径的两种连接串：SQLAlchemy 用 URL，LangGraph 的 saver 用裸路径。
# 二者都由 DB_PATH 派生，避免"两个路径常量早晚不一致"。

SQLITE_BUSY_TIMEOUT_SECONDS: Final = 30
"""SQLite 连接级忙等上限。

同一个 `app.db` 会被**两条独立连接**写（SQLAlchemy 一条、检查点一条），
没有它会立刻 `database is locked`。`journal_mode=WAL` 不在这里设 ——
它是**库文件级持久属性**，由 `AsyncSqliteSaver.setup()` 设一次即全库生效。
"""


def build_engine_kwargs(database_url: str) -> dict[str, Any]:
    """按 URL 方言给出引擎参数。**全项目唯一按方言分流的地方。**

    `db/session.py` 只做 `create_async_engine(url, **build_engine_kwargs(url))`，
    因此它不含任何方言字样（T7 有守护断言）。这不是"业务逻辑进 config"，
    而是"把配置本身读对"——三个函数都是 `settings.database_url` 的纯派生。
    """
    backend = make_url(database_url).get_backend_name()
    if backend == "sqlite":
        return {"connect_args": {"timeout": SQLITE_BUSY_TIMEOUT_SECONDS}}
    return {}


def resolve_sqlite_path(database_url: str) -> Path:
    """取 SQLite 库文件的**文件系统路径**（`AsyncSqliteSaver` 要裸路径，不是 URL）。

    非 SQLite 后端直接报错：这个函数只在 SQLite 分支上被调用，
    调用方（`agent/checkpointer.py` / `agent/store.py`）负责先判断后端。
    """
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        # 服务级配置错误（库路径解析不了）→ ConfigError（阶段四 T7 收口，SPEC 待定项 11）。
        raise ConfigError(f"resolve_sqlite_path 只适用于 SQLite，当前是 {url.database!r}")
    if not url.database:
        raise ConfigError("DATABASE_URL 里没有库路径")
    return Path(url.database)


def to_psycopg_conninfo(database_url: str) -> str:
    """把 SQLAlchemy 的 PG URL 转成 psycopg 的 conninfo（生产分支用）。

    两个构造点（检查点、Store）都要这一句，所以只能有一个家 —— 这里是唯一实现。
    """
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


# ---------- 环境相关配置 ----------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 缺 key 不得让 `import pyassist` 失败：导入期不校验，阶段二 llm/client.py 取模型时才报错。
    deepseek_api_key: SecretStr | None = None
    deepseek_api_base_url: str = DEEPSEEK_API_BASE_URL
    # 绝对路径，避免阶段三的"检查点目标 == data/app.db"断言随 CWD 漂移。
    # `.as_posix()`：URL 里出现 `\` 是自找麻烦；`make_url(...).database` 会原样回读。
    database_url: str = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"
    log_level: str = "INFO"
    log_json: bool = True
    # 阶段五接上：默认值引用常量，不再允许 None（None 会让每次调用都要判空）。
    embedding_model_id: str = Field(default=DEFAULT_EMBEDDING_MODEL_ID, min_length=1)
    daily_token_limit: int | None = None  # ROADMAP 待定项 5


settings = Settings()
