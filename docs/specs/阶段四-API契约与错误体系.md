# SPEC.md — 阶段四 · API 契约与错误体系 · 实施规格

本文件是 **ROADMAP.md「阶段四」的施工细则**，回答"这一阶段怎么建、建成什么样才算完"。

- `AGENTS.md` 是**契约**（不可协商）。本文件不重新定义契约，只把它翻译成可执行的任务。
- `ROADMAP.md` 是**施工顺序**。本文件的每个任务都能追到 ROADMAP「阶段四」的某条产出。
- 三者冲突时以 `AGENTS.md` 为准；本文件与 `AGENTS.md` 冲突，说明本文件写错了。
- 阶段一、二、三的规格已归档到 `docs/specs/阶段一-工程底座.md`、`docs/specs/阶段二-LLM基座.md`、
  `docs/specs/阶段三-持久化与状态双源.md`（本次归档，逐行一致）。

**阶段四不交付任何业务功能，交付"任何 router 都能安全地报错"的外壳。** 验收标准是**程序化**的：
不是"我们约定响应长这样"，而是任何一条出口 —— 成功、失败、未知路由、未捕获异常 ——
都被**同一段代码**强制装配成完整信封，且这件事被测试钉住。这也是硬规则 2 的落点：
**信封与错误映射必须先于任何 router 存在**，否则 `raise HTTPException` 会到处滋生、错误格式分叉。

---

## 0. 范围边界

### 做

- `src/pyassist/errors.py`：§3.2 异常层级 + `ErrorCode` 枚举（UPPER_SNAKE，与契约示例同形态）。**零第三方依赖**。
- `src/pyassist/api/schemas.py` ★：响应信封（`Envelope[T]` / `Usage` / `Meta` / `ErrorDetail` / `Page[T]`）+ `API_VERSION`。**契约冻结文件**。
- `src/pyassist/api/errors.py` ★：**唯一**异常 → HTTP 映射点（§4）。
- `src/pyassist/api/main.py`：`create_app()` 应用工厂 + `lifespan` + `request_id` 中间件 + slowapi 限流 + `GET /health`。
- `src/pyassist/api/deps.py`：依赖注入缝（`get_session` / `get_engine` / `build_meta`）。阶段七的沙箱假实现从这里替换。
- `src/pyassist/db/session.py` △：加引擎生命周期 `open_engine`（为了不破坏守护 #11）。
- `src/pyassist/db/repositories.py` △：加 `ping()`（为了不破坏守护 #13）。
- `src/pyassist/llm/client.py` △：`RuntimeError` → `errors.ConfigError`（阶段二留下的口子，本阶段收口）。
- `tests/conftest.py`、`tests/test_envelope.py` + `tests/snapshots/envelope.json`、`tests/test_error_mapping.py`、
  `tests/test_contract_guardrails.py` 的守护扩展（#14–#19）。
- `docs/decisions/0002-信封与异常映射.md`：记录本阶段四处有取舍的决定。

### 不做（明确排除，避免"顺手加一下"）

| 不建 | 归哪个阶段 | 理由 |
|---|---|---|
| **任何 router**（`api/routers/*.py`） | 阶段六 / 七 / 八 | 本阶段只立规则，不立路由。硬规则 2：信封先于 router。 |
| `services/*`、`agent/state.py`、`registry.py`、任何图 | 阶段六 | 没有图，就没有可挂的端点。 |
| SSE / `EventSourceResponse` / `X-Accel-Buffering` | 阶段六 | 流式与信封是两套序列化，混做会把 `response_model` 弄脏。 |
| `rag/*`、`sandbox/*` | 阶段五 / 七 | 依赖尚未定型的向量模型与 Docker。 |
| 单日 Token 熔断 | 阶段八 | 需要真实用量累积。 |
| `frontend/*`、`Dockerfile.api` | 阶段八 | 见待定项 8（`docker-compose.yml` 的注释与 ROADMAP 有出入）。 |
| `lifespan` 里打开 checkpointer / store | 阶段六 | 见待定项 7：本阶段没有任何图用它们。 |
| 为后续阶段留空函数 / TODO 占位 | —— | 缝只以「唯一入口 + 已归一化的数据 + 守护测试」形式存在，不留空壳。 |

---

## 1. 前置核查结果（本机实测 / 读 `.venv` 源码，2026-10-03）

### 1.1 已装版本

| 包 | 版本 | 用途 |
|---|---|---|
| `fastapi` | 0.142.2 | 应用工厂、依赖注入、异常处理器 |
| `starlette` | 1.7.0 | 中间件栈与 `ServerErrorMiddleware` 的实际行为 |
| `uvicorn` | 0.54.0 | 唯一 ASGI 服务器 |
| `slowapi` | 0.1.10（配 `limits` 5.8.0） | 限流 |
| `sse-starlette` | 3.5.0 | 阶段六用；本阶段**只记录**它与 `fastapi.sse` 的冲突 |
| `pydantic` | 2.13.5 | `Envelope[T]` / 模型序列化 |
| `pydantic-settings` | 2.15.0 | `config.Settings` |
| `httpx` | 0.28.1 | `TestClient` 的传输层 |

`pyproject.toml` 已声明 `fastapi>=0.115,<1.0` / `uvicorn[standard]>=0.30` / `sse-starlette>=2.1` /
`slowapi>=0.1.9` / `pydantic>=2.9,<3.0`（`pyproject.toml:36-39`）。**本阶段不新增任何依赖。**

### 1.2 契约的两处缺口与一处矛盾（逐字读过 `AGENTS.md` §3.1 / §3.2 · 均已于 2026-10-03 处理）

1. **§3.1 示例与规则矛盾**（**已修正**）：失败示例的 `meta` 里原本只有 `request_id` 与 `latency_ms`，
   没有 `usage`；而同一节的规则写明 `meta` 三字段**永远存在**、缺 `usage` 时填**零值而非省略**。
   → **已按规则改示例**：`AGENTS.md` §3.1 的失败示例补上了零值 `usage`，并就地注明"缺就填零、
   不省略字段"。本 SPEC 按规则实现（永远带零值 `usage`）。原待定项 1 / 9 关闭。
2. **§3.2 枚举缺传输层的码**（**已解决**）：`error.code` 只允许取 §3.2 的枚举值，但枚举里
   原本**没有** 404（未知路由）、405（方法不匹配）、429（slowapi 限流）、兜底 500 的码；
   而 §3.1 又禁止自造字符串。
   → **已于 2026-10-03 批准增补四个枚举值**（属新增、不改既有语义），并已落地进
   `AGENTS.md` §3.2；理由与影响记在 `docs/decisions/0002-信封与异常映射.md` 的决定 1。
   原阻塞项 B1 关闭。
3. **§3.2 的三条 200 是特例**：`RetrievalEmpty` / `SandboxTimeout` / `SandboxRejected` 映射到 **200**
   —— 契约的意思是"文档里没有答案""沙箱超时""代码被安全边界拒绝"是**正常业务结果**，不是故障。
   这要求映射表不只承载状态码，还要承载 `ok` 标志。见 4.3。

### 1.3 `Envelope` 的字段清单（从 §3.1 逆向，逐条可校验）

```
Envelope    = { ok: bool, data: object|null, error: ErrorDetail|null, meta: Meta }
Meta        = { request_id: str, latency_ms: int, usage: Usage }      ← 三字段永远在位
Usage       = { prompt_tokens, completion_tokens, cached_tokens, cost_usd, is_off_peak }
ErrorDetail = { code: <§3.2 枚举>, message: str, details: object|null }
data        = 永远是对象或 null；**禁止裸数组**；列表放 data.items + data.total；
              分页 ?limit=&cursor= 带 data.next_cursor
```

`Usage` 的字段名与 `llm/tracing.py` 的 `UsageSnapshot`（阶段二产物）**逐字一致** —— 这不是巧合，
是阶段二刻意留的缝：`extract_usage()` 的返回值可直接喂进信封，不需要再写一次映射。

### 1.4 `fastapi 0.142.2` 的三个实测事实

- **`RequestValidationError` 不再从 `fastapi` 顶层导出**，只能 `from fastapi.exceptions import RequestValidationError`；
  且它**不再继承** starlette 的 `HTTPException` —— 任何"验证错误会落到 `HTTPException` 处理器"
  的假设都不成立，必须单注册一个处理器。
- **`fastapi 0.142.2` 自带 `fastapi.sse`**（原生 `EventSourceResponse` / `ServerSentEvent`），
  与已装的 `sse-starlette 3.5.0` **同名不同 API**。阶段六只能二选一。本阶段只记录（待定项 12）。
- **slowapi 的 `Limiter` 不再接收 `app` 参数**；挂载方式是 `app.state.limiter = limiter` +
  `app.add_exception_handler(RateLimitExceeded, ...)` + `app.add_middleware(SlowAPIMiddleware)`。
  且它的**默认处理器返回 `{"error": "Rate limit exceeded: ..."}`** —— 不是本项目的信封，必须自己覆盖。

### 1.5 中间件栈与异常处理的实测结论（决定 4.x 的写法，逐条是硬约束）

| # | 实测事实 | 对实现的强制后果 |
|---|---|---|
| M1 | `starlette` 的 `ServerErrorMiddleware` 在最外层；它对未捕获异常生成响应后**会把异常再抛出去**（`starlette/middleware/errors.py` 的 `raise exc`） | 测 500 路径时 `TestClient` 必须 `raise_server_exceptions=False`，且该测试写成**同步 `def`** |
| M2 | `add_middleware` 是 `insert(0, ...)`，**后加的在外层**；`@app.middleware("http")` 同义 | `request_id` 中间件必须**最后**添加；slowapi 先加 |
| M3 | 中间件**无法给 500 响应盖响应头**：`ServerErrorMiddleware` 在用户中间件之外 | 每个异常处理器**自己**从 `request.state` 取 `request_id` 盖上 `X-Request-ID` |
| M4 | `request.state` 由 `scope["state"]` 支撑，**在异常处理器里可靠可用** | 处理器可读 `request.state.request_id`；但一律 `getattr(..., None)` 防御性读取 |
| M5 | 走到兜底处理器时，`structlog` 的 contextvars **已被清理** | 处理器**显式**传 `request_id=`，不依赖 `bind_request_context` 的残留 |
| M6 | `slowapi` 的 `sync_check_limits` 会**丢弃 `async def` 的处理器**，换上它自己的默认处理器 | `RateLimitExceeded` 的处理器**必须写成同步 `def`**，否则静默变回 `{"error": ...}` |

