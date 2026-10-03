# SPEC.md — 阶段三 · 持久化与状态双源 · 实施规格

本文件是 **ROADMAP.md「阶段三」的施工细则**，回答"这一阶段怎么建、建成什么样才算完"。

- `AGENTS.md` 是**契约**（不可协商）。本文件不重新定义契约，只把它翻译成可执行的任务。
- `ROADMAP.md` 是**施工顺序**。本文件的每个任务都能追到 ROADMAP「阶段三」的某条产出。
- 三者冲突时以 `AGENTS.md` 为准；本文件与 `AGENTS.md` 冲突，说明本文件写错了。
- 阶段一、阶段二的规格已归档到 `docs/specs/阶段一-工程底座.md`、`docs/specs/阶段二-LLM基座.md`。

**阶段三不交付版本，交付"对话历史只有一个家"的边界。** 验收标准是**程序化**的：不是"我们约定检查点指向 `data/app.db`"，而是开一条真实连接、问数据库自己"你是哪个文件"。

---

## 0. 范围边界

### 做

- `config.py`：新增 `DB_PATH` 与三个 URL 派生函数（`build_engine_kwargs` / `resolve_sqlite_path` / `to_psycopg_conninfo`）；`settings.database_url` 默认值切到异步驱动。
- `db/session.py`：引擎、Session 工厂、Session 依赖；**零方言字样**。
- `db/models.py`：五张业务表（`users` / `sessions` / `exercise_attempts` / `mistakes` / `token_ledger`），**没有 `messages` 表**。
- `db/repositories.py`：★ 唯一 SQL 出口，含幂等写（去重键）。
- `agent/checkpointer.py`：★ 唯一检查点构造点（`AsyncSqliteSaver` / `AsyncPostgresSaver`）。
- `agent/store.py`：★ 唯一长期记忆 Store 构造点（`AsyncSqliteStore` / `AsyncPostgresStore`）。
- `alembic.ini` + `migrations/`：建业务表；LangGraph 的地表由迁移**流程**（`env.py`）建一次。
- `tests/test_state_ownership.py`、`tests/test_idempotency.py`，以及 `tests/test_contract_guardrails.py` 的守护断言扩展。

### 不做（明确排除，避免"顺手加一下"）

| 不建 | 理由 |
|---|---|
| `agent/state.py` / `agent/registry.py` / `agent/graphs/*` | 阶段六。本阶段只保证"图一旦开建，落库这一步已经是对的"。`test_idempotency.py` 里的图是**测试夹具**，不在 `src/` |
| `api/*`（含 `/health`、`api/deps.py`） | 阶段四。本阶段的构造点是 `@asynccontextmanager`，阶段四的 `lifespan` 进来即可 |
| `services/*` | 阶段四起。本阶段只出仓储与构造点 |
| `errors.py` | 阶段四（§3.2 异常树）。本阶段参数错误抛 `ValueError`，阶段四统一换掉，**不留 TODO 占位** |
| `rag/*` / `sandbox/*` | 阶段五、阶段七 |
| 记忆抽取逻辑（"从对话里抽偏好"） | 阶段八的 `services/memory.py`。本阶段只建 Store 构造点 |
| Token 累计与单日熔断 | 阶段八（§5.5）。本阶段只建 `token_ledger` 表与写函数，**不在任何请求路径调用** |
| 任何 `src/` 下的图、`while` 工具循环 | §2 单点所有权：拼装图只有 `agent/graphs/*.py` 一个家，阶段六才有 |
| **下载新依赖** | 见 §1.4：`aiosqlite` / `sqlite-vec` / `psycopg` 都已是直接依赖的硬 `Requires-Dist`，本阶段**不需要装任何新包**。是否把它们从"传递"提升为直接声明，见「待定项 9」 |
| Postgres 真机验证 | 本机无 PG 实例。分支照写，验收明确排除（照阶段二 `--check-models` 的处理方式） |
| `sentence-transformers` / 向量索引配置 | 向量模型选型是阶段五的待定项 1。本阶段 Store 的 `index=None` |

---

## 1. 前置核查结果（本机实测 / 读 `.venv` 源码，2026-10-03）

### 1.1 已装版本

| 包 | 版本 | 对本阶段的意义 |
|---|---|---|
| `langgraph` | **1.1.10** | 检查点与 Store 的基座；`StateGraph` / `RetryPolicy` / `compile(checkpointer=..., store=...)` |
| `langgraph-checkpoint-sqlite` | **2.0.11** | `AsyncSqliteSaver` 的提供者；**也是 `aiosqlite` 与 `sqlite-vec` 的声明者** |
| `langgraph-checkpoint-postgres` | **2.0.25** | `AsyncPostgresSaver`；声明 `psycopg>=3.2.0` / `psycopg-pool>=3.2.0` |
| `sqlalchemy` | **2.1.3** | 2.0 风格（`select()` / `Mapped` / `DeclarativeBase`） |
| `alembic` | **1.20.0** | 迁移 |
| `aiosqlite` | 0.22.1 | 异步 SQLite 驱动（**已装，但 pyproject 未声明**，见 §1.4） |
| `sqlite_vec` | 0.1.9 | `langgraph.store.sqlite.base` 的**模块级硬导入**（见 §1.3） |
| `psycopg` / `psycopg_pool` | 3.3.6 / 3.3.3 | 生产分支 |

### 1.2 异步类的 API 约束（本阶段的技术地基）

直接读 `.venv` 源码，不凭记忆：

| 事实 | 位置 | 对本阶段的约束 |
|---|---|---|
| `langgraph.checkpoint.sqlite.SqliteSaver` **只支持同步**；调异步方法抛 `_AIO_ERROR_MSG` | `langgraph/checkpoint/sqlite/__init__.py:23` | ROADMAP 写的"`SqliteSaver`"必须落到 `AsyncSqliteSaver`（见 §5「与 ROADMAP 验收的差异」第 1 条） |
| `AsyncSqliteSaver.__init__` 与 `AsyncSqliteStore.__init__` **都调 `asyncio.get_running_loop()`** | `checkpoint/sqlite/aio.py:118`、`store/sqlite/aio.py:105` | **不能**在 import 期造模块级单例 —— 只能在**运行中的事件循环内**构造。这是 `open_checkpointer()` 必须写成 `@asynccontextmanager` 的**硬原因**，不是风格偏好 |
| 四个异步类的 `from_conn_string` 都是 `@asynccontextmanager`：`AsyncSqliteSaver.from_conn_string(conn_string)`；`AsyncSqliteStore.from_conn_string(conn_string, *, index=None, ttl=None)`；`AsyncPostgresSaver.from_conn_string(conn_string, *, pipeline=False, serde=None)` | 同目录 `aio.py` | 构造点的形状已定，照抄 |
| `setup()` **幂等**（`is_setup` 标志 + `CREATE TABLE IF NOT EXISTS`）。`AsyncSqliteSaver.setup()` 内含 `PRAGMA journal_mode=WAL` 并建 `checkpoints` / `writes` | `checkpoint/sqlite/aio.py:273-313` | 迁移里跑一次 + 运行时懒调用**不会撞表**，不必去重 |
| `AsyncPostgresSaver.setup()` 的 docstring 明确要求"首次由用户直接调用" | `checkpoint/postgres/aio.py:82` | 生产分支必须**保证迁移跑过**，不能指望懒调用 |
| `AsyncSqliteStore` 的 `index=None` 时 `self.embeddings = None`，不建向量表 | `store/sqlite/aio.py:89-116` | 本阶段用 `index=None`，向量检索留给阶段八 |

### 1.3 `langgraph.store.sqlite` 硬依赖 `sqlite_vec`（易踩）

`langgraph/store/sqlite/base.py:15` 是**无条件**的模块级 `import sqlite_vec`（带 `# type: ignore[import-untyped]`）。

**后果**：`from langgraph.store.sqlite.aio import AsyncSqliteStore` 在 import 期就要求 `sqlite_vec` 已安装 —— 与是否启用向量检索无关。`sqlite_vec` 0.1.9 已装，且由 `langgraph-checkpoint-sqlite` 的 `Requires-Dist` 声明（见 §1.4）。**本阶段不新增依赖，但要知道这条耦合**：将来若移除 `langgraph-checkpoint-sqlite`，`agent/store.py` 的 import 会先炸。

### 1.4 依赖声明：本阶段不需要装新包（已逐个核实）

逐个查过 dist metadata，以下都是**直接依赖的硬 `Requires-Dist`**，不是"碰巧装上的"：

| 包 | 由谁保证 | 用途 |
|---|---|---|
| `aiosqlite>=0.20` | `langgraph-checkpoint-sqlite` 2.0.11 | `AsyncSqliteSaver` / `AsyncSqliteStore` |
| `sqlite-vec>=0.1.6` | `langgraph-checkpoint-sqlite` 2.0.11 | `langgraph.store.sqlite.base` 的模块级导入（§1.3） |
| `psycopg>=3.2.0` / `psycopg-pool>=3.2.0` | `langgraph-checkpoint-postgres` 2.0.25 | 生产分支 |

