# SPEC.md — 阶段一 · 工程底座 · 实施规格

本文件是 **ROADMAP.md「阶段一」的施工细则**，回答"这一阶段怎么建、建成什么样才算完"。

- `AGENTS.md` 是**契约**（不可协商）。本文件不重新定义契约，只把它翻译成可执行的任务。
- `ROADMAP.md` 是**施工顺序**。本文件的每个任务都能追到 ROADMAP「阶段一」的某条产出。
- 三者冲突时以 `AGENTS.md` 为准；本文件与 `AGENTS.md` 冲突，说明本文件写错了。

**阶段一不交付版本，交付"能开工的地基"。** 验收标准是五条命令全绿，不是功能可用。

---

## 1. 范围边界

### 做

- 目录骨架：与 `AGENTS.md` §2 的目标布局一致，**只建拥有者文件与包**。
- `pyproject.toml`：唯一依赖来源 + ruff / mypy / pytest 三件套配置。
- `config.py`：唯一配置入口（模型 ID 常量、`PRICE_TABLE`、`DATABASE_URL`、沙箱上限）。
- `logging.py`：唯一 structlog 初始化（JSON、绑定 `request_id` / `session_id` / `user_id`）。
- `.gitignore` / `.env.example` / `docker-compose.yml` 骨架 / `data/` 占位。
- 两个守护测试：`test_smoke.py`（证明包能装能 import）、`test_contract_guardrails.py`（把 §2/§4 的禁区长进 CI）。

### 不做（明确排除，避免"顺手加一下"）

| 不建 | 理由 |
|---|---|
| `utils.py` / `helpers.py` / `common.py` | §2 明令禁止的转口模块，会变成事实上的第二所有者 |
| 第二个 settings 模块 | §2 单点所有权：`config.py` 是唯一配置入口 |
| `llm/prompts/`、`agent/state.py`、`api/schemas.py` 等空占位 | 空文件会变成"顺手实现"的邀请；各自阶段再建 |
| `frontend/`、`frontend_telegram/`、`migrations/` | 分别是阶段八、阶段八、阶段三的产物 |
| 任何 `ChatDeepSeek` / `ChatOpenAI` 的构造代码 | 阶段二的 `llm/client.py` 才有权构造 |
| `tests/__init__.py` | 让 `tests/` 保持非包，避免 pytest import-mode 的模块重名问题 |
| `sentence-transformers` 依赖 | 向量模型选型未定（ROADMAP 待定项 1），阶段五再引入 |

---

## 2. 前置核查结果（本机实测，2026-10-03）

| 项 | 实测 | 与 ROADMAP「已核实环境」的差异 | 影响 |
|---|---|---|---|
| Python | 3.12.7（`python --version`） | 一致 | 满足 §1 的 3.12+ |
| uv | 0.11.16 | 一致 | — |
| git | 2.54.0.windows.1 | 一致 | — |
| **git 仓库** | **不存在**（`git rev-parse` 报 not a repository） | ⚠️ ROADMAP 未提 | 阶段一要补 `git init`，且 `git init` 必须**排在 `.gitignore` 之后** |
| **`.env` 已存在且含真实 key** | 存在（已确认文件名，未读取内容以外的任何回显） | ⚠️ ROADMAP 记为"未设置" | 说明 roadmap 写完后被手工写入。**该文件必须被 `.gitignore` 拦住**；若此 key 曾以任何形式外泄，需轮换 |
| **当前 shell 有激活的 conda 环境** | `D:\Anaconda\envs\MLText`（Python 3.12.9） | ⚠️ ROADMAP 未提 | `uv sync` 会另建项目内 `.venv` 并优先使用它；**不要把依赖装进 conda 环境**，两者的包会互相遮蔽 |
| Docker | ✅ 已安装，2026-10-03 复查（29.8.1 / Compose v5.5.1 插件形态 / WSL2 Ubuntu） | ⚠️ ROADMAP 已同步 | `docker-compose.yml` 阶段一**可真机验证**。前置：须配 `registry-mirrors`（Docker Hub 直连超时） |
| `.venv` | 不存在 | 一致 | `uv sync` 首次创建 |