M1–M6 来自对已装版本源码的核对（`starlette/middleware/errors.py`、`starlette/middleware/__init__.py`、
`slowapi/middleware.py`、`slowapi/extension.py`）。这些不是"注意一下"的提示，是**写错就一定不通过验收**
的约束。其中 M1 / M3 / M6 是最容易被直觉写反的三条。

### 1.6 pydantic 2.13.5：`Decimal` → JSON 默认是**字符串**

实测 `Usage(cost_usd=Decimal("0.0004")).model_dump(mode="json")` 得到 `{"cost_usd": "0.0004"}`
（**带引号**）。而契约 §3.1 的示例是数字 `0.0004`。→ `cost_usd` 字段**必须**挂 `@field_serializer`
显式转 `float` 并四舍五入。这是本阶段唯一一处"模型默认行为与契约不符"，必须显式处理而非依赖默认。

### 1.7 上阶段留下的 13 条守护里，三条直接约束本阶段

| 守护 | 现行断言 | 对阶段四的后果 |
|---|---|---|
| #11 | `create_engine` / `create_async_engine` **只在 `db/session.py` 被调用** | `api/main.py` 的 `lifespan` **不能**自己建引擎 |
| #12 | `db/session.py` 内**不得出现 `sqlite` 字样**（含注释，`text.lower()` 全文扫描） | 新增的引擎生命周期函数必须零方言字样 |
| #13 | 裸 SQL（`text(...)` / `exec_driver_sql`）只在 `db/repositories.py` | `/health` 的探活**不能**自己写 `text("SELECT 1")` |

**已核实其匹配实现**：`_call_name(node)` 取 `f(...)` / `a.b.f(...)` 的**末段名**做**精确集合匹配**
（`ENGINE_FACTORY_NAMES = frozenset({"create_engine", "create_async_engine"})`，
`tests/test_contract_guardrails.py:246`）。所以：

- 新增的引擎生命周期助手**换个名字**（`open_engine`）即可绕开集合匹配 —— 这是**合规的**，
  因为"引擎只在 `db/session.py` 构造"这条**依然成立**，只是多了一个受控的生命周期入口，且它在同一个文件里。
- 探活 SQL 只能写在 `db/repositories.py` 的 `ping()` 里。
- 另注：所有 AST 类守护（#4–#13）的扫描面是 `_python_sources()` → **只遍历 `src/`**
  （`tests/test_contract_guardrails.py:66-91`）。文本类守护（#1/#2）才覆盖 `tests/`。
  故新增的 #14–#19 一律沿用 AST 扫法，**测试代码天然不受约束** —— 测试需要自己造 `FastAPI(...)`、
  自己触发 `HTTPException`，这不该被拦。

**解法落到两处增量**：`db/session.py` 加 `open_engine`、`db/repositories.py` 加 `ping`。
这样"引擎只有一个家、SQL 只有一个出口"从约定变成**仍然成立的事实**，而不是给守护开例外。

### 1.8 可复用的缝（已就位，不要重造）

| 缝 | 位置 | 本阶段怎么用 |
|---|---|---|
| `settings` | `config.Settings` | `log_level` / `log_json` / `database_url` 直取 |
| `get_logger` / `bind_request_context` / `clear_request_context` | `logging.py` | 中间件绑定 / 清理；处理器显式传 `request_id` |
| `UsageSnapshot` + `extract_usage` / `log_llm_response` | `llm/tracing.py` | 字段名逐字一致，直接构造 `Usage` |
| `get_last_model_check()` | `llm/client.py` | `/health` 暴露模型校验结果（ADR 0001：漂移是**告警**） |
| `open_session` | `db/session.py` | `deps.get_session` 的底座 |
| `repositories.*` | `db/repositories.py` | `ping()` 加在这里 |
| `build_engine_kwargs` / `resolve_sqlite_path` | `config.py` | `db/session.py` 内部已在用 |

### 1.9 仓库现状

- `tests/conftest.py` **不存在** —— 本阶段引入第一个（只放两个夹具：临时库路径与 app 客户端）。
- `tests/snapshots/` **不存在** —— 本阶段新建；`.gitignore` 未忽略 `tests/snapshots/**`，可直接提交。
- `data/app.db` **已存在**（阶段三的迁移跑过）—— §5 的人工 `curl /health` 步骤可直接执行。
- `Dockerfile.api` **不存在**，而 `docker-compose.yml:2` 的注释写着"api 阶段四"—— 与 ROADMAP 把部署放在
  阶段八冲突。列入待定项 8。
- 提交钩子 `scripts/check_secrets.py` 会扫"硬编码 `api_key=...` 形状"的待提交行。测试夹具里任何 key 样
  字面量必须含 `test` / `placeholder` / `example` 才会放行 —— 沿用阶段二的 `"test-only-placeholder-key"`
  写法，本阶段新写夹具时照抄。

---

## 2. 目标文件清单

`✓` 新建 ｜ `△` 修改 ｜ `★` 契约关键件

```
src/pyassist/
├── errors.py                       ✓ ★   §3.2 异常层级 + ErrorCode 枚举（零第三方依赖）
├── config.py                       （不动）  本阶段不加任何设置项；限流数值走模块常量（待定项 6）
├── api/
│   ├── __init__.py                 （已存在，空）
│   ├── schemas.py                  ✓ ★   信封模型 + API_VERSION（契约冻结）
│   ├── errors.py                   ✓ ★   唯一异常 → HTTP 映射点
│   ├── deps.py                     ✓     依赖注入缝
│   ├── main.py                     ✓     应用工厂 + lifespan + 中间件 + /health
│   └── routers/__init__.py         （已存在，空）—— 本阶段**不写任何 router**
├── db/
│   ├── session.py                  △     加 open_engine（引擎生命周期唯一入口）
│   └── repositories.py             △     加 ping（探活 SQL 唯一出口）
└── llm/
    └── client.py                   △     RuntimeError → ConfigError

tests/
├── conftest.py                     ✓     第一个 conftest：临时库 + 客户端夹具
├── test_envelope.py                ✓ ★   信封形状 + 契约快照
├── test_error_mapping.py           ✓ ★   异常 → 状态码 / ok 穷尽映射
├── snapshots/
│   └── envelope.json               ✓ ★   契约冻结快照（字段集合）
└── test_contract_guardrails.py     △     追加 #14–#19

docs/decisions/
└── 0002-信封与异常映射.md            ✓     四处取舍的记录
```

本阶段**不**创建：任何 `routers/*.py`、任何 `services/*.py`、`agent/*` 新文件、`rag/*`、`sandbox/*`、
`Dockerfile.api`。

---

## 3. 任务分解

| 任务 | 内容 | 产物 | 预估 |
|---|---|---|---|
| **T0** | 归档阶段三规格（已执行，`cmp` 校验逐行一致） | `docs/specs/阶段三-持久化与状态双源.md` | —— |
| **T1** | 写 `errors.py`：异常树 + `ErrorCode` | 1 文件 | 小 |
| **T2** | 写 `api/schemas.py`：信封模型 + `API_VERSION` | 1 文件 | 中 |
| **T3** | 加 `db/session.py:open_engine` + `db/repositories.py:ping` | 2 处增量 | 小 |
| **T4** | 写 `api/errors.py`：映射表 + 五个处理器 + 注册函数 | 1 文件 | **大**（本阶段核心） |
| **T5** | 写 `api/deps.py`：请求上下文与依赖 | 1 文件 | 小 |
| **T6** | 写 `api/main.py`：工厂 + lifespan + 中间件 + `/health` | 1 文件 | 中 |
| **T7** | 收口 `llm/client.py` 的 `RuntimeError`（及待定项 11 列出的同类点） | 1–4 处改动 | 小 |
| **T8** | 写 `conftest.py` + `test_envelope.py` + 快照 + `test_error_mapping.py` | 4 文件 | **大** |
| **T9** | 追加守护 #14–#19 + 回填 ADR 0002 的决定 2–4 | 2 文件 | 中 |

> ADR `0002` 的**决定 1**（§3.2 增补传输层码）与**决定 5**（§3.1 示例修正）已随
> 2026-10-03 的批准**提前落地**，且都已改成契约文本本身；T9 只需在决定 2–4 的代码生效后
> 回填它们（若施工中改了某条，必须回来更新 ADR）。

**T1 → T2 → T4** 是硬顺序（`ErrorCode` 先于引用它的 `schemas`，映射表先于引用它的处理器）。
T3 / T5 / T7 可与 T2 并行。T8 依赖 T1–T7 全部落地。**T4 与 T8 是本阶段的主要工作量**，其余是小活。

---

## 4. 关键文件规格

以下代码草案是**可直接施工**的形态；每节末尾的"要点"是**不可协商的约束**，
写实现时若与草案冲突，以要点为准并在 PR 里说明。

### 4.1 `src/pyassist/errors.py`（新建）