~~因此 **本阶段不需要下载任何新包，`uv.lock` 不变**。~~

**实施期实测更正（2026-10-03，施工中推翻上述结论）**：上面这条只对"**这三个包本身**"成立，
对它们的 **extra** 不成立。实际 `uv sync` 新增了两个锁条目，且都是硬需求：

| 新增包 | 由谁带来 | 为什么躲不掉 |
|---|---|---|
| `greenlet>=3.5.6` | `sqlalchemy[asyncio]` 的 extra | `sqlalchemy.ext.asyncio` 在 import 期即要求 greenlet；**没有它 `db/session.py` 直接 ImportError**，本阶段一行都跑不了。原 §1.4 只查了 `Requires-Dist` 的裸依赖，漏了 extra。 |
| `psycopg-binary==3.3.6` | 待定项 9 自己推荐的 `psycopg[binary,pool]` 的 `[binary]` extra | 提供捆绑 libpq（Windows 上用）。原 §1.4 说"psycopg 已在锁里"——那指的是 `psycopg` / `psycopg-pool`，`[binary]` 是额外的一个分发。 |

另外发现一处**上游版本不兼容**（本阶段实际踩到，见「待定项 9」）：
`aiosqlite>=0.22` 起不再继承 `threading.Thread`，`Connection.is_alive()` 随之消失，
而 `langgraph-checkpoint-sqlite` 2.0.11 的 `AsyncSqliteSaver.setup()` 仍在调它 ——
`graph.ainvoke` 首次懒 setup 即 `AttributeError`。故 `aiosqlite` 必须钉 `>=0.20,<0.22`。

不过"装得上"与"该不该声明"是两件事：`db/session.py` 的 `sqlite+aiosqlite` 方言、
`agent/store.py` 经 `langgraph.store.sqlite` 对 `sqlite-vec` 的硬依赖、以及生产分支对
`psycopg` 的直接使用，都是**我们自己的代码直接依赖**的东西，只是此刻由别人替我们声明。
上游哪天换实现，我们不会得到依赖解析错误，只会静默崩。是否把它们从"传递"提升为
`[project].dependencies` 里的直接声明 —— 见「待定项 9」。生产想提速可换 `psycopg[binary]`，属阶段八部署话题。

### 1.5 阶段二留下的闸门与缝（本阶段必须对齐）

| 闸门 / 缝 | 位置 | 对阶段三的约束 |
|---|---|---|
| `ChatDeepSeek(` / `ChatDeepSeekPatched(` 只许在 `llm/client.py`（**集合相等**断言） | `tests/test_contract_guardrails.py:164-181` | 本阶段追加同类断言（§4.10 / T7），沿用该文件的写法 |
| `MemorySaver` 不得出现在 `src/` | 同上 `:184-193` | **已有**，本阶段不改；`test_state_ownership.py` 再从"import 不到"角度补一刀 |
| `BaseSettings` 子类只在 `config.py` | 同上 `:196-209` | 本阶段不得新增 settings |
| `structlog.configure` 只在 `logging.py` | 同上 `:212-229` | 本阶段只用 `logging.get_logger()` |
| `UsageSnapshot` 字段名 = §3.1 信封 `meta.usage` 字段名 | `llm/tracing.py` | `token_ledger` 的列名**照抄这套字段名**，接上阶段二的缝，不另起名 |
| `config.DATA_DIR` / `REPO_ROOT` | `config.py:19-20` | `DB_PATH` 由 `DATA_DIR` 派生，不新增第二个"数据目录"概念 |
| `settings.database_url` 默认是**绝对路径** | `config.py:96` | 保留绝对路径；只把驱动从 `pysqlite` 换成 `aiosqlite` |
| `tests/test_smoke.py` 断言 `database_url.startswith("sqlite")` | `tests/test_smoke.py:24` | `sqlite+aiosqlite:///...` **仍然通过**，无需改 |

### 1.6 `RetryPolicy` 的三个坑（写测试必需，已实测源码）

| 坑 | 事实 | 后果 |
|---|---|---|
| 默认不重试 `RuntimeError` | `RetryPolicy.retry_on` 默认 `default_retry_on`，而它对 `RuntimeError` / `ValueError` / `OSError` 等返回 **`False`** | **必须显式传 `retry_on=RuntimeError`**，否则节点不会被重跑，"幂等测试"变成空转 |
| 默认会真的 sleep | `initial_interval=0.5`、`backoff_factor=2.0`、`jitter=True` | 测试里传 `initial_interval=0.0, jitter=False` |
| `add_node(..., retry_policy=)` 存在 | `langgraph/graph/state.py:300`；`RetryPolicy` 是 `NamedTuple`（`langgraph/types.py:404`） | 形状确定，照抄 |

---

## 2. 目标文件清单

`✓` = 阶段三新建；`△` = 阶段三修改；其余为既有文件。

```
python助手/
├── AGENTS.md / ROADMAP.md                       （既有，只读）
├── SPEC.md                                  △    本文件（阶段二规格已归档）
├── alembic.ini                              ✓    迁移入口（URL 留空，由 env.py 读 config）
├── pyproject.toml                           △?   默认不改（§1.4）；是否提升三个依赖见「待定项 9」
├── .env.example                             △    DATABASE_URL 换异步驱动
├── docker-compose.yml                       △    DATABASE_URL 默认值同上
├── docs/specs/
│   ├── 阶段一-工程底座.md                       （既有，归档件）
│   └── 阶段二-LLM基座.md                    ✓    阶段二规格归档
├── migrations/
│   ├── env.py                               ✓    同步引擎 + 迁移后建 LangGraph 表
│   ├── script.py.mako                       ✓    后续 `alembic revision` 需要
│   └── versions/0001_init.py                ✓    手写：五张业务表
├── src/pyassist/
│   ├── config.py                            △    +DB_PATH +三个 URL 派生函数 +异步驱动
│   ├── logging.py                                 （既有，本阶段只调用）
│   ├── db/
│   │   ├── __init__.py                            （既有，保持空）
│   │   ├── session.py                       ✓    引擎 / Session 工厂 / 依赖
│   │   ├── models.py                        ✓    五张业务表，无 messages
│   │   └── repositories.py                  ✓    ★ 唯一 SQL 出口
│   └── agent/
│       ├── __init__.py                            （既有，保持空）
│       ├── checkpointer.py                  ✓    ★ 唯一检查点构造点
│       └── store.py                         ✓    ★ 唯一 Store 构造点
└── tests/
    ├── test_contract_guardrails.py          △    追加五条单点所有权断言
    ├── test_state_ownership.py              ✓    ★ 边界守护（程序化）
    └── test_idempotency.py                  ✓    ★ 节点幂等守护
```

`db/session.py` 与 `agent/*.py` 的公开面各自只有几个函数，**不留任何空函数或 TODO 占位** —— 那是平行实现的种子（§0）。

---

## 3. 任务分解

| # | 任务 | 产出 | 预估 |
|---|---|---|---|
| T0 | 归档阶段二规格 | `docs/specs/阶段二-LLM基座.md` | 10 min |
| T1 | 路径与方言知识的单一落点 | `config.py`、`.env.example`、`docker-compose.yml` | 25 min |
| T2 | 引擎与 Session | `db/session.py` | 30 min |
| T3 | 五张业务表 | `db/models.py` | 45 min |
| T4 | 唯一 SQL 出口 + 去重键 | `db/repositories.py` | 50 min |
| T5 | 检查点与 Store 构造点 | `agent/checkpointer.py`、`agent/store.py` | 60 min |
| T6 | 迁移 | `alembic.ini`、`migrations/env.py`、`migrations/script.py.mako`、`migrations/versions/0001_init.py` | 60 min |
| T7 | 两个守护测试 + 守护断言扩展 | `tests/test_state_ownership.py`、`tests/test_idempotency.py`、`tests/test_contract_guardrails.py` | 75 min |
| T8 | 验收五连 + `alembic upgrade head` / `alembic check` + 提交 | 绿的阶段三 | 30 min |

**合计约 6 小时。** 无硬阻塞项；Postgres 分支不可验（§5）。

### T0 · 归档阶段二规格

```bash
cp SPEC.md "docs/specs/阶段二-LLM基座.md"     # 本文件随即被重写
```

与阶段二 SPEC 的 T0 同一做法。归档件保留原标题（`阶段一` 的归档件也是这么处理），因此**不要**在归档件里改标题。

### T1 · `config.py` 增量

见 §4.1。三件事：`DB_PATH`、三个纯派生函数、异步驱动。

**为什么要新增函数**：`db/session.py` 被 ROADMAP 要求"不得出现方言分支"。把**方言知识**（SQLite 需要 `busy_timeout`、SQLite 的文件路径怎么取、PG 的 conninfo 怎么拼）集中到 `config.py` 这个唯一配置入口，`db/session.py` 就只剩装配 —— 那条要求从"靠自觉"变成"可机器断言"（T7 的守护断言）。取舍见「待定项 2」。

`.env.example` 与 `docker-compose.yml` 的 `DATABASE_URL` 同步改（顺带把 `docker-compose.yml` 里的相对路径口径统一为与 config 一致的写法）。