> 核查命令：`python --version`、`uv --version`、`git --version`、`git rev-parse --show-toplevel`、`uv pip list`。

---

## 3. 目标文件清单

`✓` = 阶段一新建；`·` = 空占位（`.gitkeep`）；其余为既有文件。

```
python助手/
├── AGENTS.md                    （既有，只读）
├── ROADMAP.md                   （既有）
├── SPEC.md                      本文件
├── ai-clever-bubble.md          （既有，只读）
├── .env                         （既有，必须被忽略）
├── .env.example            ✓    只列变量名
├── .gitignore              ✓
├── .python-version         ✓    内容：3.12
├── pyproject.toml          ✓    唯一依赖来源
├── uv.lock                 ✓    uv sync 生成，**提交**
├── docker-compose.yml      ✓    骨架，sandbox 挂 profiles
├── data/
│   ├── corpus/.gitkeep     ·
│   └── chroma/.gitkeep     ·
├── docs/decisions/.gitkeep ·
├── experiments/.gitkeep    ·
├── src/pyassist/
│   ├── __init__.py         ✓
│   ├── config.py           ✓    ★ 唯一配置入口
│   ├── logging.py          ✓    ★ 唯一日志初始化
│   ├── llm/__init__.py     ✓
│   ├── rag/__init__.py     ✓
│   ├── agent/__init__.py   ✓
│   ├── agent/graphs/__init__.py   ✓
│   ├── agent/tools/__init__.py    ✓
│   ├── sandbox/__init__.py ✓
│   ├── db/__init__.py      ✓
│   ├── services/__init__.py ✓
│   ├── api/__init__.py     ✓
│   ├── api/routers/__init__.py    ✓
│   └── evals/__init__.py   ✓
└── tests/
    ├── test_smoke.py       ✓
    └── test_contract_guardrails.py  ✓
```

所有 `__init__.py` **保持为空**（不写 re-export）。理由：空包不构成平行实现，而 `from pyassist.config import settings` 这类绝对导入（§3.3 风格）本来就不需要 re-export。

---

## 4. 任务分解

| # | 任务 | 产出 | 预估 |
|---|---|---|---|
| T1 | 先拦密钥，再建仓 | `.gitignore`、`git init`、首次提交 | 10 min |
| T2 | 目录骨架 | 13 个 `__init__.py` + 4 个 `.gitkeep` | 10 min |
| T3 | `pyproject.toml` + `uv sync` | 依赖、`uv.lock` | 20 min |
| T4 | `config.py` | 唯一配置入口 | 25 min |
| T5 | `logging.py` | 唯一日志初始化 | 25 min |
| T6 | `.env.example` / `docker-compose.yml` / `.python-version` | 工程外壳 | 15 min |
| T7 | 两个测试 | `test_smoke.py`、`test_contract_guardrails.py` | 40 min |
| T8 | 五条验收命令 + 提交 | 绿的阶段一 | 15 min |

**合计约 2.5–3 小时。** 无阻塞项。

### T1 · 先拦密钥，再建仓

顺序不能反 —— `.env` 里有真实 key，先 `git init && git add -A` 会把 key 写进历史，之后只能靠 filter-branch 或换 key 挽回。

1. 写 `.gitignore`（内容见 §5.4）。
2. `git init`
3. `git add -A && git status --short` → **确认输出里没有 `.env`**。
4. 首次提交：`chore: 阶段一 工程底座骨架`。

> 若 `git status --short` 出现 `.env`，**停止**，先修 `.gitignore`，不要提交。

### T2 · 目录骨架

按 §3 清单创建。`.gitkeep` 只放在"必须有、但还没有内容"的目录里；有 `__init__.py` 的包不需要 `.gitkeep`。

### T3 · `pyproject.toml` 与 `uv sync`