```python
"""AGENTS.md §3.2 的异常层级。

这个文件**只定义异常**，不 import 任何 Web 框架 —— 领域层（rag / sandbox / services）
要能 `raise` 它而不依赖 FastAPI。把它翻译成 HTTP 是 api/errors.py 的唯一职责。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar


class ErrorCode(StrEnum):
    """§3.2 的枚举。`Envelope.error.code` 只能取这里的值（§3.1）。

    取值的形态是 UPPER_SNAKE，与契约示例 ``error.code == "RETRIEVAL_EMPTY"`` 一致；
    而类名是 PascalCase，二者之间**有且只有** api/errors.py 的映射表做转换。
    用 StrEnum 而非 Enum：它序列化后就是字符串，少一层 .value。
    """

    CONFIG_ERROR = "CONFIG_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RETRIEVAL_EMPTY = "RETRIEVAL_EMPTY"
    CORPUS_MISSING = "CORPUS_MISSING"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
    LLM_INSUFFICIENT_BALANCE = "LLM_INSUFFICIENT_BALANCE"
    LLM_MODEL_NOT_FOUND = "LLM_MODEL_NOT_FOUND"
    LLM_BAD_RESPONSE = "LLM_BAD_RESPONSE"
    SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
    SANDBOX_REJECTED = "SANDBOX_REJECTED"
    STORE_ERROR = "STORE_ERROR"

    # ↓↓↓ 以下四条是传输层的码：由框架在路由匹配 / 限流阶段抛出，不经业务代码。
    #     §3.2 原本没有它们，而 §3.1 禁止 error.code 自造字符串，于是 404 / 405 / 429 /
    #     兜底 500 无码可用。2026-10-03 **已批准增补**（属新增，不改既有码的语义），
    #     已落地进 AGENTS.md §3.2；见 docs/decisions/0002-信封与异常映射.md 决定 1。
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """§3.2 异常树的根。**基类不直接 raise**（由 test_error_mapping.py 断言）。

    若真的抛了基类，映射表会把它当未分类错误落 500 —— 这是有意的兜底，不是漏洞。
    """

    # ClassVar 而非实例字段：子类用**完全相同的注解**覆盖它，mypy --strict 才不报错。
    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR

    # 仅"映射到 200 的三个异常"用它携带成功载荷（data）。
    # 其余异常走 details（进 error.details）。二者语义不同，不合并 —— 见要点 3。
    data: dict[str, Any] | None = None

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        # 429 / 503 的响应可能带 Retry-After；放基类避免在处理器里做 isinstance 分支。
        self.retry_after: int | None = None


class ConfigError(AppError):
    """配置缺失或不可用。→ 500（这是**运维错误**，不是用户输入错误）。"""

    code: ClassVar[ErrorCode] = ErrorCode.CONFIG_ERROR


class ValidationError(AppError):
    """业务层输入校验失败。→ 422。与 pydantic 的 ValidationError 同名但无关（待定项 10）。"""

    code: ClassVar[ErrorCode] = ErrorCode.VALIDATION_ERROR


class RetrievalError(AppError):
    """检索类异常的中转基类，**不直接抛**。"""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR


class RetrievalEmpty(RetrievalError):
    """→ **200**（§3.2）。"文档里没有"是正常业务结果，不是故障。data 携带查询回显。"""

    code: ClassVar[ErrorCode] = ErrorCode.RETRIEVAL_EMPTY

    def __init__(self, message: str = "未在语料中检索到相关内容", *, query: str | None = None) -> None:
        super().__init__(message)
        self.data = {"status": "empty", "query": query}


class CorpusMissing(RetrievalError):
    """语料库缺失。→ 503。"""

    code: ClassVar[ErrorCode] = ErrorCode.CORPUS_MISSING


class LLMError(AppError):
    """上游模型类异常的中转基类，**不直接抛**。"""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR


class LLMTimeout(LLMError):
    """→ 504。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_TIMEOUT


class LLMRateLimited(LLMError):
    """→ 429，且**透传 Retry-After**（§3.2）。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_RATE_LIMITED

    def __init__(
        self,
        message: str = "上游限流，请稍后重试",
        *,
        retry_after: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after


class LLMInsufficientBalance(LLMError):
    """→ 503。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_INSUFFICIENT_BALANCE


class LLMModelNotFound(LLMError):
    """→ 500。注意与 ADR 0001 的区别：模型名**漂移**只告警，**不存在**才抛这个。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_MODEL_NOT_FOUND


class LLMBadResponse(LLMError):
    """→ 502。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_BAD_RESPONSE


class SandboxError(AppError):
    """沙箱类异常的中转基类，**不直接抛**。"""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR


class SandboxTimeout(SandboxError):
    """→ **200**（§3.2）。超时是正常业务结果，data 带可展示的部分输出。"""

    code: ClassVar[ErrorCode] = ErrorCode.SANDBOX_TIMEOUT

    def __init__(self, message: str = "执行超时", *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.data = {"status": "timeout", "stdout": stdout, "stderr": stderr}


class SandboxRejected(SandboxError):
    """→ **200**（§3.2）。代码被安全边界拒绝是正常业务结果。"""

    code: ClassVar[ErrorCode] = ErrorCode.SANDBOX_REJECTED

    def __init__(self, message: str = "代码未通过安全检查", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.data = {"status": "rejected", "reason": reason}


class StoreError(AppError):
    """状态存储（checkpointer / store）异常。→ 500。"""

    code: ClassVar[ErrorCode] = ErrorCode.STORE_ERROR
```

**6 条要点**

1. `ErrorCode` 继承 `StrEnum`（3.12 起标准库）。它序列化后就是普通字符串，故 JSON 里是
   `"RETRIEVAL_EMPTY"` 而非 `"ErrorCode.RETRIEVAL_EMPTY"`。
2. **本文件零第三方 import**（除 `enum` / `typing`）。这是"领域层可独立 raise"的可执行形式；
   若这里出现 `from fastapi import ...`，§3.2 的分层就塌了。
3. `data` 与 `details` **是两个不同的槽**：`data` 只喂成功信封的 `data` 字段（只被那三个 200 异常使用）；
   `details` 只喂失败信封的 `error.details`。不合并 —— 合并会让"这个异常是失败还是正常结果"
   变成运行时才可判定的事。
4. **每条 `code` 的注解必须完全一致地写成 `ClassVar[ErrorCode]`**。mypy --strict 下，子类若把它写成
   裸赋值，重载检查会通过、但 `ClassVar` 会被推断成实例字段，随后 `type[AppError]` 索引映射表时报错。
5. 中转基类（`RetrievalError` / `LLMError` / `SandboxError`）**不直接抛**，由 `test_error_mapping.py`
   断言"叶子类都在映射表里、中转基类不落 200"。
6. `retry_after` 放基类：`LLMRateLimited` 与（可能的）`CorpusMissing` 都能带，避免处理器里
   `isinstance` 分支。默认 `None` = 不发该响应头。

### 4.2 `src/pyassist/api/schemas.py` ★（新建）

```python
"""★ 契约冻结（AGENTS.md §4）。

改本文件的字段 = 破坏性变更，必须同时：
  1. bump API_VERSION；
  2. 更新 frontend/ 的消费代码（阶段八）；
  3. 更新 tests/snapshots/envelope.json（否则 test_envelope.py 立刻变红）。

本文件**不 import starlette / fastapi** —— 它是纯数据契约，能被任何层引用。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, field_serializer

from pyassist.errors import ErrorCode

# 契约版本。前端与集成方按它判断兼容性。变更规则见文件头。
API_VERSION = "v1"

T = TypeVar("T")


class Usage(BaseModel):
    """§3.1 的 meta.usage。字段名与 llm/tracing.UsageSnapshot **逐字一致**（刻意的缝）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    is_off_peak: bool = True

    @field_serializer("cost_usd")
    def _serialize_cost(self, value: Decimal) -> float:
        """pydantic 2.13.5 默认把 Decimal 序列化成**字符串**（实测 "0.0004"）。

        契约示例是数字 0.0004，故必须显式转 float。
        round 到 8 位：避免 Decimal→float 的二进制尾巴进快照。
        """
        return round(float(value), 8)


class ErrorDetail(BaseModel):
    """§3.1 的 error。`code` 的类型就是枚举本身，故不可能出现枚举外的值。"""

    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class Meta(BaseModel):
    """§3.1 的 meta。三字段**永远在位**；缺 usage 时填零值而非省略（示例已同步一致）。"""

    request_id: str
    latency_ms: int
    usage: Usage = Field(default_factory=Usage)


class Envelope(BaseModel, Generic[T]):
    """§3.1 的响应信封。成功与失败**共用**这一个形状，只有 ok / data / error 三者互换。"""

    ok: bool
    data: T | None = None
    error: ErrorDetail | None = None
    meta: Meta


class Page(BaseModel, Generic[T]):
    """§3.1 的列表口径：列表放 data.items，绝不裸数组；分页带 next_cursor。"""

    items: list[T]
    total: int
    next_cursor: str | None = None


class HealthData(BaseModel):
    """GET /health 的 data。契约允许 data 是任意对象，这里定死它的形状。"""

    status: str  # "ok" | "degraded"
    api_version: str
    database_ok: bool
    model_check_ok: bool | None  # None = 尚未校验过（进程刚起）
    checked_at: str  # ISO8601


# ---------- 信封构造器（模块级函数，不是 classmethod）----------
# 理由：泛型 classmethod 的返回类型在 mypy --strict 下推断得很差
# （cls 参数无法绑定 T）。模块级函数 + PEP 695 泛型参数能精确推断。


def envelope_success[T](data: T, *, meta: Meta) -> Envelope[T]:
    """成功信封。也是那三个"映射到 200"的异常要用的形状（ok=True）。"""
    return Envelope[T](ok=True, data=data, error=None, meta=meta)


def envelope_failure(error: ErrorDetail, *, meta: Meta) -> Envelope[dict[str, Any]]:
    """失败信封。data 恒为 None（§3.1：失败时 data 不承载业务载荷）。"""
    return Envelope[dict[str, Any]](ok=False, data=None, error=error, meta=meta)
```

**6 条要点**

1. **`API_VERSION` 必须存在且被 `create_app` 用上**（`FastAPI(version=API_VERSION)`）。
   它是"契约冻结"的版本锚点，不是装饰。
2. `Usage` 字段名与 `UsageSnapshot` 逐字一致，且**一律给默认零值** —— 这样 `Meta()` 无需参数即可构造，
   任何处理器都能在缺用量信息时给出合规信封。