### T2 · `db/session.py`

见 §4.2。`create_engine` / `create_session_factory` / `open_session`。

**不做模块级引擎单例**：单例在测试里要额外重置，而阶段四的 `lifespan` 本来就会持有引擎。需要长生命周期时由调用方持有（阶段四），本阶段不提前发明生命周期管理。

### T3 · `db/models.py`

见 §4.3。五张表，**没有 `messages`**。

### T4 · `db/repositories.py`

见 §4.4。★ 唯一 SQL 出口。**幂等的权威是 `UNIQUE` 约束，不是应用层的 `if`**。

### T5 · `agent/checkpointer.py` + `agent/store.py`

见 §4.5 / §4.6。两个 `@asynccontextmanager` + 两个 `setup_*`。

**`open_*` 不调 `setup()`**：§2 明确"不要在请求路径里调"。SQLite 侧上游会懒调用（幂等无害），PG 侧由迁移（T6）保证。

### T6 · 迁移

见 §4.7。`env.py` 用**同步**引擎，LangGraph 的地表在**迁移事务提交之后**建。

### T7 · 两个守护测试 + 守护断言

见 §4.8 / §4.9 / §4.10。这是本阶段唯一有"判断逻辑"的产出。

### T8 · 验收

跑 §5 的全部命令。`uv run pytest` 必须是**退出码 0**。`alembic upgrade head` 必须在**删掉 `data/app.db` 之后**也能从零跑通 —— 一次性的"在我机器上已经建过"不算通过。

---

## 4. 关键文件规格

### 4.1 `src/pyassist/config.py`（增量）

```python
# ---------- 数据库 ----------
# 唯一的库文件路径常量。检查点、业务表、Store 都指向它（§2）。
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
        raise ValueError(f"resolve_sqlite_path 只适用于 SQLite，当前是 {url.database!r}")
    if not url.database:
        raise ValueError("DATABASE_URL 里没有库路径")
    return Path(url.database)


def to_psycopg_conninfo(database_url: str) -> str:
    """把 SQLAlchemy 的 PG URL 转成 psycopg 的 conninfo（生产分支用）。

    两个构造点（检查点、Store）都要这一句，所以只能有一个家 —— 这里是唯一实现。
    """
    return make_url(database_url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )
```

`Settings` 只改一行：

```python
    database_url: str = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"
```

三条要点：

1. **`.as_posix()`**：`sqlite+aiosqlite:///C:/.../data/app.db` 是 SQLAlchemy 在 Windows 上的规范写法；反斜杠虽然也能用，但 URL 里出现 `\` 是自找麻烦。`make_url(...).database` 会原样回读，`Path()` 再解析回来 —— 往返一致（`test_state_ownership.py` 断言的正是这条）。
2. **`build_engine_kwargs` 用 `make_url` 而不是字符串 `startswith`**：URL 的方言段大小写与形态都可能变，交给 SQLAlchemy 解析。
3. 三个函数是 `settings.database_url` 的**纯派生**，没有状态、没有 IO —— 这是它们能待在 `config.py` 而不算"业务逻辑"的理由。取舍见「待定项 2」。

### 4.2 `src/pyassist/db/session.py`

```python
"""引擎与 Session（§2）。

**本文件不含任何方言字样。** 方言相关的连接参数由 `config.build_engine_kwargs()`
以数据形式给出，本文件只装配 —— 于是"不得出现方言分支"从约定变成可断言的
事实（`tests/test_contract_guardrails.py`）。

引擎**不做模块级单例**：`create_async_engine` 本身是惰性的，但单例在测试里要
额外重置；需要长生命周期时由调用方持有（阶段四的 FastAPI `lifespan`）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pyassist.config import build_engine_kwargs, settings


def create_engine(database_url: str | None = None) -> AsyncEngine:
    """建一个异步引擎。不传 URL 则用 `settings.database_url`。

    传参是给测试留的缝（照 `llm.client.get_chat_model(api_key=...)` 的做法）：
    **构造引擎这个动作在物理上只有这一处**，测试不必自己 new 一个。
    """
    url = database_url or settings.database_url
    return create_async_engine(url, **build_engine_kwargs(url))


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """`expire_on_commit=False`：提交后还要读对象属性是常态（阶段四/六），
    默认的过期会在 async 场景里抛 `MissingGreenlet`。"""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def open_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """一次业务操作一个 Session，退出即关闭。阶段四的依赖注入会包一层它。"""
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
```

### 4.3 `src/pyassist/db/models.py`

```python
"""SQLAlchemy ORM（§2）。**没有 `messages` 表。**

对话历史的唯一真相是 LangGraph 检查点（`agent/checkpointer.py`）；业务表只存
"人的行为结果"——用户、会话、练习提交、错题、Token 账本。在这里再存一份消息，
就制造了两份会互相矛盾的对话历史，正是 §2 要防的分叉。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有业务表的基类。`migrations/env.py` 的 `target_metadata` 用它。"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    display_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ChatSession(Base):
    """会话。**`id` 就是 LangGraph 的 `thread_id`** —— 不另立一套身份。

    §2 说续跑只要 `config={"configurable": {"thread_id": <会话 id>}}`；
    若这里再造一个 `thread_id` 列，同一件事就有两个 id，早晚不一致。
    类名叫 `ChatSession` 是为了不和 SQLAlchemy 的 `Session` 混淆。
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ExerciseAttempt(Base):
    """v3 的练习提交（§5.4）。"""

    __tablename__ = "exercise_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    exercise_id: Mapped[str] = mapped_column(String(64), index=True)
    topic: Mapped[str] = mapped_column(String(32))
    is_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[int | None] = mapped_column(Integer, default=None)
    # 用户代码落库是**产品数据**（`GET /history` 要回放），
    # 与 §3.2「日志不记完整用户代码」不冲突 —— 那条约束的是 structlog，不是数据库。
    submitted_code: Mapped[str] = mapped_column(Text)
    stdout: Mapped[str | None] = mapped_column(Text, default=None)
    stderr: Mapped[str | None] = mapped_column(Text, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    # 幂等键：节点重跑时同一个键只落一行（§2）。权威见 repositories.py。
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Mistake(Base):
    """错题（§5.4 / §5.5）。v4 的推荐**必须引用实际做错的题目 id**，所以要有 `exercise_id`。"""

    __tablename__ = "mistakes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    exercise_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    topic: Mapped[str] = mapped_column(String(32))
    error_signature: Mapped[str | None] = mapped_column(String(256), default=None)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TokenLedger(Base):
    """Token 账本。**列名照抄 `llm.tracing.UsageSnapshot` = §3.1 信封 `meta.usage`。**

    阶段二留下的缝：`UsageSnapshot` 已经在算这些数，阶段四的信封直接序列化它，
    阶段八的 `/stats` 与熔断读这张表 —— 三处字段名一致，不需要任何映射层。
    """

    __tablename__ = "token_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    model_id: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Decimal 落库：单价来自 config.PRICE_TABLE（也是 Decimal）。用 Float 会产生精度假象。
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    is_off_peak: Mapped[bool] = mapped_column(Boolean, default=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
```

三条要点：

1. **`DateTime(timezone=True)` 在 SQLite 上读回来是 naive**（SQLite 没有时区类型）。这是已知取舍：SQLite 只做开发，PG 上是真正的 `timestamptz`。写死在 `_utcnow()` 里返回带时区的 UTC，读取侧的归一化留给阶段四的序列化层 —— 本阶段不做"读出来再补时区"的加工，那会变成第二套时间语义。
2. **`idempotency_key` 是 `unique=True`**，索引由唯一约束自动提供（额外 `index=True` 只是为了显式可见）。它是幂等的**权威**。
3. 表名复数 `snake_case`、类名 `PascalCase`、时间字段 `*_at`、布尔 `is_*` —— 全部照 §3.3。

### 4.4 `src/pyassist/db/repositories.py`

```python
"""★ 唯一 SQL 出口（§2）。

`services/*` / 图节点 / 阶段四的路由**一律不写 SQL**，只调这里的函数。
例外只有 `migrations/` —— 迁移本来就是 DDL，不算业务读写。

幂等的权威是表上的 `UNIQUE` 约束（`idempotency_key`），不是应用层的 `if`：
恢复会重跑被中断的节点（§2），"先查再插"在并发下仍会撞，只有约束能真正兜住。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.db.models import ChatSession, ExerciseAttempt, Mistake, TokenLedger, User
from pyassist.logging import get_logger

logger = get_logger(__name__)


async def ensure_user(
    session: AsyncSession, *, user_id: str, display_name: str | None = None
) -> User:
    """幂等建档。`user_id` 由调用方给出（将来从鉴权来），不用随机值。"""
    existing = await session.get(User, user_id)
    if existing is not None:
        return existing
    user = User(id=user_id, display_name=display_name)
    session.add(user)
    await session.flush()
    return user