按 §5.1 的草案写全量依赖，然后：

```bash
uv sync
```

- **一次性声明全部运行时依赖**（含后期阶段才用的），理由：`uv sync` 只做一次解析、只留一个 `uv.lock`；解析冲突在空仓库里暴露，比混在阶段二/三的代码改动里排查便宜得多。
- 若某个约束解析不到（`langgraph>=1.1` 等是契约里的前瞻约束，PyPI 未必已有）：
  **不要静默放宽约束**。放宽 = 改契约（§6），必须写 ADR 到 `docs/decisions/` 并在此处汇报，由人拍板。见「待定项 1」。
- `uv.lock` **要提交**（可复现构建）；`pyproject.toml` 仍是唯一依赖来源（§1），lock 只是它的产物。
- `.python-version` 写 `3.12`，让 uv 固定解释器，不受 conda 影响。

### T4 · `config.py`

按 §5.2 的结构实现。两条设计决定：

1. **常量与 Settings 分工**：随代码评审变更的（模型 ID、价格、沙箱上限）是模块常量；随部署环境变更的（API key、DB URL、日志级别）进 `Settings`。二者都在 `config.py`，仍然只有一个配置入口。
2. **缺 `DEEPSEEK_API_KEY` 不得让 `import pyassist` 失败**：key 类型为 `SecretStr | None = None`，**导入期不校验**，只在阶段二的 `llm/client.py` 真正取模型时报错。否则 `uv run pytest` 在没有 key 的机器上直接崩，阶段一的验收无从谈起。

### T5 · `logging.py`

按 §5.3 的结构实现。要求：

- `structlog.configure(...)` 全局只在这里调一次（唯一初始化）。
- 输出 `JSONRenderer`（生产）与 `ConsoleRenderer`（本地调试）二选一，由 `settings.log_json` 决定。
- `request_id` / `session_id` / `user_id` 用 `structlog.contextvars` 绑定 —— contextvars 对 async 任务天然隔离，阶段四的中间件才能"一次调用串起来"。
- 用 `ProcessorFormatter` 把 stdlib `logging`（uvicorn、sqlalchemy 的日志）也接到同一个渲染器上。**这条现在不做，阶段四再补就得动 uvicorn 的启动配置**。

### T6 · 工程外壳

- `.env.example`：只列变量名（§4）。变量集：`DEEPSEEK_API_KEY`、`DEEPSEEK_API_BASE_URL`、`DATABASE_URL`、`LOG_LEVEL`、`LOG_JSON`、`EMBEDDING_MODEL_ID`、`DAILY_TOKEN_LIMIT`。**不含任何真实值，也不含已停用模型名**。
- `docker-compose.yml`：`api` / `web` / `sandbox` 三服务骨架，`sandbox` 加 `profiles: [sandbox]` 先挂起（ROADMAP 阶段一要求）。**Docker 已于 2026-10-03 装好（29.8.1），此文件可真机验证**：跑 `docker compose config` 确认解析无误。**不要写顶层 `version:` 字段**——Compose 已是 v5，该字段早已移除。
- `.python-version`：`3.12`。

### T7 · 两个测试

见 §5.5、§5.6。这是阶段一唯一有"判断逻辑"的产出，是 §2/§4 禁区的第一道自动闸门。

### T8 · 验收

跑 §6 的全部命令。`uv run pytest` 必须是**退出码 0**，不是 5（"没收集到用例"也是 5，ROADMAP 特别点出过这个坑）。

---

## 5. 关键文件规格