3. `cost_usd` 的 `field_serializer` **不是可选优化**：不写它，JSON 里就是字符串，与契约示例不符。
   `round(float(v), 8)` 而非裸 `float(v)`：避免浮点尾巴污染快照。
4. `Envelope` 与 `Page` 是 `Generic[T]`，但列表端点返回 `Envelope[Page[Item]]`，
   **不是** `Page[Item]`，也不是 `Envelope[list[Item]]` 当 `response_model`。
5. 构造器是**模块级函数**（`envelope_success` / `envelope_failure`），用 PEP 695 的 `def f[T](...)` 语法；
   不要写成 `@classmethod def success(cls)`，泛型 `cls` 在 mypy --strict 下推断不出 `T`。
6. **本文件不得 import `JSONResponse` 或 starlette**。序列化到响应体是 `api/errors.py` 的事；
   `schemas.py` 只描述形状。守护 #17 会断言这一点。

### 4.3 `src/pyassist/api/errors.py` ★（新建）

```python
"""★ 唯一异常 → HTTP 映射点（AGENTS.md §4：在别处 raise HTTPException 会让错误格式分叉）。

全项目**只有这个文件**：注册异常处理器、构造 JSONResponse、把异常翻译成状态码。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from pyassist.api.schemas import ErrorDetail, Envelope, Meta, envelope_failure, envelope_success
from pyassist.errors import (
    AppError,
    ConfigError,
    CorpusMissing,
    ErrorCode,
    LLMBadResponse,
    LLMInsufficientBalance,
    LLMModelNotFound,
    LLMRateLimited,
    LLMTimeout,
    RetrievalEmpty,
    SandboxRejected,
    SandboxTimeout,
    StoreError,
    ValidationError,
)
from pyassist.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Mapping:
    """一条映射：状态码 + 信封的 ok 标志。

    ok 必须进映射表而不是写死 False —— 因为 §3.2 有三个异常映射到 **200 且 ok=True**。
    """

    status: int
    ok: bool


# §3.2 的异常树 → HTTP。**必须覆盖每一个叶子类**（由 test_error_mapping.py 穷尽断言）。
# 键是异常类，不是枚举 —— 中转基类不必逐行写，AppError 那一行兜住它们。
_MAPPING: dict[type[AppError], _Mapping] = {
    ConfigError: _Mapping(500, False),
    ValidationError: _Mapping(422, False),
    RetrievalEmpty: _Mapping(200, True),          # ← 正常业务结果，不是 500
    CorpusMissing: _Mapping(503, False),
    LLMTimeout: _Mapping(504, False),
    LLMRateLimited: _Mapping(429, False),
    LLMInsufficientBalance: _Mapping(503, False),
    LLMModelNotFound: _Mapping(500, False),
    LLMBadResponse: _Mapping(502, False),
    SandboxTimeout: _Mapping(200, True),          # ← 同上
    SandboxRejected: _Mapping(200, True),         # ← 同上
    StoreError: _Mapping(500, False),
    # 兜底：AppError 自身与三个中转基类。子类未登记时沿 MRO 落到这里。
    AppError: _Mapping(500, False),
}


def _resolve(exc: AppError) -> _Mapping:
    """沿 MRO 找第一条命中：这样阶段六若给 LLMTimeout 加子类，也不必回头改映射表。"""
    for klass in type(exc).__mro__:
        mapping = _MAPPING.get(klass)
        if mapping is not None:
            return mapping
    return _Mapping(500, False)   # 理论上不可达：AppError 已在表里


def _request_id_of(request: Request) -> str:
    """防御性读取：处理器可能在任何中间件之外被调用。"""
    return getattr(request.state, "request_id", None) or "unknown"


def _latency_ms_of(request: Request) -> int:
    started = getattr(request.state, "started_at", None)
    if started is None:
        return 0
    return int((time.perf_counter() - started) * 1000)


def _meta_of(request: Request) -> Meta:
    """零值 usage：契约要求 meta.usage 永远在位，失败时也不例外（§3.1，示例已同步）。"""
    return Meta(request_id=_request_id_of(request), latency_ms=_latency_ms_of(request))


def _json(
    status: int,
    envelope: Envelope[Any],
    request: Request,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """所有出口的**唯一**构造点。

    为什么不用 JSONResponse(content=envelope)：pydantic 模型不是 JSON 可序列化对象。
    必须 model_dump(mode="json") —— 且必须是 mode="json"，否则 Decimal 不走 field_serializer。

    为什么要自己盖 X-Request-ID：500 由 ServerErrorMiddleware 生成，它在用户中间件**之外**，
    中间件盖不上头（实测 M3）。所以每个出口自己盖。
    """
    out_headers = {"X-Request-ID": _request_id_of(request)}
    if headers:
        out_headers.update(headers)
    return JSONResponse(
        status_code=status,
        content=envelope.model_dump(mode="json"),
        headers=out_headers,
    )


def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """§3.2 异常树的总出口。同步 def：见要点 4。"""
    mapping = _resolve(exc)
    meta = _meta_of(request)

    if mapping.ok:
        # 映射到 200 的三个异常：正常业务结果，data 由异常自带的载荷派生。
        payload = exc.data if exc.data is not None else {"status": "ok"}
        envelope: Envelope[Any] = envelope_success(payload, meta=meta)
    else:
        envelope = envelope_failure(
            ErrorDetail(code=exc.code, message=exc.message, details=exc.details),
            meta=meta,
        )

    logger.warning(
        "api.error",
        request_id=meta.request_id,          # 显式传：兜底时 contextvars 已被清理（M5）
        error_code=exc.code.value,
        status_code=mapping.status,
        error_type=type(exc).__name__,
        # 不记 message 全文：可能含用户代码片段（§4 安全约束）
    )

    headers: dict[str, str] = {}
    if isinstance(exc, LLMRateLimited) and exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)   # §3.2 要求透传
    return _json(mapping.status, envelope, request, headers or None)


def _handle_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """pydantic 校验失败 → 422。

    实测：它**不再继承** starlette 的 HTTPException，必须单注册（1.4）。
    """
    meta = _meta_of(request)
    envelope = envelope_failure(
        ErrorDetail(
            code=ErrorCode.VALIDATION_ERROR,
            message="请求参数不合法",
            # 只回显 pydantic 的 loc/msg，**不回显 input** —— input 可能是用户代码。
            details={
                "errors": [
                    {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
                    for e in exc.errors()
                ]
            },
        ),
        meta=meta,
    )
    logger.warning(
        "api.error",
        request_id=meta.request_id,
        error_code=ErrorCode.VALIDATION_ERROR.value,
        status_code=422,
    )
    return _json(422, envelope, request)


def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """starlette 自产的 404 / 405 也要落进信封。

    这两条是**框架**在路由匹配阶段抛的，不经过我们的代码 —— 不接管就会漏出 {"detail": ...}。
    """
    code = ErrorCode.METHOD_NOT_ALLOWED if exc.status_code == 405 else ErrorCode.NOT_FOUND
    meta = _meta_of(request)
    envelope = envelope_failure(ErrorDetail(code=code, message=str(exc.detail), details=None), meta=meta)
    logger.warning(
        "api.error", request_id=meta.request_id, error_code=code.value, status_code=exc.status_code
    )
    return _json(exc.status_code, envelope, request)


def _handle_rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """slowapi 限流 → 429。

    **必须写成同步 def**：slowapi 的 sync_check_limits 会丢弃 async 处理器、换上它自己返回
    {"error": "Rate limit exceeded: ..."} 的默认处理器（实测 M6）。写成 async 会静默绕过整条信封链路。
    """
    meta = _meta_of(request)
    envelope = envelope_failure(
        ErrorDetail(code=ErrorCode.RATE_LIMITED, message="请求过于频繁，请稍后重试", details=None),
        meta=meta,
    )
    logger.warning(
        "api.error", request_id=meta.request_id, error_code=ErrorCode.RATE_LIMITED.value, status_code=429
    )
    return _json(429, envelope, request, {"Retry-After": "60"})


def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """兜底：**唯一**一处捕获 Exception。

    §3.1 要求任何失败都带完整信封，而 FastAPI 默认 500 返回 {"detail": ...}。边界写死在这里：
      · 必须 structlog 打点并带 request_id（显式传，M5）；
      · **不外泄** traceback / 异常 message / 密钥（message 用固定文案）；
      · **不吞异常语义** —— 这不是 `except Exception: pass`（守护 #19 会检查这一点）。
    """
    meta = _meta_of(request)
    envelope = envelope_failure(
        ErrorDetail(code=ErrorCode.INTERNAL_ERROR, message="服务内部错误", details=None), meta=meta
    )
    logger.error(
        "api.unhandled_error",
        request_id=meta.request_id,
        error_type=type(exc).__name__,
        status_code=500,
    )
    return _json(500, envelope, request)


def register_exception_handlers(app: FastAPI) -> None:
    """**唯一**的处理器注册点。main.py 只调用这一行。

    注册面必须完整：少注册 RequestValidationError 就漏 422、
    少注册 StarletteHTTPException 就漏 404/405。
    """
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RateLimitExceeded, _handle_rate_limited)
    app.add_exception_handler(Exception, _handle_unexpected)
```

**9 条要点**

1. **`_MAPPING` 的完整内容**（草案里已写全）：

   | 异常类 | 状态 | ok |
   |---|---|---|
   | `ConfigError` | 500 | False |
   | `ValidationError` | 422 | False |
   | `RetrievalEmpty` | **200** | **True** |
   | `CorpusMissing` | 503 | False |
   | `LLMTimeout` | 504 | False |
   | `LLMRateLimited` | 429 | False |
   | `LLMInsufficientBalance` | 503 | False |
   | `LLMModelNotFound` | 500 | False |
   | `LLMBadResponse` | 502 | False |
   | `SandboxTimeout` | **200** | **True** |
   | `SandboxRejected` | **200** | **True** |
   | `StoreError` | 500 | False |
   | `AppError`（含中转基类，兜底） | 500 | False |

   映射表的**键是异常类，不是枚举**。中转基类不必逐行写，`AppError` 那行兜住它们即可；
   但叶子类**必须**逐行写出，否则它们的 `code`（`ClassVar`）与实际状态码会脱节。