async def create_chat_session(
    session: AsyncSession, *, session_id: str, user_id: str
) -> ChatSession:
    """建会话。`session_id` 同时就是 LangGraph 的 `thread_id`（见 models.py）。"""
    existing = await session.get(ChatSession, session_id)
    if existing is not None:
        return existing
    chat_session = ChatSession(id=session_id, user_id=user_id)
    session.add(chat_session)
    await session.flush()
    return chat_session


async def record_attempt(
    session: AsyncSession,
    *,
    idempotency_key: str,
    session_id: str,
    exercise_id: str,
    topic: str,
    submitted_code: str,
    is_passed: bool,
    score: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    duration_ms: int | None = None,
) -> bool:
    """落一条练习提交。**已被同一个 `idempotency_key` 记过则返回 `False`，不重复落。**

    返回 `bool` 而不是 `ExerciseAttempt`：调用方（节点）需要知道"这次跑是新的还是重跑"，
    但不需要拿到行对象。阶段八要读记录时另开查询函数，不从这里返回。
    """
    if await _already_recorded(session, ExerciseAttempt, idempotency_key):
        return False
    try:
        # SAVEPOINT（begin_nested）：唯一约束冲突**只回滚这一条插入**，
        # 不牵连调用方在同一事务里已经做过的别的写入。
        # 换成 session.rollback() 会把整个事务一起丢掉 —— 那是个等着被踩的坑。
        async with session.begin_nested():
            session.add(
                ExerciseAttempt(
                    idempotency_key=idempotency_key,
                    session_id=session_id,
                    exercise_id=exercise_id,
                    topic=topic,
                    submitted_code=submitted_code,
                    is_passed=is_passed,
                    score=score,
                    stdout=stdout,
                    stderr=stderr,
                    duration_ms=duration_ms,
                )
            )
            await session.flush()
    except IntegrityError:
        # 并发下另一个任务抢先落了同一把键 —— 这正是唯一约束存在的意义。
        # 捕获的是具体异常（§3.2「捕获链条不捕获大类」），不是 except Exception。
        logger.info("db.attempt.duplicate", idempotency_key=idempotency_key)
        return False
    return True


async def record_mistake(
    session: AsyncSession,
    *,
    idempotency_key: str,
    user_id: str,
    topic: str,
    exercise_id: str | None = None,
    error_signature: str | None = None,
) -> bool:
    """落一条错题（幂等，语义同 `record_attempt`）。"""
    ...


async def append_token_usage(
    session: AsyncSession,
    *,
    idempotency_key: str,
    session_id: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    cost_usd: Decimal,
    is_off_peak: bool,
) -> bool:
    """追加一条 Token 账本（幂等）。

    **本阶段不接任何请求路径**：阶段四的信封与阶段八的 `/stats` 才调它。
    字段名与 `llm.tracing.UsageSnapshot` 一一对应，调用方直接按属性名传。
    """
    ...


async def list_attempts(
    session: AsyncSession, *, session_id: str, limit: int = 50
) -> Sequence[ExerciseAttempt]:
    """`GET /history` 用（阶段八）。只有一处 `select()`，不散落在 service 里。"""
    result = await session.scalars(
        select(ExerciseAttempt)
        .where(ExerciseAttempt.session_id == session_id)
        .order_by(ExerciseAttempt.created_at.desc())
        .limit(limit)
    )
    return result.all()


async def count_attempts(session: AsyncSession) -> int:
    """测试与阶段八的统计用。"""
    return await session.scalar(select(func.count()).select_from(ExerciseAttempt)) or 0


async def _already_recorded(
    session: AsyncSession, model: type[ExerciseAttempt] | type[Mistake] | type[TokenLedger],
    idempotency_key: str,
) -> bool:
    """键是否已存在。**只是快路径优化，权威是表上的唯一约束。**"""
    found = await session.scalar(
        select(model.id).where(model.idempotency_key == idempotency_key)
    )
    return found is not None
```

三条要点：

1. **不用 `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_nothing()`**。它能把方言绑进仓储，生产切 PG 时得改代码 —— 而 §1 的整个设计前提是"换库只改 `DATABASE_URL`"。`select` 探测 + `IntegrityError` 兜底是两种方言都成立的做法。
2. 回滚的粒度是 **SAVEPOINT**（`session.begin_nested()`），不是整个事务。这一点是刻意选的：如果写成 `session.rollback()`，一次正常的"重复写入"就会把调用方在同一事务里已经做过的其它写入一起丢掉 —— 而"重复写入"在恢复重跑时是**常态**，不是异常路径。用 SAVEPOINT 之后，`record_*` 的调用方不必关心事务编排，这条重担不会外溢成调用方的纪律要求。
3. 查询函数与写入函数分开返回（`bool` vs 行对象），避免"写入顺便返回行"引诱调用方在写入路径上做读取加工。

### 4.5 `src/pyassist/agent/checkpointer.py`

```python
"""★ 唯一检查点构造点（§2）。

开发：`AsyncSqliteSaver` → `data/app.db`（与业务表**同一个文件**）。
生产：`AsyncPostgresSaver` → 同一个 PG 实例。**禁止 `MemorySaver`**（重启即丢）。

为什么是异步类而不是 ROADMAP 字面写的 `SqliteSaver`：
`langgraph.checkpoint.sqlite.SqliteSaver` **只支持同步**，而 §2 禁止在 async 路由里
阻塞事件循环、§3.4 要求 DB 路径 async。两类的表结构完全相同，只是接口。
详见 SPEC §5「与 ROADMAP 验收的差异」第 1 条。

为什么是 `@asynccontextmanager` 而不是模块级单例：
`AsyncSqliteSaver.__init__` 会调 `asyncio.get_running_loop()`，import 期构造直接抛。
构造点只能在**运行中的事件循环内**进入 —— 阶段四的 FastAPI `lifespan` 是它唯一的
长生命周期宿主。

Postgres 类是**函数内 import**：它们会拉进 psycopg，而 psycopg 的二进制轮子带
libpq。放在模块级，一台没有 libpq 的机器上连 SQLite 路径与全部测试都会 import 失败 ——
一个开发期用不到的后端不该有这个能力。SQLite 类留在模块级（它是开发期的主路径，
早失败更早发现）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy.engine import make_url

from pyassist.config import resolve_sqlite_path, settings, to_psycopg_conninfo
from pyassist.logging import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def open_checkpointer(
    *, database_url: str | None = None
) -> AsyncIterator[BaseCheckpointSaver[str]]:
    """打开唯一的检查点。不传 URL 则用 `settings.database_url`。

    `database_url` 是给测试留的缝：测试用 `tmp_path`，绝不污染真实的 `data/app.db`。

    **不在这里调 `setup()`** —— §2 明确"不要在请求路径里调"。SQLite 侧上游会在
    首次使用时懒调用（幂等，无害）；PG 侧由迁移（`migrations/env.py`）保证已建表。
    """
    resolved = database_url or settings.database_url
    backend = make_url(resolved).get_backend_name()

    if backend == "sqlite":
        path = resolve_sqlite_path(resolved)
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            logger.info("checkpointer.opened", backend="sqlite", db_path=str(path))
            yield saver
        return

    if backend == "postgresql":
        # **函数内 import**，不是模块级：`langgraph.checkpoint.postgres.aio` 会拉进
        # psycopg / psycopg_pool，而 psycopg 的二进制轮子带 libpq。放在模块级，
        # 一台没有 libpq 的机器上连 SQLite 路径和**全部测试**都会 import 失败 ——
        # 一个开发期根本用不到的后端，不该有能力拖垮开发环境。
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(to_psycopg_conninfo(resolved)) as saver:
            logger.info("checkpointer.opened", backend="postgresql")
            yield saver
        return

    raise ValueError(f"不支持的数据库后端：{backend!r}（§2 只允许 SQLite / PostgreSQL）")


async def setup_checkpointer(*, database_url: str | None = None) -> None:
    """建检查点表。**只给迁移脚本调用**（§2：不在请求路径里调）。

    幂等（`is_setup` 标志 + `CREATE TABLE IF NOT EXISTS`），所以"迁移跑一次、
    运行时上游再懒调用"不会撞表，不必额外去重。
    """
    async with open_checkpointer(database_url=database_url) as saver:
        await saver.setup()
```

### 4.6 `src/pyassist/agent/store.py`

```python
"""★ 唯一长期记忆 Store 构造点（§2）。

长期记忆与用户画像走 LangGraph `Store`，**不走 `db/repositories.py`，也不另开
第二个向量库**。`services/memory.py`（阶段八）只负责"从对话里抽取偏好"这类加工，
持久化一律经这里。

`index=None`：不配 embeddings。向量模型选型是阶段五的待定项 1，本阶段**不替它拍板**；
语义检索所需的 index 配置是阶段八记忆功能的缝。

注意：`langgraph.store.sqlite.base` 有**模块级**的 `import sqlite_vec`，
所以本模块的 import 硬依赖 `sqlite-vec`（已由 `langgraph-checkpoint-sqlite` 声明）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.store.base import BaseStore
from langgraph.store.sqlite.aio import AsyncSqliteStore
from sqlalchemy.engine import make_url