### 5.1 `pyproject.toml`

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "pyassist"
version = "0.1.0"
description = "Python AI 学习助手：文档问答 / 错误诊断 / 练习批改 / 学习路径"
requires-python = ">=3.12"
# readme 暂不声明：README.md 是阶段八产物（§5.6），此处声明会让构建失败
dependencies = [
  # Agent 编排（阶段二起）
  "langgraph>=1.1,<2.0",
  "langchain-core>=1.3,<2.0",
  "langchain-deepseek>=1.0",
  # 检查点（阶段三）
  "langgraph-checkpoint-sqlite>=2.0,<3.0",
  "langgraph-checkpoint-postgres>=2.0,<3.0",
  # LLM 传输：仅启动时模型名校验用，业务代码不得直调（§2）
  "openai>=1.50,<2.0",
  # API（阶段四 / 六）
  "fastapi>=0.115,<1.0",
  "uvicorn[standard]>=0.30",
  "sse-starlette>=2.1",
  "slowapi>=0.1.9",
  # 数据（阶段三 / 五）
  "pydantic>=2.9,<3.0",
  "pydantic-settings>=2.4,<3.0",
  "sqlalchemy>=2.0,<3.0",
  "alembic>=1.13",
  "chromadb>=0.5",
  # 通用（阶段一起就在用）
  "jinja2>=3.1",
  "structlog>=24.1",
  "httpx>=0.27",
  "python-dotenv>=1.0",
  "docker>=7.1",
  # 前端（阶段八）
  "streamlit>=1.57",
]

[dependency-groups]
dev = [
  "pytest>=8",
  "pytest-asyncio>=0.24",
  "respx>=0.21",
  "ruff>=0.6",
  "mypy>=1.11",
]

[tool.hatch.build.targets.wheel]
packages = ["src/pyassist"]

# ---------- lint ----------
[tool.ruff]
line-length = 100
target-version = "py312"
src = ["src", "tests"]
extend-exclude = ["data", "migrations/versions"]   # 运行产物 / alembic 自动生成

[tool.ruff.lint]
select = [
  "E", "W",     # pycodestyle
  "F",          # pyflakes
  "I",          # import 排序
  "N",          # 命名规范（§3.3）
  "UP",         # pyupgrade，锁 3.12 语法
  "B",          # bugbear
  "A",          # 内建名遮蔽
  "C4",         # comprehension
  "SIM",        # 化简
  "PT",         # pytest 风格
  "RUF",        # ruff 自有规则
  "ASYNC",      # 异步陷阱（阶段六的流式铁律靠它兜底）
  "S",          # bandit：exec/eval/subprocess 等（§4 禁区）
]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]        # 测试里 assert 是本体
"experiments/**" = ["S", "T20"]   # 一次性实验：允许 print、允许宽松（§3.5）

# ---------- 类型 ----------
[tool.mypy]
python_version = "3.12"
strict = true
mypy_path = "src"
packages = ["pyassist"]
plugins = ["pydantic.mypy"]

# ---------- 测试 ----------
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q --strict-markers --strict-config"
markers = [
  "docker: 需要真实 Docker 守护进程（阶段七）",
]
```

要点说明：

- `S`（bandit）**现在就要开**：§4 禁止 `exec()` / `eval()` / 无隔离跑用户代码，让 linter 从第一天就替我们盯着，比阶段七再补便宜。
- `ASYNC` 规则集同理：§2 的"async 路由里禁止同步 `graph.stream()`"这条铁律，靠人记不如靠 linter。
- `--strict-markers` + 预注册 `docker` marker：阶段七要跑 `pytest -m docker`，现在登记好，将来写错 marker 名会直接报错而不是静默通过。
- **只有 `src/pyassist` 受 `mypy --strict` 约束**，`tests/` 不在其中 —— 不必给测试写全类型注解。
- 不启用 `D`（docstring 强制）与 `ANN`（注解强制）：`mypy --strict` 已覆盖注解，`D` 会让阶段一的大部分时间花在写套话上。见「待定项 2」。

### 5.2 `src/pyassist/config.py`

```python
"""唯一配置入口（§2）。其他模块一律从此处取配置，不得另建 settings 模块。"""
from __future__ import annotations

from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DATA_DIR: Final = REPO_ROOT / "data"