2. `_resolve` 走 **MRO** 而非精确查表：阶段六若定义 `LLMTimeout` 的子类，不必回头改映射表。
3. `_json` 是**唯一**的响应构造点。禁止在任何地方直接 `JSONResponse(...)` 或 `return {...}`。
4. **所有处理器写成同步 `def`**（不是 `async def`）。理由有二：(a) `_handle_rate_limited` 被 slowapi
   调用时**必须**是同步的（M6）；(b) 统一写法，避免"有的处理器在事件循环里跑、有的在线程池里跑"
   的隐蔽差异。处理器内不做 IO，同步不阻塞事件循环。
5. `X-Request-ID` 由**每个出口自己盖**（在 `_json` 里），不靠中间件 —— 500 不经过用户中间件（M3）。
6. `model_dump(mode="json")` 的 `mode="json"` **不可省**：省了 `Decimal` 不走 `field_serializer`，
   `cost_usd` 会变成字符串。
7. `_handle_unexpected` 的 `message` 是**固定文案**，绝不拼接 `str(exc)` 或 traceback。真实信息只进日志，
   且日志里也只记 `error_type`，不记 message —— 可能含用户代码。
8. `_handle_request_validation_error` 同样**不回显 `input`**：pydantic 的错误里带 `input`，
   它可能就是用户提交的代码片段。
9. 本文件是所有守护的**允许清单成员**：`HTTPException` / `add_exception_handler` / `JSONResponse`
   只在这里出现（#14 / #15 / #17）。

### 4.4 `src/pyassist/api/deps.py`（新建）

```python
"""依赖注入缝。

阶段七的沙箱假实现在这里替换；阶段六的图节点在这里取 session。
本阶段只把"从请求拿到资源"的路径定死，不引入任何业务依赖。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pyassist.api.schemas import Meta, Usage
from pyassist.db.session import open_session


@dataclass(frozen=True, slots=True)
class RequestContext:
    """中间件塞进 request.state 的东西。处理器与路由都从这里取。"""

    request_id: str
    started_at: float

    @property
    def latency_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)


def get_request_context(request: Request) -> RequestContext:
    ctx = getattr(request.state, "context", None)
    if ctx is None:
        # 未经过中间件（例如直接单测处理器）：给一个自洽的降级，而不是崩。
        return RequestContext(request_id="unknown", started_at=time.perf_counter())
    return ctx  # type: ignore[no-any-return]  # request.state 是 Any


def build_meta(request: Request, *, usage: Usage | None = None) -> Meta:
    """路由构造成功信封用的入口。usage 缺省时填**零值**，不省略字段（§3.1）。"""
    ctx = get_request_context(request)
    return Meta(request_id=ctx.request_id, latency_ms=ctx.latency_ms, usage=usage or Usage())


def get_engine(request: Request) -> AsyncEngine:
    """从 app.state 取引擎。引擎**不在**这里构造（守护 #11 把它钉在 db/session.py）。"""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("引擎未初始化：lifespan 未运行（TestClient 必须是上下文管理器）")
    return engine  # type: ignore[no-any-return]


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """请求级 AsyncSession。每次请求一个，退出即关。"""
    async with open_session(get_engine(request)) as session:
        yield session
```

**4 条要点**

1. `get_engine` **不构造引擎**，只从 `app.state` 取。构造点在 `db/session.py`（守护 #11）。
   若 `app.state.engine` 缺失，说明 `lifespan` 没跑 —— 通常意味着测试里 `TestClient(app)`
   没当上下文管理器用（见 4.8 要点 1）。
2. `build_meta` 是路由**唯一**该用的 Meta 构造器：它保证 `usage` 一定在位。
3. `RequestContext` 是 `frozen` dataclass，不可变 —— 处理器与路由拿到的是同一份快照。
4. `get_request_context` 对"未经中间件"的调用做**降级**而非抛错：这让处理器能独立单测。

### 4.5 `src/pyassist/api/main.py`（新建）

```python
"""应用工厂。

**唯一**构造 FastAPI 的地方（守护 #18）。本阶段不 include 任何业务 router —— /health 用
APIRouter 定义在本文件里，作为"路由怎么写"的样板与守护的锚点。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, Request, Response
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from pyassist.api.deps import RequestContext, build_meta
from pyassist.api.errors import register_exception_handlers
from pyassist.api.schemas import API_VERSION, Envelope, HealthData, envelope_success
from pyassist.config import settings
from pyassist.db.repositories import ping
from pyassist.db.session import open_engine, open_session
from pyassist.llm.client import get_last_model_check
from pyassist.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
)

logger = get_logger(__name__)

# 限流数值：模块常量而非 Settings —— 它不是部署差异，是接口策略（待定项 6）。
DEFAULT_RATE_LIMIT = "120/minute"

_health_router = APIRouter()


@_health_router.get("/health", response_model=Envelope[HealthData])
async def health(request: Request) -> Envelope[HealthData]:
    """健康检查。**永远返回 200**，用 data.status 表达降级（ADR 0001：模型名漂移是告警）。

    数据库不可用时**不是 500** —— 否则编排器会把"上游模型漂移"当成"服务挂了"。
    """
    database_ok = False
    try:
        async with open_session(request.app.state.engine) as session:
            database_ok = await ping(session)
    except Exception:  # noqa: BLE001 —— 探活的全部意义就是"连不上也要给出信封"
        logger.warning("health.db_unreachable")

    check = get_last_model_check()
    model_ok = None if check is None else check.is_ok
    data = HealthData(
        status="ok" if (database_ok and model_ok is not False) else "degraded",
        api_version=API_VERSION,
        database_ok=database_ok,
        model_check_ok=model_ok,
        checked_at=datetime.now(UTC).isoformat(),
    )
    return envelope_success(data, meta=build_meta(request))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程级资源的唯一开合点。

    本阶段只开引擎。checkpointer / store **不在这里开**（待定项 7）：本阶段没有任何图用它们，
    过早打开会让 /health 依赖一个没人用的存储，也会把"能否起服务"与"迁移是否跑过"耦合。
    """
    configure_logging(settings.log_level, json_output=settings.log_json)
    async with open_engine(app.state.database_url) as engine:
        app.state.engine = engine
        logger.info("api.startup", api_version=API_VERSION)
        yield
    logger.info("api.shutdown")


def create_app(*, database_url: str | None = None, rate_limit_enabled: bool = True) -> FastAPI:
    app = FastAPI(title="pyassist", version=API_VERSION, lifespan=lifespan)
    app.state.database_url = database_url or settings.database_url

    # 1) 处理器：在**任何请求发生前**注册完。
    register_exception_handlers(app)

    # 2) 限流：slowapi 不接收 app 参数，走 app.state + add_middleware 三步。
    #    它查找的 RateLimitExceeded 处理器就是第 1 步注册的那个（同步的，见 M6）。
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[DEFAULT_RATE_LIMIT],
        enabled=rate_limit_enabled,
    )
    app.state.limiter = limiter

    # 3) 中间件 onion：add_middleware 是 insert(0)，**后加的在外层**（M2）。
    #    先加 slowapi（内层），后加 request_id（外层）—— 外层才能给限流的 429 也盖上 id。
    app.add_middleware(SlowAPIMiddleware)
    app.middleware("http")(_request_context_middleware)

    app.include_router(_health_router)
    return app


async def _request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """给每个请求发一个 request_id，绑定日志上下文，并在响应头回显。

    进出一律清理 contextvars：并发请求共享进程，残留会串台（M5 的另一面）。
    """
    request_id = _normalize_request_id(request.headers.get("X-Request-ID"))
    started_at = time.perf_counter()
    request.state.request_id = request_id
    request.state.started_at = started_at
    request.state.context = RequestContext(request_id=request_id, started_at=started_at)
    bind_request_context(request_id=request_id)
    try:
        response = await call_next(request)
    finally:
        clear_request_context()
    response.headers["X-Request-ID"] = request_id
    return response


def _normalize_request_id(raw: str | None) -> str:
    """接受上游透传的 id，但**做字符集与长度校验**：它会进日志与响应头。

    只放行 [A-Za-z0-9._-]，上限 64 字符；不合法就重新生成。
    （不校验等于把 header / 日志注入的口子交给调用方。）
    """
    if raw and len(raw) <= 64 and all(c.isalnum() or c in "._-" for c in raw):
        return raw
    return f"req_{uuid.uuid4().hex}"


app = create_app()
```

**7 条要点**

1. `create_app` **先注册处理器、再挂中间件、最后 include router**。任何请求发生后 `add_middleware`
   都会 `RuntimeError`（Starlette 的栈已建）。全部注册在工厂函数内完成。
2. 中间件注册顺序 = 洋葱顺序，**最后加的在最外层**（M2）。`request_id` 中间件必须最后加。
   若写反，slowapi 的 429 就带不上 `X-Request-ID`。
3. `request.state.request_id` **同时**写 `request.state.request_id`（供处理器防御性读取）
   与 `request.state.context`（供 `deps.get_request_context`）—— 两处都写，避免处理器依赖 `deps` 的类型。
4. **不要**手动设置 `limiter._exception_handler`。slowapi 的 `SlowAPIMiddleware` 在处理时查找
   `app` 上注册的 `RateLimitExceeded` 处理器；`register_exception_handlers` 注册的同步处理器
   就是它要找的那个（M6）。手动赋值会把关联搞乱。
5. 限流**必须能被测试关掉**（`rate_limit_enabled=False`）—— 否则 `pytest` 里连着打几十个请求会撞上
   429，测试变红且原因难查。`create_app` 的该参数就是为此存在。