from pyassist.config import resolve_sqlite_path, settings, to_psycopg_conninfo
from pyassist.logging import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def open_store(*, database_url: str | None = None) -> AsyncIterator[BaseStore]:
    """打开唯一的长期记忆 Store。与检查点指向**同一个库**（§2）。"""
    resolved = database_url or settings.database_url
    backend = make_url(resolved).get_backend_name()

    if backend == "sqlite":
        path = resolve_sqlite_path(resolved)
        async with AsyncSqliteStore.from_conn_string(str(path), index=None) as store:
            logger.info("store.opened", backend="sqlite", db_path=str(path))
            yield store
        return

    if backend == "postgresql":
        # 函数内 import，理由同 `agent/checkpointer.py`（psycopg → libpq）。
        from langgraph.store.postgres.aio import AsyncPostgresStore

        async with AsyncPostgresStore.from_conn_string(
            to_psycopg_conninfo(resolved), index=None
        ) as store:
            logger.info("store.opened", backend="postgresql")
            yield store
        return

    raise ValueError(f"不支持的数据库后端：{backend!r}（§2 只允许 SQLite / PostgreSQL）")


async def setup_store(*, database_url: str | None = None) -> None:
    """建 Store 的表。**只给迁移脚本调用**。幂等，语义同 `setup_checkpointer`。"""
    async with open_store(database_url=database_url) as store:
        await store.setup()
```

### 4.7 迁移

**`alembic.ini`**：

```ini
[alembic]
script_location = migrations
prepend_sys_path = .
path_separator = os
# Deliberately empty: the single source of the URL is pyassist.config (SPEC section 2,
# single config entry point). migrations/env.py reads it and injects it.
# NOTE: keep this file ASCII-only -- configparser reads it with the system locale codec
# (GBK on a zh-CN Windows box), so UTF-8 comments here crash `alembic` before it starts.
sqlalchemy.url =
```

不给 `[loggers]` 那一段 —— `env.py` 不调 `fileConfig`，少一段样板，也少一处 ruff 要过的地方。

> **实施期修正（2026-10-03）**：本节的 `alembic.ini` 草案原本带**中文注释** —— 实测在本机（zh-CN
> Windows）直接 `UnicodeDecodeError: 'gbk' codec can't decode`，因为 `configparser` 用**系统
> locale 编码**读它，不是 UTF-8。故改为 ASCII-only 的英文注释。**约定：`alembic.ini` 不许出现非
> ASCII 字符**（同理适用于任何被 `configparser` 读的文件）。
>
> 另外两处草案在 `ruff` / `mypy --strict` 下要被改：`include_object` 的 `if/return/return` 触发
> `SIM103`，收成单个 `return not (...)`；`setup_checkpointer/setup_store` 里的 `saver.setup()` /
> `store.setup()` 在 `BaseCheckpointSaver` / `BaseStore` 基类上没有声明（mypy `attr-defined`），
> 各加一个模块内 `_HasSetup` Protocol + `cast`，而不是 `# type: ignore`。

**`migrations/env.py`**：

```python
"""Alembic 环境（同步）。

刻意用**同步**引擎：迁移是 CLI 的一次性动作，而 `checkpointer.setup()` /
`store.setup()` 是异步的 —— 只有在**没有运行中事件循环**的同步上下文里，
才能安全地 `asyncio.run(...)`。用 Alembic 的异步模板（`connection.run_sync`）
反而会因为 greenlet 外层已有事件循环而炸。

LangGraph 的地表**不写进 0001_init.py 的 upgrade()**：那里还处在迁移事务内，
此时再开一条连接去建表，会和迁移事务抢同一个 SQLite 文件的写锁。改成在
**事务提交、连接关闭之后**建 —— 顺序见 `run_migrations_online()`。
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from pyassist.agent.checkpointer import setup_checkpointer
from pyassist.agent.store import setup_store
from pyassist.config import settings
from pyassist.db.models import Base

config = context.config
target_metadata = Base.metadata

# LangGraph 自己的表不归 Alembic 管（它们的形状由上游的 setup() 决定，会随
# langgraph 版本变）。没有这个过滤器，`alembic check` 会把它们报成"数据库里多出来的
# 表"并要求 DROP —— 一个会把检查点连同对话历史一起删掉的假告警。
_LANGGRAPH_TABLE_PREFIXES = ("checkpoint", "writes", "store", "vector")


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """只把**业务表**纳入 Alembic 的视野。"""
    if type_ == "table" and name is not None:
        if name.startswith(_LANGGRAPH_TABLE_PREFIXES):
            return False
    return True


# 迁移用同步驱动。这个映射只属于迁移流程 —— §2 的"不得出现方言分支"针对的是
# db/session.py（业务路径），迁移从来就是一次性的 DDL 工具。
_SYNC_DRIVERS = {
    "sqlite+aiosqlite": "sqlite+pysqlite",
    "postgresql+psycopg": "postgresql+psycopg",  # psycopg3 同步异步同名驱动
}


def _sync_url() -> str:
    url = make_url(settings.database_url)
    return url.set(drivername=_SYNC_DRIVERS.get(url.drivername, url.drivername)).render_as_string(
        hide_password=False
    )


def _setup_langgraph_storage() -> None:
    """建检查点与 Store 的表。幂等，重复跑安全（§2「setup 只需在建库时跑一次」）。"""

    async def _run() -> None:
        await setup_checkpointer()
        await setup_store()

    asyncio.run(_run())


def run_migrations_offline() -> None:
    """只生成 SQL（`alembic upgrade --sql`）：不建 LangGraph 的表，也不连库。"""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_object=include_object,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
    # 到这里迁移事务已提交、连接已关闭 —— 再建 LangGraph 的表，不抢写锁。
    _setup_langgraph_storage()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

**`migrations/versions/0001_init.py`**：

```python
"""0001 · 建业务表（§2）。**没有 `messages` 表。**

LangGraph 的 `checkpoints` / `writes` / store 表**不在这里** —— 它们由
`migrations/env.py` 在迁移事务提交之后调 `checkpointer.setup()` / `store.setup()`
建（幂等）。理由见 env.py 的模块 docstring（SQLite 写锁）。

Revision ID: 0001_init
Revises:
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None

_BUSINESS_TABLES = ("token_ledger", "mistakes", "exercise_attempts", "sessions", "users")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("display_name", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(32), primary_key=True),  # == LangGraph thread_id
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    # exercise_attempts / mistakes / token_ledger 同形，按 models.py 的列一一对应；
    # 三张表各带一个 unique 的 idempotency_key（幂等的权威，见 repositories.py）。
    ...


def downgrade() -> None:
    for table in _BUSINESS_TABLES:
        op.drop_table(table)
```

**手写而非 autogenerate**：autogenerate 需要活库、且对 `op` 的生成顺序不透明；手写才能让"业务表与 LangGraph 表分两步建"这件事在文件里看得见。

### 4.8 `tests/test_state_ownership.py`

```python
"""阶段三的边界守护（§2）。

三条断言围绕同一件事：**对话历史只有一个家，业务记录不重复存对话**。
其中"检查点目标 == 业务表目标 == data/app.db"是**程序化**的 —— 不是读常量比对，
而是开一条真实连接，问数据库自己"你是哪个文件"。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pyassist
from sqlalchemy.engine import make_url

from pyassist.agent.checkpointer import open_checkpointer, setup_checkpointer
from pyassist.agent.store import setup_store
from pyassist.config import DB_PATH, settings
from pyassist.db import models
from pyassist.db.session import create_engine


def test_settings_url_points_at_the_single_db_path() -> None:
    """URL 与 `DB_PATH` 是同一个文件 —— 路径常量只有一个。"""
    url = make_url(settings.database_url)
    assert url.get_backend_name() == "sqlite"  # 开发库
    assert Path(url.database or "").resolve() == DB_PATH.resolve()


def test_engine_resolves_to_the_same_file() -> None:
    """`db/session.py` 的引擎指向同一个文件。"""
    engine = create_engine()
    assert Path(make_url(engine.url).database or "").resolve() == DB_PATH.resolve()


async def test_live_checkpointer_connection_is_the_same_file() -> None:
    """**关键一步**：问活的检查点连接"你的 main 库是哪个文件"。

    这是"程序化断言，不是靠约定"的落点 —— 常量比对只能证明"我们写了同一个字符串"，
    证明不了连接真的开在那。
    """
    async with open_checkpointer() as saver:
        # 测试不参与 mypy --strict（只管 src/pyassist）；这里刻意伸手到 saver.conn，
        # 因为 AsyncSqliteSaver 的公开属性就是它。
        cursor = await saver.conn.execute(
            "SELECT file FROM pragma_database_list WHERE name = 'main'"
        )
        row = await cursor.fetchone()
        await cursor.close()
    assert row is not None
    assert Path(row[0]).resolve() == DB_PATH.resolve()