# ---------- 模型 ID：全项目只允许出现在这里（§1 / §2）----------
MODEL_FLASH: Final = "deepseek-v4-flash"
MODEL_PRO: Final = "deepseek-v4-pro"
EXPECTED_MODEL_IDS: Final[frozenset[str]] = frozenset({MODEL_FLASH, MODEL_PRO})

# ---------- 端点 ----------
DEEPSEEK_API_BASE_URL: Final = "https://api.deepseek.com"

# ---------- 单价表：USD / 1M tokens（§1）。仅用于量级判断与成本告警 ----------
class PriceTier(BaseModel):
    """单模型三个单价。显式存峰值/非峰值两列，避免在计价处做浮点乘法。"""
    cache_hit_off_peak: Decimal
    cache_hit_peak: Decimal
    input_miss_off_peak: Decimal
    input_miss_peak: Decimal
    output_off_peak: Decimal
    output_peak: Decimal

# ---------- 错峰窗口：UTC 周一至周五（§1）----------
PEAK_WINDOWS_UTC: Final[tuple[tuple[dt_time, dt_time], ...]] = (
    (dt_time(1, 0), dt_time(4, 0)),
    (dt_time(6, 0), dt_time(10, 0)),
)

# ---------- 沙箱上限（§5.3）。数值待实测校准，见「待定项 4」----------
class SandboxLimits(BaseModel):
    timeout_seconds: int = 10
    memory_mb: int = 512
    cpus: float = 1.0
    output_head_lines: int = 100   # 超长输出截断：保留头尾（§2 检查点膨胀）
    output_tail_lines: int = 100
    is_network_enabled: bool = False
    is_rootfs_read_only: bool = True
    is_non_root: bool = True

SANDBOX_LIMITS: Final = SandboxLimits()

# ---------- 环境相关配置 ----------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    deepseek_api_key: SecretStr | None = None   # 导入期不校验，见 §4 T4
    deepseek_api_base_url: str = DEEPSEEK_API_BASE_URL
    database_url: str = f"sqlite+pysqlite:///{DATA_DIR / 'app.db'}"
    log_level: str = "INFO"
    log_json: bool = True
    embedding_model_id: str | None = None        # 阶段五拍板（ROADMAP 待定项 1）
    daily_token_limit: int | None = None         # ROADMAP 待定项 5

settings = Settings()
```

三条要点：

1. `DATABASE_URL` 默认值用**绝对路径**（基于 `REPO_ROOT`），不用 `data/app.db` 相对路径。阶段三要程序化断言"检查点目标 == `db/session.py` 目标 == `data/app.db`"，相对路径会让断言随 CWD 漂移。
2. 价格用 `Decimal` 不用 `float`。`usage.cost_usd` 在信封里是 JSON number，转换发生在阶段四的序列化边界，不在这里。
3. `PEAK_WINDOWS_UTC` 与 `PRICE_TABLE` 是**数据**；判断"现在是否错峰"的函数属于记账逻辑，放阶段二的 `llm/tracing.py`，不在 `config.py` 里写业务函数。

### 5.3 `src/pyassist/logging.py`

```python
"""唯一 structlog 初始化（§2 / §3.5）。其他模块只调用 get_logger()，不得自行 configure。"""
```

需要包含：

- `configure_logging(level: str, *, json_output: bool) -> None`：幂等（`structlog.configure` 本身可重复调用，后者覆盖前者）。
- `get_logger(name: str | None = None) -> BoundLogger`：内部用 `cast` 收窄 `structlog.get_logger()` 的 `Any` 返回 —— 否则 `mypy --strict` 的 `warn_return_any` 会在这里报错。
- `bind_request_context(*, request_id: str, session_id: str | None = None, user_id: str | None = None) -> None` / `clear_request_context() -> None`：基于 `structlog.contextvars`。
- 处理器链：`merge_contextvars` → `add_log_level` → `TimeStamper(fmt="iso", utc=True)` → `StackInfoRenderer` → `format_exc_info` → `JSONRenderer()`。
- stdlib 桥接：`structlog.stdlib.ProcessorFormatter` 挂到 root logger，让 uvicorn / sqlalchemy 的日志走同一套 JSON 渲染。
- 在模块顶部注释里写明：**`pyassist/logging.py` 不会遮蔽 stdlib 的 `logging`**（Python 3 默认绝对导入，包内 `import logging` 取的是标准库），但为避免读者困惑，`logging.py` 内部导入标准库时请 `import logging as stdlib_logging` 并显式别名。

### 5.4 `.gitignore`

```gitignore
# 密钥与本地产物（§4）
.env
*.db
data/**
!data/**/
!data/**/.gitkeep