6. `_normalize_request_id` **不是可选的洁癖**：`X-Request-ID` 会进日志与响应头，不受约束地透传
   等于把 CRLF 注入与日志伪造的机会交给调用方。字符集白名单 + 长度上限。
7. `app = create_app()` 模块级实例是给 `uvicorn pyassist.api.main:app` 用的；测试用 `create_app(...)`
   工厂**另造实例**，不污染它。

### 4.6 `db/session.py`（增量）与 `db/repositories.py`（增量）

`src/pyassist/db/session.py` 追加：

```python
@asynccontextmanager
async def open_engine(database_url: str | None = None) -> AsyncIterator[AsyncEngine]:
    """建一个引擎并在退出时 dispose。**FastAPI lifespan 专用**。

    存在的理由：契约守护 #11 把 create_engine 的调用点钉在 db/session.py 一处，
    于是"谁持有引擎的生命周期"这件事也必须在这里回答 —— api/main.py 不能自己建。
    """
    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        await engine.dispose()
```

`src/pyassist/db/repositories.py` 追加：

```python
async def ping(session: AsyncSession) -> bool:
    """探活：能跑通一条最小查询即视为存储可用。

    裸 SQL 的唯一出口是本文件（守护 #13），所以 /health 的探活语句落在这里。
    捕获 SQLAlchemyError 而非 Exception：探活失败是可预期的运维状态，不是缺陷。
    """
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("db.ping.failed")
        return False
    return True
```

**4 条要点**

1. `open_engine` 的**名字是刻意的**：守护 #11 用 `_call_name` 做**精确集合匹配**
   （`{"create_engine","create_async_engine"}`，`tests/test_contract_guardrails.py:246`），
   叫 `open_engine` 就不在集合里。这**不是**绕过守护 —— "引擎只在 `db/session.py` 构造"依然为真，
   只是多了一个受控的生命周期入口，且它在同一个文件里。
2. `open_engine` 内**不得出现 `sqlite` 字样**（含注释）—— 守护 #12 全文扫描该文件。
   方言知识一律留在 `config.build_engine_kwargs`。
3. `ping` 是**唯一**新增的裸 SQL 出口，且必须在 `db/repositories.py`（守护 #13）。
4. `ping` 捕获 `SQLAlchemyError`（新增 import），**不**捕获 `Exception`：捕获面要窄，
   `BaseException`（如 `CancelledError`）必须继续上抛。

### 4.7 `src/pyassist/llm/client.py`（增量）

```python
# 改动前（阶段二留下）：
#     raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法调用模型")
# 改动后（顶部 import from pyassist.errors import ConfigError）：
    raise ConfigError("未配置 DEEPSEEK_API_KEY，无法调用模型")
```

**3 条要点**

1. 这是阶段二 SPEC 待定项 2 明确留下的口子（"阶段四把 `client.py` 里的这一处换成 `errors.ConfigError`"），
   本阶段**必须**收。
2. `errors.py` 零第三方依赖，故 `llm/` → `errors` 的依赖方向安全（不引入 API 层）。
3. 异常 message **不含密钥本身**，只说明"未配置"。
4. 同类收口（`llm/tracing.py` 的缺价目表 `RuntimeError`、`agent/checkpointer.py` / `agent/store.py`
   的"不支持的数据库后端" `ValueError`、`config.resolve_sqlite_path` 的 `ValueError`）一并转 `ConfigError`
   —— 边界见待定项 11（**只**收这些"服务级"条件，不收纯断言型 `ValueError`）。

### 4.8 `tests/conftest.py` + `tests/test_envelope.py` + 快照（新建）

`tests/conftest.py`：

```python
"""本项目的第一个 conftest。只放"所有测试可能用到"的夹具。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from pyassist.api.main import create_app


@pytest.fixture
def tmp_database_url(tmp_path) -> str:
    """临时库。测试绝不碰 data/app.db —— 那是开发者的真实数据。"""
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def client(tmp_database_url: str) -> Iterator[TestClient]:
    """**必须当上下文管理器用**：否则 lifespan 不运行，app.state.engine 不存在。

    且 raise_server_exceptions=False：我们**刻意**测兜底 500 的信封，
    而 ServerErrorMiddleware 在生成响应后会把异常再抛出去（M1）。
    """
    app = create_app(database_url=tmp_database_url, rate_limit_enabled=False)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
```

`tests/test_envelope.py` 的关键用例：

```python
import json
from decimal import Decimal
from pathlib import Path

from pyassist.api.schemas import API_VERSION, Envelope, ErrorDetail, Meta, Page, Usage

SNAPSHOT = Path(__file__).parent / "snapshots" / "envelope.json"


def test_api_version_is_declared() -> None:
    assert API_VERSION == "v1"


def test_usage_serializes_whole_numbers() -> None:
    """pydantic 2.13.5 默认把 Decimal 序列化成字符串，必须被 field_serializer 修掉。"""
    dumped = Usage(cost_usd=Decimal("0.0004")).model_dump(mode="json")
    assert dumped["cost_usd"] == 0.0004
    assert isinstance(dumped["cost_usd"], float)


def test_meta_always_carries_usage() -> None:
    """§3.1：usage 永远在位；缺就填零值，不省略（示例与规则现已一致）。"""
    meta = Meta(request_id="req_x", latency_ms=1)
    assert meta.usage.prompt_tokens == 0
    assert set(meta.model_dump()) == {"request_id", "latency_ms", "usage"}


def test_contract_snapshot_fields_unchanged() -> None:
    """★ 契约冻结的可执行形式：字段集合一变即红。只比字段名，不比取值。"""
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = {
        "API_VERSION": API_VERSION,
        "Envelope": sorted(Envelope.model_fields),
        "Meta": sorted(Meta.model_fields),
        "Usage": sorted(Usage.model_fields),
        "ErrorDetail": sorted(ErrorDetail.model_fields),
        "Page": sorted(Page.model_fields),
    }
    for key, value in actual.items():
        assert value == frozen[key], f"{key} 的字段集合已变 —— 这是破坏性变更（§4）"


def test_health_returns_compliant_envelope(client) -> None:
    response = client.get("/health")
    body = response.json()
    assert set(body) == {"ok", "data", "error", "meta"}
    assert body["ok"] is True
    assert set(body["meta"]) == {"request_id", "latency_ms", "usage"}
    assert response.headers["X-Request-ID"].startswith("req_")


def test_unknown_route_yields_envelope(client) -> None:
    """404 也必须是完整信封，且带 meta —— 由错误处理器接管，不是默认 {"detail": ...}。"""
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"ok", "data", "error", "meta"}
    assert body["ok"] is False
    assert body["error"]["code"] == "NOT_FOUND"


def test_unhandled_exception_yields_envelope(client) -> None:
    """兜底 500 也是信封。**同步 def** + raise_server_exceptions=False 是必需的（M1）。

    为此在测试里往 app 上临时加一条会抛异常的路由（测试代码，不违反任何守护）。
    """
    def boom() -> None:
        raise RuntimeError("test-only failure")

    client.app.add_api_route("/_boom", boom)   # type: ignore[attr-defined]
    response = client.get("/_boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["message"] == "服务内部错误"   # 不回显 str(exc)
```

`tests/snapshots/envelope.json`（首次生成后提交，此后只允许"有意变更 + 同步本 SPEC"）：

```json
{
  "API_VERSION": "v1",
  "Envelope": ["data", "error", "meta", "ok"],
  "Meta": ["latency_ms", "request_id", "usage"],
  "Usage": ["cached_tokens", "completion_tokens", "cost_usd", "is_off_peak", "prompt_tokens"],
  "ErrorDetail": ["code", "details", "message"],
  "Page": ["items", "next_cursor", "total"]
}
```

**5 条要点**

1. `TestClient(app)` **必须**当上下文管理器（`with ...`）—— 否则 `lifespan` 不运行，
   `app.state.engine` 缺失，`/health` 会走降级分支而非正常分支（测试会"通过"但测错东西）。
2. 500 那条用例必须是**同步 `def`**，且客户端 `raise_server_exceptions=False`。理由是 M1：
   `ServerErrorMiddleware` 生成响应后**把异常再抛出去**，异步测试里这个再抛出的异常会被
   `pytest-asyncio` 当成测试失败上报。
3. 快照测试**只比字段名，不比取值**。取值里 `cost_usd` 是 float，比较值的测试对格式化细节敏感
   （`0.0004` vs `0.0004000000000000001`），是 flaky 之源。
4. 快照文件放 `tests/snapshots/`；`.gitignore` 未忽略该路径，可正常提交。
5. 夹具用**临时库**，绝不碰 `data/app.db`。

### 4.9 `tests/test_error_mapping.py`（新建）