async def test_business_and_checkpoint_tables_live_in_one_file(tmp_path: Path) -> None:
    """业务表、检查点表、Store 表三者都在**同一个文件**里（ROADMAP 的"迁移后检查表结构"）。

    用 tmp 库而不是真实 `data/app.db`，避免测试互相污染、也不 require 迁移先跑过。
    """
    url = f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    await setup_checkpointer(database_url=url)
    await setup_store(database_url=url)

    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        names = {row[0] for row in rows}
    await engine.dispose()

    assert {"users", "sessions", "exercise_attempts", "mistakes", "token_ledger"} <= names
    assert {"checkpoints", "writes"} <= names
    assert "store" in names  # AsyncSqliteStore.setup() 建的 KV 表


def test_models_declare_no_message_shaped_table() -> None:
    """§2：不在业务表里再存一份对话消息。

    不只查表名 —— 也查形状：没有任何表同时具备 role/content/tool_calls 这三列。
    """
    tables = models.Base.metadata.tables
    assert "messages" not in tables
    for table in tables.values():
        columns = set(table.columns.keys())
        assert not {"role", "content", "tool_calls"} <= columns, table.name


def test_memory_saver_is_not_reachable_from_pyassist() -> None:
    """§2：`MemorySaver` 重启即丢，从 `src/pyassist` 里 import 不到（ROADMAP 的原文要求）。"""
    for module_info in pkgutil.walk_packages(pyassist.__path__, prefix="pyassist."):
        module = importlib.import_module(module_info.name)
        assert not hasattr(module, "MemorySaver"), module_info.name
```

### 4.9 `tests/test_idempotency.py`

```python
"""节点幂等守护（§2）：恢复会重跑被中断的节点，副作用必须带去重键。

图和 State 都定义在**本测试文件内** —— 阶段三不建任何产品图（那是阶段六的
`agent/graphs/*.py` + `agent/state.py`）。这是测试夹具，不是"第二套图状态 schema"。
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from pyassist.agent.checkpointer import open_checkpointer
from pyassist.db.models import Base
from pyassist.db.repositories import (
    count_attempts,
    create_chat_session,
    ensure_user,
    record_attempt,
)
from pyassist.db.session import create_engine, create_session_factory

THREAD_ID = "thread-idempotency-1"
DEDUP_KEY = f"{THREAD_ID}:probe:0"


class _ProbeState(TypedDict):
    passes: int


def _build_graph(session_factory: object, *, fail_first: bool) -> tuple[StateGraph, dict[str, int]]:
    """一个会"第一次失败、第二次成功"的单节点图。

    用 `RetryPolicy` 迫使**同一个节点在同一个 thread 下重跑** —— 这正是 §2 说的
    "恢复会重跑被中断的节点"的最小可复现形态。
    """
    calls = {"n": 0}

    async def probe(state: _ProbeState) -> _ProbeState:
        calls["n"] += 1
        async with session_factory() as session:  # type: ignore[operator]
            await record_attempt(
                session,
                idempotency_key=DEDUP_KEY,
                session_id=THREAD_ID,
                exercise_id="ex-1",
                topic="列表",
                submitted_code="pass",
                is_passed=True,
            )
            await session.commit()
        if fail_first and calls["n"] == 1:
            raise RuntimeError("第一次执行故意失败，迫使同节点重跑")
        return {"passes": state["passes"] + 1}

    builder = StateGraph(_ProbeState)
    builder.add_node(
        "probe",
        probe,
        # 三个坑（SPEC §1.6）：默认 retry_on 不重试 RuntimeError；默认会真 sleep。
        retry_policy=RetryPolicy(
            max_attempts=2, retry_on=RuntimeError, initial_interval=0.0, jitter=False
        ),
    )
    builder.add_edge(START, "probe")
    builder.add_edge("probe", END)
    return builder, calls


async def test_node_rerun_keeps_business_rows_at_one(tmp_path: Path) -> None:
    """同一 thread 下节点跑了两次，业务表仍只有一行（去重键生效）。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        await ensure_user(session, user_id="u1")
        await create_chat_session(session, session_id=THREAD_ID, user_id="u1")
        await session.commit()

    async with open_checkpointer(database_url=url) as saver:
        builder, calls = _build_graph(session_factory, fail_first=True)
        graph = builder.compile(checkpointer=saver)
        # §2：恢复只传 config，不重传 state。
        await graph.ainvoke({"passes": 0}, {"configurable": {"thread_id": THREAD_ID}})

    assert calls["n"] == 2, "节点必须真的重跑过，否则这个测试是空转"

    async with session_factory() as session:
        assert await count_attempts(session) == 1
    await engine.dispose()