# 虚拟环境与缓存
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/

# 编辑器
.idea/
.vscode/
*.swp
```

两个陷阱：

1. `data/**` 会连目录一起排除，git 便不再往目录里看，`!data/corpus/.gitkeep` 就失效了。必须先用 `!data/**/` 把目录放回来，再放行 `.gitkeep`。
2. `.env` 只匹配 `.env` 这一个文件名，**不会误伤 `.env.example`**。`uv.lock` 也别加进来（要提交）。

验证：

```bash
git check-ignore -v data/corpus/.gitkeep   # 期望：无输出（= 未被忽略）
git check-ignore -v .env                   # 期望：命中 .env 规则
```

### 5.5 `tests/test_smoke.py`

不作"断言常量等于自己"的空转测试。两条断言，各自验证一个真实的阶段一风险：

1. `importlib.metadata.version("pyassist")` 返回非空 → 证明**打包配置正确且已 editable 安装**（`src` 布局最容易在这里翻车）。
2. `settings.database_url.startswith("sqlite")` → 证明 `config.py` 能在**没有 API key** 的环境里成功构造。

### 5.6 `tests/test_contract_guardrails.py`

把 §2 / §4 的禁区长进 CI。设计约束：

```python
REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS  = [REPO_ROOT / "src", REPO_ROOT / "tests"]
SCAN_FILES = [
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / ".env.example",
    REPO_ROOT / ".env",
    REPO_ROOT / "pyproject.toml",
]
# 用拼接构造，避免守护测试自己被自己命中
FORBIDDEN_MODEL_IDS = ("deepseek-" + "chat", "deepseek-" + "reasoner")
```

**关键设计决定：扫描范围排除 `AGENTS.md` / `ROADMAP.md` / `SPEC.md` / `ai-clever-bubble.md`。**
契约与路线图必须在正文里点名已停用模型才能讲清楚历史；若把 `*.md` 纳入扫描，守护测试会与契约自相矛盾。扫描范围只覆盖**会真正发出去的**代码与配置面。

断言清单（标注"现在就有意义"或"预防性"）：

| # | 断言 | 依据 | 性质 |
|---|---|---|---|
| 1 | 扫描面内不出现两个已停用模型名 | §1 / §4 | 现在就有意义 |
| 2 | 不存在 `requirements.txt` | §1 单一来源 | 现在就有意义 |
| 3 | `src/` 下不存在 `utils.py` / `helpers.py` / `common.py` | §2 转口模块 | 预防性 |
| 4 | AST 扫描：`src/` 内无 `exec(...)` / `eval(...)` 调用，无 `import subprocess`、无 `os.system` / `os.popen` | §4 禁区 | 预防性（阶段七才有真风险） |
| 5 | `src/` 内除 `llm/client.py` 外不出现 `ChatDeepSeek(` / `ChatOpenAI(` | §2 单点所有权 | 预防性 |
| 6 | `src/` 内不出现 `MemorySaver` | §2 禁用 | 预防性 |
| 7 | `BaseSettings` 子类只在 `config.py` 里定义 | §2 唯一配置入口 | 预防性 |
| 8 | `pyassist/logging.py` 是 `structlog.configure` 的唯一调用点 | §2 唯一日志初始化 | 预防性 |

第 4 条**必须用 AST 而不是 grep**：注释里写"禁止使用 subprocess 顶替沙箱"是完全合法的（ROADMAP 阶段七正是这么写的），grep 会把它当违规。用 `ast.parse` 找 `ast.Call(func=ast.Name(id in {"exec","eval"}))` 与 `ast.Import(names=[...])`。

**安全细节**：断言失败信息只打印文件路径与命中的规则名，**不得回显文件内容** —— 扫描面包含 `.env`，回显即泄漏 key。

---

## 6. 验收

```bash
uv sync
uv run python -c "import pyassist"
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/pyassist
uv run pytest                      # 退出码必须为 0；5 = 没收集到用例，不算通过
```

补充两道 ROADMAP 没写但值得加的闸门：

```bash
git status --short                 # 期望：空（且从未出现过 .env）
git check-ignore -v .env           # 期望：命中忽略规则
```

### 验收判据

- [ ] `uv sync` 从干净环境成功，生成并提交 `uv.lock`
- [ ] `import pyassist` 退出码 0
- [ ] `ruff check` / `ruff format --check` 无 diff
- [ ] `mypy --strict src/pyassist` 零错误
- [ ] `pytest` 退出码 0，收集到 **2 个**测试文件、≥3 条用例
- [ ] `.env` 从未进入 git 暂存区
- [ ] `docker-compose.yml` 存在且 sandbox 带 `profiles: [sandbox]`（**可真机验证**：`docker compose config` 能解析，且默认 `docker compose up` 不启动 sandbox）
- [ ] 未创建 `utils.py` / `helpers.py` / `common.py` / 第二个 settings 模块

### 与 ROADMAP 验收的差异（三处，均需知悉）

1. ROADMAP 的阶段一验收**不含 git 相关命令**；本文件补了 `git init` 与 `git status --short`。原因：本机**还不是 git 仓库**（§2 核查），而 §4 的整个禁区体系依赖 git。
2. ROADMAP 说 `.gitignore` 内容为 `data/**`；本文件改为 `data/**` + 两条反选。原因：不反选则 `data/corpus/.gitkeep`（ROADMAP 明确要求的产出）无法提交。
3. **`docker-compose.yml` 的验证方式已放宽**（2026-10-03 复查：Docker 29.8.1 已装、WSL2 已通）。阶段一交付"骨架 + 真机验证"，不再限于文本审查。

---

## 7. 风险与缓解

| 风险 | 触发点 | 缓解 |
|---|---|---|
| 真实 API key 进 git 历史 | T1 顺序颠倒 | `.gitignore` 先于 `git init`；提交前查 `git status --short` |
| 依赖解析失败（`langgraph>=1.1` 等前瞻约束） | T3 | 不放宽约束 → 写 ADR 汇报（见待定项 1） |
| `structlog` 的类型存根不足，卡住 `mypy --strict` | T5 | 在调用点 `cast` 收窄；万不得已只对 `structlog` 加 `ignore_missing_imports` 的窄 override，并在文件头注释原因 |
| conda 环境（`MLText`）与项目 `.venv` 抢解释器 | T3 / 所有验收 | `.python-version` 固定 3.12；一律用 `uv run`，不直接 `python` |
| 守护测试误伤文档 | T7 | 扫描范围排除 `*.md`；AST 而非 grep |
| 空占位目录演变成"顺手实现" | 全程 | §1「不做」清单；`__init__.py` 保持空 |
| 阶段一把 `docker-compose.yml` 写成"看起来能跑" | T6 | 2026-10-03 起可真机校验（Docker 已装）：`docker compose config` 不通过就不算完成 |

---

## 8. 待定项

不阻塞阶段一动工，但需要人拍板。前三条在阶段一内就会遇到。

### 1. 依赖约束解析不到时怎么办（T3 可能立刻遇到）

`langgraph>=1.1,<2.0` / `langchain-core>=1.3,<2.0` 等是 `AGENTS.md` 写下的前瞻约束，PyPI 上未必已有对应的版本区间。

- **推荐**：解析失败就**如实报告并停下**，不静默放宽。放宽版本约束属于改契约（§6），需要 ADR + 人工确认。
- 备选：只装阶段一真正用得到的子集（`structlog` / `pydantic-settings` / `python-dotenv` / `pytest` / `ruff` / `mypy`），把 Agent 与 RAG 栈推到各自阶段引入 —— 代价是 `uv.lock` 会被多次重写，冲突排查与业务代码改动混在一起。**不推荐**，但可行。

### 2. ruff 是否启用 `D`（docstring 强制）与 `ANN`（注解强制）

**推荐不启用**：`mypy --strict` 已经强制注解，`D` 只会让阶段一的时间花在写套话上。将来想加，代价是一次性的批量补 docstring。

### 3. `pytest` 是否开 `filterwarnings = ["error"]`

**推荐阶段一不开**：第三方库（chromadb / streamlit）的 deprecation warning 会把测试直接打红，且与我们自己的代码质量无关。阶段四引入 API 层时再评估。

### 4. 沙箱上限的具体数值（T4 写进 `config.py`）

`timeout_seconds=10` / `memory_mb=512` / `cpus=1.0` / 头尾各 100 行 是**提案值**，未经实测。

- 阶段一只声明常量，不影响任何逻辑。
- 阶段七有了真实 Docker 后需按"修复通过率 ≥ 70%"反推校准。**校准前不要声称这些数值已验证。**

### 5. 单日 Token 熔断阈值（T4 对应 `Settings.daily_token_limit`）

`AGENTS.md` §5.5 要求单日超限有熔断，但没给数。阶段一留 `None`（= 不熔断）。见 ROADMAP 待定项 5。

### 6. `.env` 里的真实 key 是否需要轮换

`.env` 已存在于本机且含真实 key，而 ROADMAP 把它记为"未设置"—— 说明 roadmap 写完后被手工写入。阶段一只保证它被 git 忽略。

**需人确认**：这个 key 是否曾以任何形式（截图、粘贴、日志、其他仓库）外泄？若有，直接在 DeepSeek 控制台轮换。

### 7. 是否现在就 `git init`

**推荐立刻做**（T1）。理由：§4 的禁区（禁止提交 `.env` / `data/**` / `*.db`）全部依赖 git 才能落地；仓库不存在时这些规则是无牙的。

### 8. `line-length = 100` vs `88`

**推荐 100**（本项目 URL、`Field(...)` 描述、日志字段较多）。已写进 §5.1，改成 88 只是改一个数字，越早定越省事。

### 9. 包名 `pyassist` 确认

`AGENTS.md` §2 的目录树写的是 `src/pyassist/`，本文件沿用。若想换成别的名字，现在是唯一零成本的时机 —— 一旦提交，改包名要动所有导入。

---

## 9. 为后续阶段预留的接口

阶段一结束时，下列"缝"必须已经存在，否则后续阶段会以错误的方式重造它们：

| 缝 | 阶段一提供 | 被谁消费 |
|---|---|---|
| 配置入口 | `config.settings` + 模块常量 | 全部阶段 |
| 日志入口 | `logging.get_logger()` / `bind_request_context()` | 阶段四的 `request_id` 中间件 |
| 模型 ID 唯一落点 | `config.MODEL_FLASH` / `MODEL_PRO` | 阶段二 `llm/client.py` 的启动校验 |
| 错峰数据 | `config.PEAK_WINDOWS_UTC` | 阶段二 `llm/tracing.py` |
| 沙箱上限 | `config.SANDBOX_LIMITS` | 阶段七 `sandbox/runner.py` |
| 禁区闸门 | `tests/test_contract_guardrails.py` | 全程；各阶段按需追加断言 |
| 已知未决 | `config.settings.embedding_model_id` / `daily_token_limit` | 阶段五 / 阶段八 |

阶段一**不**为后续阶段留任何空函数、空类或 TODO 占位 —— 那些是平行实现的种子。缝只以"常量 + 唯一入口 + 守护测试"的形式存在。