```python
"""§3.2 的异常树 ↔ HTTP 状态码：**穷尽**映射。

每条用例断言 (status_code, ok)。三个 200 单独断言 —— 它们最容易被误改成 500。
"""

import pytest

from pyassist.api.errors import _MAPPING, _resolve
from pyassist.errors import (
    AppError,
    ConfigError,
    CorpusMissing,
    ErrorCode,
    LLMBadResponse,
    LLMError,
    LLMInsufficientBalance,
    LLMModelNotFound,
    LLMRateLimited,
    LLMTimeout,
    RetrievalEmpty,
    RetrievalError,
    SandboxError,
    SandboxRejected,
    SandboxTimeout,
    StoreError,
    ValidationError,
)

CASES = [
    (ConfigError("x"), 500, False),
    (ValidationError("x"), 422, False),
    (RetrievalEmpty(), 200, True),          # ← 不是 500，也不是 ok=False 的 200
    (CorpusMissing("x"), 503, False),
    (LLMTimeout("x"), 504, False),
    (LLMRateLimited("x", retry_after=3), 429, False),
    (LLMInsufficientBalance("x"), 503, False),
    (LLMModelNotFound("x"), 500, False),
    (LLMBadResponse("x"), 502, False),
    (SandboxTimeout(), 200, True),
    (SandboxRejected(), 200, True),
    (StoreError("x"), 500, False),
    (AppError("x"), 500, False),
]


@pytest.mark.parametrize(("exc", "status", "ok"), CASES, ids=lambda c: type(c).__name__)
def test_exception_maps_to_status(exc: AppError, status: int, ok: bool) -> None:
    resolved = _resolve(exc)
    assert (resolved.status, resolved.ok) == (status, ok)


def test_every_leaf_exception_is_in_the_mapping_table() -> None:
    """新增一个叶子异常却忘了登记 → 立刻红。这是映射表不腐烂的保证。"""
    intermediate = {AppError, RetrievalError, LLMError, SandboxError}

    def leaves(klass: type[AppError]) -> set[type[AppError]]:
        children = set(klass.__subclasses__())
        return {k for k in children if not k.__subclasses__()} | set().union(
            *(leaves(k) for k in children if k.__subclasses__())
        )

    missing = {k.__name__ for k in leaves(AppError) if k not in _MAPPING}
    assert not missing, f"这些叶子异常未登记到 _MAPPING：{sorted(missing)}"
    assert intermediate.isdisjoint({k for k in _MAPPING if k is not AppError})


def test_three_business_results_are_200_not_500() -> None:
    """显式重复断言，防止有人"顺手统一成 5xx"。"""
    for exc in (RetrievalEmpty(), SandboxTimeout(), SandboxRejected()):
        assert _resolve(exc).status == 200
        assert _resolve(exc).ok is True


def test_error_code_values_are_upper_snake() -> None:
    for code in ErrorCode:
        assert code.value == code.name
```

**3 条要点**

1. `ids=lambda c: type(c).__name__` —— 参数化用例的 id 用类名，失败时一眼看出是哪个异常。
2. `test_every_leaf_exception_is_in_the_mapping_table` 递归枚举 `AppError.__subclasses__()`，
   断言每个叶子类都在表里；并断言**中转基类**（除 `AppError` 外）不出现在表里（它们靠 MRO 兜底）。
3. 三个 200 用例**必须**独立于参数化表再断言一次。参数化表里若有人把测试里的 `200` 改成 `500`
   仍会"通过"（改的是测试本身）—— 独立断言让改动意图更显眼。

### 4.10 `tests/test_contract_guardrails.py`（追加）

```python
# ---------- 阶段四追加（SPEC 4.10）----------
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
    """§4：`except Exception: pass` / `...` 是吞异常，禁止。"""
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
```

**4 条要点**

1. 六条守护**全部沿用 AST 扫法**（`_python_sources()`），扫描面是 `src/` 而已。
   测试代码不受约束 —— 这正是我们要的：测试需要自造 `FastAPI(...)`、自触发 `HTTPException`。
2. `test_no_swallowed_exceptions` 是本节新增的最有价值一条：`except Exception: pass` 与
   `except Exception: ...` 都被拦。注意 `_handle_unexpected`（4.3）与 `/health` 的 try（4.5）
   **都有函数体**（构造信封 / 置降级 + 打点），不会被误伤 —— 这两处是本阶段的"合法捕获大类"。
3. `test_json_response_only_in_api_errors` 的允许清单**只有 `api/errors.py`**。不要把
   `api/schemas.py` 也算进去 —— 它连 starlette 都不 import，不该构造任何响应。
4. 这几条是"新文件里少写一行就变红"型守护，作用是**防止阶段六的作者**把 `HTTPException`
   直接写进 router（那正是硬规则 2 要防的事）。

### 4.11 `docs/decisions/0002-信封与异常映射.md`（新建）

沿用 `0001-模型名校验结论.md` 的 ADR 体例（背景 / 结论 / 理由 / 影响），记录五件事。
**该文件已随 B1 的批准建立**：决定 1（§3.2 增补）与决定 5（§3.1 示例修正）已随
2026-10-03 的批准落地；决定 2–4 记的是本 SPEC 的取舍，T9 施工时回填。

1. **§3.2 枚举的追加**（四条传输层码）：为何是"新增"而非"改语义"，为何需审批，
   审批通过前 404/405/429/500 都无合规码可用。
2. **兜底 `Exception` 处理器**：与 §3.2"捕获链条不要捕获大类"的张力，为何仍然要做
   （§3.1 的信封完备性压倒局部洁癖）、边界写在哪（不外泄、有打点、不吞）。
3. **三个 200 异常**：为何由处理器转 200 而非让服务层直接返回状态值（保住"唯一映射点"），
   以及 `ok=True` 的语义（正常业务结果）。
4. **`Decimal` 的序列化**：为何必须显式 `field_serializer`，`round(x, 8)` 的理由。
5. **§3.1 失败示例的修正**：为何改示例（补零值 `usage`）而不是改规则 —— 规则是对的，
   示例是笔误；反方向要付 bump `API_VERSION` 的代价。

---

## 5. 验收

### 命令

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src/pyassist
uv run pytest                                  # 退出码 0；全部不触网（本阶段无网络依赖）