```

**为什么这个测试是有效的**：`calls["n"] == 2` 断言了重跑**真的发生**（否则测试会因为"节点只跑一次所以只有一行"而假绿）；`count_attempts == 1` 断言了重跑的副作用被去重键吸收。两个断言缺一不可。

### 4.10 `tests/test_contract_guardrails.py`（追加）

沿用该文件的 AST 扫法与"断言信息不回显内容"的安全约定，追加：

| # | 断言 | 手法 |
|---|---|---|
| 9 | 检查点类只在 `agent/checkpointer.py` 被引用 | AST：`ast.ImportFrom` 模块前缀 `langgraph.checkpoint`、或 `ast.Call` 名匹配 `{"SqliteSaver", "AsyncSqliteSaver", "PostgresSaver", "AsyncPostgresSaver"}`，排除 `src/pyassist/agent/checkpointer.py` |
| 10 | Store 类只在 `agent/store.py` 被引用 | 同上，模块前缀 `langgraph.store`，名字 `{"SqliteStore", "AsyncSqliteStore", "PostgresStore", "AsyncPostgresStore"}` |
| 11 | `create_async_engine` / `create_engine` 只在 `db/session.py` 出现（`migrations/env.py` 除外） | AST：`ast.Call` 名匹配 |
| 12 | `db/session.py` 里不出现 `sqlite` 字面量（**大小写不敏感**，注释与 docstring 也算） | 文本扫描（§4.1 的设计把方言知识挪去了 `config.py`，这条断言就是那个设计的可执行形式）。§4.2 的代码草案已按此约束写就 —— 它的 docstring 刻意不含该词 |
| 13 | 裸 SQL 只在 `db/repositories.py`（`migrations/` 除外）：`sqlalchemy.text(` / `exec_driver_sql` | AST |

`MemorySaver` 的断言（原第 6 条）**保持不动** —— 它是阶段一就写下的，本阶段不重复实现。

---

## 5. 验收

沿用前两阶段的五条命令，加上 ROADMAP 阶段三指定的两条（`alembic upgrade head` / 两个守护测试）与一条本文件补的漂移探针（`alembic check`）：

```bash
uv sync
uv run python -c "import pyassist"
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/pyassist
uv run pytest

# 阶段三专属
rm -f data/app.db && uv run alembic upgrade head    # 从零建库也要通
uv run alembic check                                 # models 与迁移不漂移
uv run pytest tests/test_state_ownership.py tests/test_idempotency.py
```

### 验收判据

- [ ] `uv run pytest` 退出码 0（不是 5），全部测试**不碰网络**
- [ ] `alembic upgrade head` 在**删掉 `data/app.db` 之后**仍从零跑通；重复跑第二次不报错（幂等）
- [ ] `alembic check` 无 diff：`db/models.py` 与迁移描述的是同一套业务表（手写迁移的探针）。LangGraph 的表**不**被报成"多余的表"（`env.py` 的 `include_object` 生效）
- [ ] 迁移后 `data/app.db` 里**同时**存在业务表（`users`/`sessions`/`exercise_attempts`/`mistakes`/`token_ledger`）与 `checkpoints`/`writes`，**同一个文件**
- [ ] `make_url(settings.database_url).database` 与**活的检查点连接**（`pragma_database_list`）解析出的路径都等于 `config.DB_PATH`
- [ ] 从 `src/pyassist` 里 import 不到 `MemorySaver`
- [ ] `db/models.py` 没有任何 message 形状的表（表名与列形状都查）
- [ ] 同一 `thread_id` 下节点重跑 2 次，`exercise_attempts` 行数仍为 1
- [ ] `ruff check` / `ruff format --check` 无 diff；`mypy --strict src/pyassist` 零错误
- [ ] `src/` 内除 `agent/checkpointer.py` 外不出现检查点类的构造；除 `agent/store.py` 外不出现 Store 类的构造
- [ ] `db/session.py` 内不出现 `sqlite` 字样
- [ ] 未创建 `errors.py`、`agent/state.py`、任何图、任何 router、任何 service
- [x] `uv sync` 的锁变化**只有** `greenlet` 与 `psycopg-binary` 两条（§1.4 实施期更正）。原判据"不新增任何需要下载的包 / `uv.lock` 不变"**经实测不成立**，已作废并替换为本条：`pyproject.toml` 新增 `aiosqlite` / `sqlite-vec` / `psycopg[binary,pool]` 三条直接声明 + `sqlalchemy[asyncio]` extra，`uv.lock` 相应新增 `greenlet` / `psycopg-binary`。（`aiosqlite` 另钉 `<0.22`，理由见「待定项 9」复注。）

### 与 ROADMAP 验收的差异（四条，均需知悉）

1. **ROADMAP 写"开发 `SqliteSaver`"，本阶段实现的是 `AsyncSqliteSaver`。** 实测 `SqliteSaver` 是同步类（§1.2 第 1 条），而 §3.4 要求 DB 路径 async、§2 禁止在 async 路由里阻塞事件循环。两类的表结构与库文件完全相同，只是接口同步/异步之别。ROADMAP 的"`SqliteSaver`"应读作"SQLite 的检查点持久化"这个概念。
2. **ROADMAP 说"`checkpointer.setup()` / `store.setup()` 写进迁移脚本"，本阶段把它们放在 `migrations/env.py` 的迁移事务**之后**，而不是 `versions/0001_init.py` 的 `upgrade()` 里。** 原因：`upgrade()` 还在迁移事务内，此时再开一条连接去建表会与迁移事务抢同一个 SQLite 文件的写锁。两者都属于"迁移脚本"，但只有放在事务后才是安全的。取舍见「待定项 5」。
3. **ROADMAP 的阶段三验收只有两条命令；本文件补了 `rm -f data/app.db` 前置和 `alembic check`。** 前者：`alembic upgrade head` 在一个已经建过表的库上跑，测不出"从零建库"这条路径，而阶段八的 `docker compose up` 走的正是从零那条。后者：手写迁移最大的风险就是改了 `db/models.py` 忘了同步迁移，`alembic check` 是它的探针 —— 代价只是一条命令，不采纳没道理。
4. **ROADMAP 说"断言从 `src/pyassist` 里 import 不到 `MemorySaver`"，本文件实现为运行时断言（遍历子模块 `hasattr`），而非只靠 AST 文本扫描。** 两者互补：AST（`test_contract_guardrails.py` 的第 6 条，已有）拦"写进源码"；运行时（§4.8 的最后一条）拦"从别处 import 进来再 re-export"。已有的那条不动。

**Postgres 分支不纳入本阶段验收。** 本机无 PG 实例，`AsyncPostgresSaver` / `AsyncPostgresStore` 分支只保证"导入与签名正确"（`ruff` / `mypy` 会替我们盯着），**未经验证**。照阶段二 `--check-models` 的处理方式：如实标注为人工步骤，不进 CI。

---

## 6. 风险与缓解

| 风险 | 触发点 | 缓解 |
|---|---|---|
| **两条连接写同一个 SQLite 文件** → `database is locked` | T2 / T5 | `busy_timeout=30`（`config.build_engine_kwargs`）；`journal_mode=WAL` 由检查点 `setup()` 一次设成库级属性。文档里点明"同一个库"是**同一个文件**、**不是同一条连接** —— 这两件事混淆会导致"共享连接"的错误重构 |
| **`asyncio.get_running_loop()` 构造约束**：在 import 期或同步上下文造 saver 直接抛 | T5 | 一律经 `open_checkpointer()` / `open_store()`；加守护断言（T7 第 9/10 条）防止有人"顺手"import 一个类就在模块级 new |
| **Alembic 异步模板与 `asyncio.run` 冲突** | T6 | `env.py` 用同步引擎 + `_SYNC_DRIVERS` 映射（§4.7）；理由写进文件 docstring |
| **LangGraph 地表与迁移事务抢写锁** | T6 | 建表放在迁移事务提交、连接关闭**之后**（§4.7）；见「待定项 5」 |
| **`alembic check` 把 LangGraph 的表报成"多余"并要求 DROP** | T6 | `env.py` 的 `include_object` 按前缀过滤 `checkpoint* / writes / store* / vector*`（§4.7）。**这条不加，`check` 会给出一个"删掉全部对话历史"的假告警** |
| **模块级 import Postgres 类 → 没 libpq 的机器连 SQLite 与全部测试一起崩** | T5 | 两个构造点的 Postgres 类一律**函数内 import**（§4.5 / §4.6）；SQLite 类留模块级（开发主路径，早失败更早发现） |
| **手写迁移与 `db/models.py` 漂移**（改了模型忘了改迁移） | T3 / T6 | `alembic check` 进验收（§5）；这是手写迁移的代价，用一条命令换回确定性 |
| **`session.rollback()` 连带回滚** | T4 | `record_*` 的 docstring 写明"一次业务操作一个 Session"；§4.9 的测试实际跑过这条路径 |
| **Postgres 分支不可验** | T5 | 只保证导入/签名正确，验收明确排除；实现与 SQLite 分支对称，将来真机验证只需换 `DATABASE_URL` |
| **`sqlite-vec` 是"别人的依赖"** | T5 / 全程 | §1.3 记录这条耦合。本阶段不新增声明（§1.4 已核实是硬依赖），但若将来移除 `langgraph-checkpoint-sqlite`，`agent/store.py` 会先炸 |
| **`migrations/env.py` 会被 ruff 检查**（`extend-exclude` 只排除 `migrations/versions`） | T6 | env.py 保持最小，删掉 Alembic 的 `fileConfig` 样板；`0001_init.py` 在 `versions/` 内不受 ruff 约束 |
| **测试污染真实 `data/app.db`** | T7 | 除 `test_state_ownership.py` 的两条只读断言外，一律用 `tmp_path`（§4.8 / §4.9） |
| **方言知识散落**，`db/session.py` 变成第二个配置源 | T1 / T2 | 方言知识集中在 `config.py`，`db/session.py` 零方言；T7 第 12 条断言把它钉死。取舍见「待定项 2」 |
| **在 `db/models.py` 里"顺手"加一张 `messages` 表** | T3 | §4.8 的表名 + 列形状双查；ROADMAP 原文点名"没有 `messages` 表" |
| **贪快把 Token 累计 / 熔断一起做了** | T4 | §0「不做」表；账本只建表与写函数，不接请求路径 |
| **在阶段三就开始拼图** | T7 | §0「不做」表 + §2 单点所有权；§4.9 的图明确写在测试文件内，且注释说明它是夹具不是产品图 |

---

## 7. 待定项

不阻塞阶段三动工，但需要人拍板。

### 1. `SPEC.md` 的归档策略（本文件已按推荐执行）

`SPEC.md` 原为阶段二规格。本次重写为阶段三，阶段二原文**逐行一致**地归档到 `docs/specs/阶段二-LLM基座.md`（与阶段一的归档件同处一目录、同保留原标题）。

- **推荐（已执行）**：归档而非覆盖。理由同阶段二：归档件是既有引用的解析目标。
- 需人确认：`docs/specs/` 是否继续逐阶段累积（现在是三个文件：阶段一、阶段二、`SPEC.md`）。
- **附带项**：`config.py:95` 与 `docker-compose.yml` 里有指向"SPEC 待定项"的注释，归档后这些编号指向的是**新** SPEC。本阶段未改这些注释（属代码注释改动），请拍板是否要改写成显式路径。

### 2. 方言知识放 `config.py` 还是 `db/session.py`（T1/T2 立刻会遇到）

ROADMAP 要求 `db/session.py`"不得出现方言分支"，但 SQLite 的连接级 `busy_timeout` 客观存在。

- **推荐（本文件采用）**：方言知识集中到 `config.py` 的三个纯派生函数（`build_engine_kwargs` / `resolve_sqlite_path` / `to_psycopg_conninfo`），`db/session.py` 只剩装配，那条要求变成可断言的事实（T7 第 12 条）。
- 备选：允许 `db/session.py` 里有一处显式标注的 SQLite 专用行，并在注释里说明"这不是业务逻辑的方言分支"。**代价**：那条 ROADMAP 要求退回成约定，加不了机器断言。
- 备选二：新开 `db/urls.py` 作为"URL → 连接串"的唯一所有者（是合法的单点所有权，但多一个 ROADMAP 未列的文件）。
- 需人确认：`config.py` 里放**函数**是否触犯阶段一 SPEC §5.2 那句"不在 config.py 里写业务函数"。本文件的立场：这三个是 `settings.database_url` 的纯派生，属于"把配置读对"，不是业务。

### 3. `sessions.id` 是否就用作 LangGraph 的 `thread_id`

- **推荐（本文件采用）**：是。§2 原文就是 `thread_id = <会话 id>`；再立一个 `thread_id` 列会让同一件事有两个 id。
- 备选：另立 `thread_id` 列。若将来需要"一个会话换一个图线程"（如重新开一段上下文）则备选更合适 —— 那是产品决定，不是技术决定。

### 4. 是否把 Postgres 分支推迟到阶段八

- **推荐（本文件采用）**：本阶段实现，但标注"未验证"。理由：两个分支对称、代码量小，且 §2 要求"检查点指向与 `db/session.py` 相同的库"这条必须**现在**就成立；推迟实现等于把 `DATABASE_URL` 切换这件事留到部署前才做，那时才发现问题更贵。
- 备选：本阶段只实现 SQLite，PG 分支留空（`raise NotImplementedError`），阶段八补。**代价**：留了一个空实现 —— 正是 §0 说的"平行实现的种子"。

### 5. `AsyncSqliteSaver.setup()` 的 docstring 说"不要直接调"，而 ROADMAP 要求写进迁移

`AsyncSqliteSaver.setup()` 的 docstring 是 *"It is called automatically when needed and should not be called directly by the user."*；`AsyncPostgresSaver.setup()` 的 docstring 恰好相反：*"It MUST be called directly by the user the first time"*。

- **推荐（本文件采用）**：迁移里调一次。理由：`setup()` 幂等（`is_setup` + `IF NOT EXISTS`），"上游会懒调用"与"迁移里先建好"并不冲突；而 PG 侧**必须**显式调，SQLite 侧照同一套流程走才对称。
- 需人确认：接受这个读法。若不接受，退路是 SQLite 侧完全依赖懒调用、只在 PG 侧显式 `setup()` —— 代价是"表什么时候建的"变成隐式行为。

### 6. `.env` 里若已写死同步驱动的 `DATABASE_URL`

`.env` 已存在（含真实 key，被 git 忽略）。若其中设置了 `DATABASE_URL=sqlite+pysqlite:///...`，它会**覆盖** `config.py` 的新默认值，阶段三的异步引擎会直接报错。

- **推荐**：实施 T1 时检查 `.env` 的 `DATABASE_URL` 一行（**只看变量是否存在与取值形态，不回显内容**），若存在则改驱动段为 `sqlite+aiosqlite`。
- 需人确认：这是本机私改，`scripts/check_secrets.py` 与守护测试都拦不到它（`.env` 的值不参与断言）。

### 7. `token_ledger` 本阶段是否写入

- **推荐（本文件采用）**：只建表 + 提供 `append_token_usage()`，**不在任何请求路径调用**（本阶段还没有请求路径）。阶段四的信封与阶段八的 `/stats` 才接。
- 需人确认：是否希望本阶段就补一个"把 `UsageSnapshot` 落库"的小工具函数。**不推荐** —— 阶段三没有调用方，那会是一个没人跑过的函数。

### 8. `sqlite-vec` 是否需要显式加载扩展

`AsyncSqliteStore` 在 `index=None` 时**不**建向量表，也不加载扩展；`sqlite_vec` 只在 import 期被 `langgraph.store.sqlite.base` 引用到。

- **推荐**：本阶段不加载扩展、不配 index。向量检索所需的 `index=SqliteIndexConfig(embeddings=...)` 是阶段八记忆功能的缝，等阶段五的向量模型选型定了一起做。
- 需人确认：接受"阶段三的 Store 只能做 KV 记忆、还不能做语义检索"。

### 9. 三个传递依赖是否提升为直接声明（`pyproject.toml`）

`aiosqlite` / `sqlite-vec` / `psycopg`(+`pool`) 都已装、都在 `uv.lock` 里，且都是
`langgraph-checkpoint-sqlite` / `-postgres` 的硬 `Requires-Dist`（§1.4 已逐个核实）——
所以**本阶段不声明也能跑通，验收不受影响**。

但这三件是"我们自己的代码直接需要"的东西：`db/session.py` 用 `sqlite+aiosqlite` 方言、
`agent/store.py` 经 `langgraph.store.sqlite` 硬依赖 `sqlite-vec`、生产分支直接调 psycopg。
由别人替我们声明，意味着上游哪天换实现，我们不会得到解析错误，只会静默崩。

- **推荐（已采纳并实施）**：提升为 `[project].dependencies` 里的直接声明（`aiosqlite>=0.20,<0.22`、`sqlite-vec>=0.1.6,<1.0`、`psycopg[binary,pool]>=3.2,<4.0`）。注意**分发名是 `sqlite-vec`（短横线），import 名是 `sqlite_vec`（下划线）**。

  **实施后复注（实测，与原文三处不符，均如实记录）**：
  1. 原文"`uv sync` 应**不新增锁条目**"**不成立**。实际新增两条，都不是"版本区间与现状不符"，而是被漏掉的**真依赖**：`greenlet`（来自 `sqlalchemy[asyncio]` 的 extra，`sqlalchemy.ext.asyncio` 的 import 硬前提）与 `psycopg-binary`（来自上面这条 `psycopg[binary,pool]` 自己的 `[binary]` extra）。见 §1.4 的更正表。
  2. `aiosqlite` 的上界从原建议的 `<1.0` 收紧为 **`<0.22`**，且是**必须**的：0.22 起 `Connection` 不再继承 `threading.Thread`，`is_alive()` 消失，而 `langgraph-checkpoint-sqlite` 2.0.11 的 `AsyncSqliteSaver.setup()` 仍在调它（该上游把 `aiosqlite>=0.20` 声明成不带上下界，是个漏洞）。不钉上界，`graph.ainvoke` 首次懒 setup 即 `AttributeError` —— 本阶段实施时实际踩到。这不是"放宽区间"，是加了一条**兼容性上界**；上游修好后可解除。
  3. `sqlalchemy` 由裸声明改为 **`sqlalchemy[asyncio]`**（即第 1 点的 greenlet）。
- 备选（本文件当前默认）：一行不改，只把耦合写进文档（§1.3 / §1.4）。
- 需人确认：是否现在就把依赖面收紧。**代价很不对称** —— 三条声明 vs 一个可能静默崩的上游变更。

### 10. SQLite 是否开启 `PRAGMA foreign_keys=ON`

SQLite **默认不校验外键**，Postgres 校验。不开的话，"先写子行后写父行"在本机跑得过、
到了生产才炸 —— 典型的 dev/prod 分叉。

- **推荐**：开启，与 PG 对齐。做法与 §4.1 的方言数据一致：`config` 提供 `connect_pragmas_for(url)`（SQLite 返回 `("PRAGMA foreign_keys=ON",)`，PG 返回空元组），`db/session.py` 只 `for` 一遍 —— 仍是零分支、零方言字样，§4.10 第 12 条断言不受影响。
- 代价：测试必须先落父行。§4.9 的 `test_idempotency.py` **已经**先建了 `users` / `sessions`，不受影响。
- 备选：不开，接受分叉。
- 注：WAL **不受这条影响** —— 它是库文件级持久属性，由检查点 `setup()` 设一次即可（§4.1）。
- 需人确认：把"FK 约束真的生效"加进 §4.8 的断言（`PRAGMA foreign_keys` 读回来必须是 `1`）。

### 11. SQLite 丢 `tzinfo` 要不要在 ORM 层补回来

`DateTime(timezone=True)` 在把 SQLite 只当存储时读回来是 **naive** datetime（SQLite 没有时区类型），PG 上是真 `timestamptz`。

- **推荐（本文件当前默认）**：不加工。写入端统一 `datetime.now(UTC)`；读取端的归一化（补 tzinfo）交给阶段四的序列化层 —— 它本来就负责把领域对象变成 JSON，那是唯一该关心"外部看到的字符串长什么样"的地方。在 ORM 层再补一次会造出第二套时间语义。
- 备选：一个 `UTCDateTime` TypeDecorator（bind 时强制 UTC、result 时补 tzinfo），把差异收在 `db/models.py` 内。
- 需人确认：阶段四的序列化层是否接受"从库读出来的时间可能是 naive"，并负责归一化。

---

## 8. 为后续阶段预留的接口

阶段三结束时，下列"缝"必须已经存在，否则后续阶段会以错误的方式重造它们：

| 缝 | 阶段三提供 | 被谁消费 |
|---|---|---|
| 检查点 | `agent.checkpointer.open_checkpointer()` / `setup_checkpointer()` | 阶段四的 FastAPI `lifespan`、阶段六/七/八的每张图 `compile(checkpointer=...)` |
| 长期记忆 | `agent.store.open_store()` / `setup_store()` | 阶段八 `services/memory.py`（持久化一律经它，不另开向量库） |
| 业务读写 | `db.repositories.*`（唯一 SQL 出口，写入带 `idempotency_key`） | 阶段四/六/七/八 的 service 与图节点 |
| 引擎与 Session | `db.session.create_engine()` / `create_session_factory()` / `open_session()` | 阶段四的依赖注入 |
| 会话身份 | `sessions.id` == LangGraph `thread_id` | 阶段四/六（`config={"configurable": {"thread_id": ...}}`） |
| Token 账本 | `token_ledger` 表；列名 == `llm.tracing.UsageSnapshot` == §3.1 `meta.usage` | 阶段四信封的 `usage`、阶段八 `/stats` 与单日熔断 |
| 路径常量 | `config.DB_PATH`（唯一） | 全部需要"同一个库"的断言 |
| 迁移 | `alembic upgrade head` 一次建齐业务表 + LangGraph 表（幂等，可重跑） | 阶段八 `docker compose up` 的从零建库 |
| 表结构漂移探针 | `alembic check`（`env.py` 的 `include_object` 已把 LangGraph 的表排除在外） | 每个后续阶段改完 `db/models.py` 后复跑 |
| 幂等模式 | `idempotency_key` UNIQUE + `record_*` 返回 `bool` | 阶段七的 `sandbox_tools.py`（检查点膨胀截断处的同款模式） |

阶段三**不**为后续阶段留任何空函数、空类或 TODO 占位 —— 那些是平行实现的种子。缝只以"唯一入口 + 已归一化的数据 + 守护测试"的形式存在。