# 本阶段专属
uv run pytest tests/test_envelope.py tests/test_error_mapping.py -v
uv run uvicorn pyassist.api.main:app --port 8000
#    另开一个终端：
curl -s localhost:8000/health | python -m json.tool     # data/app.db 已存在，可直接跑
curl -si localhost:8000/nope | head -n 1                # 期望 404，且 body 是信封
```

`mypy` 的严格范围是 `src/pyassist`（`pyproject.toml:108-113`），测试不参与类型检查。
`ruff` 的 `extend-exclude` 已排除 `*.md`，故本 SPEC 里的代码草案不会被格式化。

### 验收判据

| # | 判据 | 由什么保证 |
|---|---|---|
| 1 | 成功响应有完整信封（`ok/data/error/meta` 四键齐全） | `test_health_returns_compliant_envelope` |
| 2 | 失败响应同样有完整信封（含未知路由 404、未捕获 500） | `test_unknown_route_yields_envelope` / `test_unhandled_exception_yields_envelope` |
| 3 | `meta.usage` 永远在位且含 `cached_tokens` | `test_meta_always_carries_usage` + 快照 |
| 4 | `RetrievalEmpty` / `SandboxTimeout` / `SandboxRejected` **单独**断言 `== 200` 且 `ok=True` | `test_three_business_results_are_200_not_500` |
| 5 | §3.2 每个叶子异常都有映射，无遗漏 | `test_every_leaf_exception_is_in_the_mapping_table` |
| 6 | `error.code` 恒为枚举值且 UPPER_SNAKE | `test_error_code_values_are_upper_snake` + `ErrorDetail.code: ErrorCode` |
| 7 | 契约快照无 diff（字段集合冻结） | `test_contract_snapshot_fields_unchanged` |
| 8 | `cost_usd` 在 JSON 里是数字不是字符串 | `test_usage_serializes_whole_numbers` |
| 9 | `X-Request-ID` 在成功与失败响应上都回显 | 中间件（成功）+ `_json`（失败） |
| 10 | `src/` 内除 `api/errors.py` 外不出现 `HTTPException(` / `add_exception_handler(` | 守护 #14 / #15 |
| 11 | `AppError` 子类只在 `errors.py` 定义 | 守护 #16 |
| 12 | `JSONResponse(` 只在 `api/errors.py`；`FastAPI(` 只在 `api/main.py` | 守护 #17 / #18 |
| 13 | `except Exception: pass/...` 零命中 | 守护 #19 |
| 14 | 既有守护 #11 / #13 **仍然全绿**（`open_engine` / `ping` 未破坏单点所有权） | `tests/test_contract_guardrails.py` 全跑 |
| 15 | `API_VERSION` 常量存在且被 `FastAPI(version=...)` 使用 | `test_api_version_is_declared` + 代码审查 |
| 16 | 未创建任何 router / service / 图 | 人工核对 §2 清单；`git status` 无意外新文件 |

### 与 ROADMAP 验收的差异（七条，均需知悉）

1. ROADMAP 的产出文件列 7 项（含 2 个测试）；本阶段另加 `tests/snapshots/envelope.json`、
   `tests/conftest.py`、`docs/decisions/0002-*.md` —— 契约冻结要落到可执行快照；conftest 是测试基础设施；
   决策要留痕。
2. ROADMAP 未提"要改既有文件"；本阶段明确改 `db/session.py`、`db/repositories.py`、`llm/client.py`。
   前两者是为了**不破坏既有守护**（#11 / #13），后者是阶段二留下的必收尾口子。
3. ROADMAP 的验收命令只有 `pytest` 两个文件；本阶段追加 `uvicorn` + `curl /health` / `curl /nope`
   的人工步骤 —— 要把"未知路由也是信封"这件事在**真实 ASGI 栈**上看见（TestClient 不暴露全部
   中间件行为）。
4. ROADMAP 说"/health 暴露模型校验结果"但未定状态码；本阶段**定死：永远 200**，
   用 `data.status ∈ {ok, degraded}` 表达。依据 ADR 0001：模型名漂移是**告警**不是故障；
   非 200 会让编排器误判服务宕机。
5. ROADMAP 说"slowapi 限流"未给数值；本阶段用模块常量 `DEFAULT_RATE_LIMIT = "120/minute"`，
   且**必须可关**（`create_app(rate_limit_enabled=False)`）—— 数值是待定项 6；测试必须能关掉，
   否则自身撞 429。
6. ROADMAP 未提"§3.2 需增补枚举"；本阶段曾把它标为阻塞项 B1，**已于 2026-10-03 批准并落地**
   （现见下节）。
7. **ROADMAP 的阶段四原写"阻塞项：无"，本 SPEC 一度列出 B1。** 这不是推翻它，是细化：
   ROADMAP 写"产出文件 + 验收"时把信封与映射当成可以直接施工的事，而逐字对齐 §3.1/§3.2 后
   发现 404/405/429/500 无合规码可用。B1 是**契约层面**的缺口，不是阶段四实现的缺口；
   现已批准，ROADMAP 阶段四的"阻塞项"一行也同步改成了指向 §3.2 的说明。

### 阻塞项

**已批准 2026-10-03 —— 四个码为新增，不改既有语义。** 原阻塞项 B1 关闭。

`NOT_FOUND` / `METHOD_NOT_ALLOWED` / `RATE_LIMITED` / `INTERNAL_ERROR` 已补进
`AGENTS.md` §3.2 的枚举清单，与业务异常树同级；判据 6（"`error.code ∈ 枚举`"）因此可满足。
决定与理由见 `docs/decisions/0002-信封与异常映射.md` 的决定 1。

**本阶段现已无阻塞项。** 其余待定项（§7）都不阻塞动工。

---

## 6. 风险与缓解

| # | 风险 | 触发条件 | 缓解 |
|---|---|---|---|
| R1 | 兜底 500 的响应拿不到 `X-Request-ID` | 依赖中间件盖响应头 | `_json` 里**每个出口自己盖**；500 不经用户中间件（M3） |
| R2 | `RateLimitExceeded` 处理器写成 `async def` | 直觉写法 | 强制同步 `def`；slowapi 会静默丢弃 async 处理器（M6）。写成 async 时 429 的 body 会变成 `{"error": ...}` —— 需一条集成断言兜住 |
| R3 | 中间件顺序写反，限流的响应漏掉 `request_id` | 先加 request_id、后加 slowapi | 注释写死"后加的在外层"（M2）；人工审查 `create_app` 的三步顺序 |
| R4 | `Decimal` 泄漏成字符串进 JSON | 忘了 `mode="json"` 或忘了 serializer | 两层防护：serializer + `test_usage_serializes_whole_numbers` |
| R5 | 测试撞上自身限流、随机变红 | 未禁用限流 | `create_app(rate_limit_enabled=False)`；`conftest.client` 默认关 |
| R6 | `TestClient` 没当上下文管理器，`/health` 测到降级分支还"通过" | 直接 `TestClient(app)` | `conftest.client` 用 `with`；`get_engine` 在缺引擎时抛错而非静默降级 |
| R7 | 兜底处理器吞掉异常语义 | 误写成 `except Exception: pass` | 守护 #19 拦 `pass`/`...`；ADR 0002 记录边界 |
| R8 | 契约快照频繁飘红 | 动了字段又没更新快照 | 快照只比**字段名**；变更流程写进 `schemas.py` 文件头与 4.2 |
| R9 | §3.2 增补枚举未获批 | 阻塞项 B1 | §5 的阻塞项，合并前必须拍板 |
| R10 | 阶段六作者把 `HTTPException` 写进 router | 无约束 | 守护 #14 在 CI 拦下 |
| R11 | `/health` 在数据库不可用时返回 500 | 直觉写法 | 探活包在 try 里，降级为 `status="degraded"`，HTTP 恒 200 |
| R12 | `open_engine` / `ping` 破坏了既有守护 #11 / #13 | 命名或落点错误 | 命名进 `db/session.py`、SQL 进 `db/repositories.py`；既有守护继续跑即验证 |
| R13 | 新增依赖 | 有人为 `request_id` 引入 ULID 库 | 本阶段**零新增依赖**；`req_` + `uuid4().hex` 足够（待定项 5） |
| R14 | 从 `fastapi` 顶层 import `RequestValidationError` | 旧教程写法 | 只从 `fastapi.exceptions` import；mypy 会立刻报错 |

---

## 7. 待定项

每条给**推荐值**，不阻塞动工；但第 2 条（B1）需在**合并前**拍板。

### 1. §3.1 的 `usage` 矛盾（**已解决**）

失败示例的 `meta` 里原本没有 `usage`，规则却说永远在位。
→ **已于 2026-10-03 按规则改示例**：`AGENTS.md` §3.1 的失败示例补上零值 `usage`。
之所以选"改示例"而不是"改规则"：契约的**规则**是对的 —— `usage` 永远在位才能让前端无条件
读 `meta.usage.cached_tokens`，省一个 `?.` 分支；而示例只是笔误。改动是**文档修正**，
不是语义变更，故不涉及 `API_VERSION`。

### 2. §3.2 是否增补四个传输层码（**已解决**）

`NOT_FOUND` / `METHOD_NOT_ALLOWED` / `RATE_LIMITED` / `INTERNAL_ERROR`。
→ **已于 2026-10-03 批准增补**（新增，不改语义），并落地进 `AGENTS.md` §3.2。
原阻塞项 B1 关闭，理由见 ADR 0002 决定 1。本条目保留以记录决策已发生。

### 3. 兜底 `Exception` 处理器与 §3.2"不要捕获大类"的张力

→ **推荐：保留兜底**，因为 §3.1 要求任何失败都带完整信封，而 FastAPI 默认 500 是 `{"detail": ...}`。
边界写死在 4.3 要点 7。若要更严，可把兜底降级为"仅记录 + 复用 starlette 默认响应"——
但那会破坏信封完备性，不推荐。

### 4. 三个 200 异常：`ok=True` 信封 vs 服务层返回状态值

→ **推荐：异常 + 处理器转 200**（保住"唯一映射点"，服务层只 `raise`）。备选
（服务层返回 `ModelResult(status="empty")`）会把 HTTP 语义渗进服务层，不推荐。

### 5. `request_id` 是否上 ULID

契约示例形如 `req_01H...`。→ **推荐：`req_` + `uuid4().hex`，不引入依赖**。
若确需时间有序性（便于日志范围查询），再考虑 `ulid-py` —— 那是新增依赖，留到真正需要时。

### 6. 速率限制的数值与豁免

→ **推荐：默认 `120/minute`，`/health` 不豁免**（豁免需要一个 `key_func` 分支，得不偿失；
120/分钟对探活绰绰有余）。数值最终由运维定。

### 7. `lifespan` 是否本阶段就打开 checkpointer / store

→ **推荐：不打开**。本阶段没有任何图使用它们；过早打开会让 `/health` 依赖一个没人用的存储，
也会让"进程能否起来"与"数据库迁移是否跑过"耦合。阶段六接入图时再开。

### 8. `Dockerfile.api` 的归属

`docker-compose.yml:2` 的注释写"api 阶段四"，而 ROADMAP 把部署放在阶段八。
→ **推荐：维持 ROADMAP**，本阶段不写 Dockerfile；顺手把 `docker-compose.yml` 的注释改成
"api 阶段八"（一行注释的修正，非功能变更）。

### 9. 失败信封的示例是否回改 `AGENTS.md`（**已完成**）

→ **已于 2026-10-03 回改示例**（补上 `meta.usage` 零值），与待定项 1 是同一件事、
同一次改动。理由：示例与规则不一致本身就是文档缺陷，且规则的语义是对的。

### 10. `errors.ValidationError` 与 pydantic `ValidationError` 的同名混淆

→ **推荐：保持类名**（契约 §3.2 就叫 `ValidationError`）；在需要 pydantic 那个的场合用
`from pydantic import ValidationError as PydanticValidationError`，绝不裸 import。

### 11. 异常收口的边界

哪些既有 `RuntimeError` / `ValueError` 转 `ConfigError`。
→ **推荐：只转"服务级"条件** —— 缺 API key（`llm/client.py`）、缺价目表（`llm/tracing.py`）、
不支持的数据库后端（`agent/checkpointer.py` / `agent/store.py`）、无法解析的库路径
（`config.resolve_sqlite_path`）。**不转**纯断言型 `ValueError`（如 `is_off_peak` 收到 naive datetime）
—— 那是编程错误，应继续以 `ValueError` 在开发期炸掉。

### 12. 阶段六 SSE：`sse-starlette` 还是 `fastapi.sse`

二者已同时装在本环境且**同名不同 API**。→ 本阶段**只记录不决定**。等阶段六接入流式时，
做一次与阶段一"选依赖"同规格的比对再定。

---

## 8. 为后续阶段预留的接口

本阶段不打地基之外的桩。留下的缝是**已就位的代码 + 已生效的守护**，不是空函数：

| 缝 | 形态 | 谁在哪个阶段用 |
|---|---|---|
| `create_app()` 工厂 | 已实现（`api/main.py`） | 阶段六/七/八的测试都用它造 app，不各自 `FastAPI(...)` |
| `deps.get_session` | 已实现 | 阶段六的图节点、阶段七/八的 router 从它取 `AsyncSession` |
| `deps.get_engine` / `deps.build_meta` | 已实现 | 路由构造成功信封（`build_meta`） |
| `register_exception_handlers` | 已实现 | 阶段六/七/八新增异常只需在 `errors.py` 加类 + 在映射表加行，**不必碰处理器** |
| `Envelope[T]` / `Page[T]` / `envelope_success` | 已实现 | 阶段六/七/八所有端点返回 `Envelope[X]`；列表端点返回 `Envelope[Page[Item]]` |
| 守护 #14–#19 | 已在 CI | 阶段六作者若把 `HTTPException` 写进 router，CI 立刻红 |
| `open_engine` | 已实现（`db/session.py`） | 阶段六的 `lifespan` 若还要开 checkpointer / store，在**同一个 lifespan** 里加，仍不违反 #11 |
| `ping` | 已实现（`db/repositories.py`） | 阶段八的运维探针复用；新的探活 SQL 继续加在这个文件 |
| `HealthData` | 已实现（`api/schemas.py`） | 阶段六起往 `/health` 的 data 里加字段（**记得同步快照**） |
| ADR `docs/decisions/0002-*.md` | 已建 | 阶段六若决定 SSE 库，写 `0003-*.md` |

**阶段四结束时应当为真的三件事**（供阶段五开工前自检）：

1. `uv run pytest` 全绿，且 `tests/test_envelope.py` / `tests/test_error_mapping.py` 在内。
2. `git status` 里没有 `src/pyassist/api/routers/` 下的任何非 `__init__.py` 文件。
3. `grep -rn "HTTPException" src/` 的结果**只**出现在 `src/pyassist/api/errors.py`。
