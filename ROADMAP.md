# ROADMAP.md — Python AI 学习助手 · 分阶段施工路线图

本文件回答一个问题：**每个阶段做什么。**

- `AGENTS.md` 是**契约**（怎么建、建成什么样），本文件是**施工顺序**（先做什么、后做什么）。
- `ai-clever-bubble.md` 是**学习路线**（学什么），只读。三者冲突时以 `AGENTS.md` 为准。
- 本文件不重复契约原文，只引用条款号（§1、§2、§3.1……）并说明该条款在**哪个阶段**落地。

---

## 0. 三个坐标轴

项目里有三套编号，容易混淆，先对上：

| 施工阶段（本文件） | 对应学习阶段（`ai-clever-bubble.md`） | 交付版本（`AGENTS.md` §0） | 一句话 |
|---|---|---|---|
| 一 · 工程底座 | 一 · Python 速通 | — | 骨架 + 配置 + 日志，能 lint 能测 |
| 二 · LLM 基座 | 二 · Prompt Engineering | — | 唯一模型工厂 + `reasoning_content` 补丁 |
| 三 · 持久化与状态双源 | 六 · Agent 框架（Checkpointer） | — | 一个库、一个检查点构造点 |
| 四 · API 契约与错误体系 | 七 · 工程化（FastAPI） | — | 冻结信封 + 唯一异常映射 |
| 五 · RAG 子系统 | 四 · RAG | — | 可重跑索引 + 检索 + 引用 |
| 六 · v1 文档问答 + SSE | 四 + 七 | **v1** | 答案必带来源 |
| 七 · 沙箱 + v2 错误诊断 | 三 + 六 | **v2** | 报错 → 解释 → 修复 → 沙箱验证 |
| 八 · v3 批改 + v4 学习路径 + UI + 部署 | 五 + 八 | **v3 / v4** | 全链路 + 公网可访问 |

### 为什么重排，而不是照抄学习路线

学习路线把 RAG 排在第 4 周、FastAPI 排在第 8 周。照抄会卡死：**v1 的核心能力是"RAG + 带引用的问答"，交付物是一个 HTTP 服务**，而 v1 的图需要三样东西先就位 —— 模型工厂（阶段二）、检查点（阶段三）、响应信封与错误映射（阶段四）。所以 RAG（学习阶段四）在施工上落到**阶段五**，API 外壳（学习阶段七）提前到**阶段四**。

### 施工顺序的三条硬规则

1. **补丁（阶段二）先于任何图。** `llm/client.py` 是任何模块取模型的唯一入口；`ChatDeepSeekPatched` 未就位就建图，全部要返工。补丁约 20 行，晚了要重写每张图的工具循环。
2. **信封与错误映射（阶段四）先于任何 router。** router 一旦先存在，`raise HTTPException` 就会到处滋生，错误格式随即分叉（§2、§4）。
3. **检查点（阶段三）先于任何图持久化。** 两份互相矛盾的对话历史一旦形成就很难拆（§2）。

---

## 已核实的环境

本机实测结果，施工前需知：

| 工具 | 状态 | 影响 |
|---|---|---|
| Python | ✅ 3.12.7 | 满足 §1 的 3.12+ 要求 |
| uv | ✅ 0.11.16 | 满足 §1 包管理要求 |
| git | ✅ 2.54.0 | — |
| **Docker** | ✅ 29.8.1 + Compose v5.5.1（插件形态）+ WSL2 Ubuntu（2026-10-03 装） | 阶段七沙箱可落地。**注意 Docker Hub 直连超时，须配 `registry-mirrors`** |
| `DEEPSEEK_API_KEY` | ✅ 已设置 | 真实模型调用与启动校验可落地 |

---

## 阶段一 · 工程底座

> 对应学习阶段一。不交付版本，交付"能开工的地基"。

### 目标

一个可运行、lint 干净的空骨架。目录结构与契约 §2 的目标布局一致，但**只有拥有者文件**（owner files），不建任何可能变成平行实现的模块。

### 产出文件

| 文件 | 说明 |
|---|---|
| `pyproject.toml` | **唯一依赖来源**（§1）。含 `[tool.ruff]`、`[tool.mypy]`（`strict = true`）、`[tool.pytest.ini_options]`（`asyncio_mode = "auto"`）、src 布局 |
| `src/pyassist/__init__.py` + 各子包 `__init__.py` | `llm/ rag/ agent/ agent/graphs/ agent/tools/ sandbox/ db/ services/ api/ api/routers/ evals/` |
| `src/pyassist/config.py` | ★ **唯一配置入口**：模型 ID 常量、`PRICE_TABLE`、`DATABASE_URL`、沙箱上限 |
| `src/pyassist/logging.py` | ★ **唯一 structlog 初始化**，JSON 输出，绑定 `request_id`/`session_id`/`user_id` |
| `.env.example` | 只列变量名，不含真实值（§4） |
| `.gitignore` | `data/**`、`.env`、`*.db`、`.venv/`、`__pycache__/` |
| `docker-compose.yml` | 骨架：`api` / `web` / `sandbox` 三件套，sandbox 用 `profiles: [sandbox]` 先挂起 |
| `data/corpus/.gitkeep`、`data/chroma/.gitkeep` | 只建目录，运行产物不提交（§4） |
| `docs/decisions/`、`experiments/`、`tests/` | ADR 目录、一次性实验目录、测试目录 |
| `tests/test_smoke.py` | 一条 import 断言，保证 `pytest` 退出码为 0 |
| `tests/test_contract_guardrails.py` | 契约守护：断言 `src/` 内不出现已停用模型名 |

### 关键契约

- **§1**：禁止提交 `requirements.txt`（单一来源）；Python 必须 3.12+（依赖 `tomllib` 与新版 typing 语法）。
- **§2 单点所有权**：**不得创建** `utils.py` / `helpers.py` / `common.py` / `llm.py` 之流的转口模块，也不得建第二个 settings 模块。测试里也不许有直接构造 `ChatDeepSeek` 的 helper —— 那会变成事实上的第二个所有者。第一行代码起就要让每个能力只有一个家。
- **§4**：`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 停用且无宽限期。这两个字符串出现在代码、`.env`、CI 变量、docker-compose 里都会让请求直接 400，**包括注释和测试**。

### 验收方式

```bash
uv sync
uv run python -c "import pyassist"
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/pyassist
uv run pytest          # 退出码 0（不是"收集到 0 个用例"的 5）
```

### 阻塞项

无。阶段一可在当前环境下完整完成。

### 为什么排在这

所有后续阶段的文件都落在这套目录结构里。契约 §2 的 22 项单点所有权必须从第一天就在路径上体现，否则"顺手加个 utils.py"会一路累积成平行实现。

---

## 阶段二 · LLM 基座与 `reasoning_content` 修复

> 对应学习阶段二（Prompt Engineering）。**本项目最高风险技术点在这里落地。**

### 目标

唯一模型实例工厂就位，并且把契约 §1 描述的 `reasoning_content` 丢失缺陷修掉 —— 不修，v2/v3 的 Agent 循环在第二轮必崩。

### 为什么它必须排在任何图之前

这不是"顺手提前"，是硬依赖：

1. `llm/client.py` 是**任何**模块取模型的唯一入口。先建图，等于让图去实例化错误的类，全部返工。
2. §1.4 要求的那条回归测试是防 400 的**唯一保险**。补丁和工具循环同时调试，正是 §4 警告的"最容易踩且最难排查"的处境。
3. 失败模式在阶段六就会到达，不是只在阶段七 —— `search_docs` / `read_doc` 本身就是 LLM 可见的工具（§2），一旦它们被工具循环驱动，就会出现 `tool_calls`。
4. 成本不对称：补丁约 20 行；发现晚了要重写每张图的工具循环。

### 产出文件

| 文件 | 说明 |
|---|---|
| `src/pyassist/llm/deepseek_chat.py` | ★ `ChatDeepSeekPatched(ChatDeepSeek)`：重写 `_get_request_payload`，把 `additional_kwargs["reasoning_content"]` 写回出站 assistant 消息；**消息带 `tool_calls` 而字段缺失时回填 `""`**（空串也必须传，不能省略） |
| `src/pyassist/llm/client.py` | ★ **唯一模型实例工厂**，只产出 `ChatDeepSeekPatched`。启动时调一次 `client.models.list()`，比对 `config.py` 里的期望模型名，不一致打 `error` 级日志并在 `/health` 暴露 |
| `src/pyassist/llm/tracing.py` | ★ **唯一 Token/成本记账**。命中缓存与未命中的 token **分别记录**，按 `config.PRICE_TABLE` 分别计价后求和，再按 `is_off_peak` 套半价 |
| `src/pyassist/llm/prompts/qa.jinja2` + 加载器 | 经 `ChatPromptTemplate` 加载；渲染顺序固定 `tools` → `system` → `messages` |
| `tests/test_deepseek_reasoning.py` | ★ 强制的两轮工具调用回归测试 |
| `tests/test_prompt_stability.py` | 缓存前缀稳定性守护 |
| `tests/test_tracing.py` | 缓存命中/未命中分开计价 |

### 关键契约

- **§1 思考模式**：由 `extra_body={"thinking": {"type": "enabled"}}` 控制，**不是靠换模型**。V4 默认开启思考；要旧的非思考行为必须显式关闭。
- **§1 参数陷阱**：思考模式下 `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` **全部不生效** —— 设了不报错但被忽略。**不要靠调 temperature 解决输出问题，那是在自欺。**
- **§1 上下文缓存**：命中与未命中的输入价差可达 **50 倍**。这要求 prompt 前缀**逐字节稳定**：`llm/prompts/*.jinja2` 里禁止出现时间戳、随机 id、`datetime.now()`，禁止未排序的 dict 迭代。易变的用户输入永远放最后。
- **§1 上游状态**：社区 PR（#37461 出站镜像、#35093 在 `tool_calls` 场景回填 `""`）**不能假定已合进你装到的版本**。`uv sync` 后第一件事就是打开装到的 `langchain-deepseek` 看一眼 `_get_request_payload` / `_convert_message_to_dict`。
- **§4**：禁止直接 `ChatDeepSeek(...)` / `ChatOpenAI(...)`；模型名只允许写在 `config.py` 一处。
- **§1 兜底**：若补丁阻塞进度，可临时关闭思考模式，但**必须**在 `docs/decisions/` 写下决定与恢复计划。代价是诊断质量下降。

### 验收方式

`tests/test_deepseek_reasoning.py` 三条，全部离线（用 `respx` mock，不碰网络）：

1. **payload 级**：构造带 `tool_calls` 且 `additional_kwargs={"reasoning_content": "..."}` 的出站 `AIMessage`，调 `_get_request_payload`，断言序列化后的 assistant 消息里 `reasoning_content` 存在。
2. **回填级**：构造带 `tool_calls` 但**没有** `reasoning_content` 的 `AIMessage`，断言 payload 里该字段为 `""` —— **存在，不是省略**。
3. **连续两轮回归（强制）**：模拟第一轮 assistant 发 `tool_calls` → `ToolMessage` → 第二轮请求，断言第二轮 payload 含 `reasoning_content`。这就是 §5.1 给所有涉及工具调用的功能设的那道门。

另外：

```bash
uv run pytest tests/test_deepseek_reasoning.py tests/test_prompt_stability.py tests/test_tracing.py
```

`test_prompt_stability.py` 须断言：同一模板同样输入渲染两次，输出**逐字节一致**；且模板中不出现 `datetime.now` / `uuid` / 随机数 token。

### 阻塞项

`DEEPSEEK_API_KEY` 未设置 → 启动时的模型名校验只能用 `respx` mock 验证；真机校验需人工跑一次 `uv run python -m pyassist.llm.client --check-models`。

### 为什么排在这

见上文「为什么它必须排在任何图之前」。它是阶段六/七/八的硬前置。

---

## 阶段三 · 持久化与状态双源

> 对应学习阶段六（LangGraph Checkpointer）。

### 目标

对话历史只有一个家。LangGraph 的图执行状态与 SQLAlchemy 的业务记录划清边界 —— 这是引入 LangGraph 后最易分叉的地方（§2）。

### 产出文件

| 文件 | 说明 |
|---|---|
| `src/pyassist/db/session.py` | 引擎与 Session；`DATABASE_URL` 来自 `config.py`；**不得出现方言分支** |
| `src/pyassist/db/models.py` | `users` / `sessions` / `exercise_attempts` / `mistakes` / `token_ledger`。**没有 `messages` 表** |
| `src/pyassist/db/repositories.py` | ★ **唯一 SQL 出口** |
| `src/pyassist/agent/checkpointer.py` | ★ **唯一检查点构造点**。开发 `SqliteSaver` → `data/app.db`；生产 `PostgresSaver` → 同一个 PG 实例。**禁止 `MemorySaver`** |
| `src/pyassist/agent/store.py` | ★ **唯一长期记忆 Store 构造点** |
| `alembic.ini` + `migrations/versions/0001_init.py` | 建业务表，**并只跑一次** `checkpointer.setup()` / `store.setup()` |
| `tests/test_state_ownership.py` | 边界守护 |
| `tests/test_idempotency.py` | 节点幂等守护 |

### 关键契约

- **§2 两份状态的分工**：
  - 图执行状态（消息轨迹、工具中间结果、节点产物）→ Checkpointer，**这是"对话上下文"的唯一真相**。
  - 业务记录（用户、练习提交、评分、错题、Token 账本）→ SQLAlchemy，**不重复存对话消息**。
- **§2 同一个数据库**：`agent/checkpointer.py` 必须指向与 `db/session.py` 相同的库。**不允许第二个状态库，禁止 `MemorySaver`**（重启即丢）。
- **§2 对话历史只读 checkpoint**：回放用 `app.get_state_history(config)`，不要在业务表里再存一份 `messages`。
- **§2 恢复时不传状态**：checkpointer 是 state authority —— `invoke()` 传入的 state 会被丢弃，续跑只需 `config={"configurable": {"thread_id": <会话 id>}}`。
- **§2 节点必须幂等**：恢复会重跑被中断的节点，副作用（写业务表、调沙箱）要带去重键。
- **§2 检查点会膨胀**：带大工具结果的检查点可达 10MB 级。截断逻辑落在 `sandbox_tools.py`（阶段七），此处先立下幂等模式。
- **§2 记忆归属**：长期记忆与用户画像走 LangGraph `Store`（`agent/store.py`），**不走 `db/repositories.py`，也不另开第二个向量库**。`services/memory.py` 只负责"从对话里抽取偏好"这类加工，持久化一律经 `agent/store.py`。
- **§2 setup 时机**：`checkpointer.setup()` / `store.setup()` 只需在建库时跑一次，写进迁移脚本，**不要在请求路径里调**。

### 验收方式

```bash
uv run alembic upgrade head
uv run pytest tests/test_state_ownership.py tests/test_idempotency.py
```

- `test_state_ownership.py`：断言检查点连接目标 == `db/session.py` 目标 == `data/app.db`（**程序化断言，不是靠约定**）；断言从 `src/pyassist` 里 import 不到 `MemorySaver`；断言 `db/models.py` 没有 message 形状的表。
- `test_idempotency.py`：同一个 `thread_id` 下重跑一个节点两次，断言业务表行数不变（去重键生效）。
- 迁移后检查表结构：检查点表与业务表在**同一个文件**里。

### 阻塞项

无。

### 为什么排在这

必须在第一张图落库之前把边界钉死（硬规则 3）。定晚了，两份互相矛盾的对话历史已经形成，拆分成本极高。

---

## 阶段四 · API 契约与错误体系

> 对应学习阶段七（FastAPI 工程化）。

### 目标

冻结的响应外壳 + 唯一的异常映射点。这两样必须先于任何 router 存在，否则 `raise HTTPException` 会到处滋生，错误格式随即分叉。

### 产出文件

| 文件 | 说明 |
|---|---|
| `src/pyassist/errors.py` | 异常层级，严格按 §3.2 的树 |
| `src/pyassist/api/schemas.py` | ★ **契约冻结**（§4）。声明 `API_VERSION` |
| `src/pyassist/api/errors.py` | ★ **唯一**把异常翻译成 HTTP 的地方 |
| `src/pyassist/api/main.py` | app 工厂 + `request_id` 中间件 + slowapi 限流 + `/health` |
| `src/pyassist/api/deps.py` | 依赖注入缝。**阶段七的沙箱 fake 从这里注入** |
| `tests/test_envelope.py` | 信封守护 |
| `tests/test_error_mapping.py` | 错误码 → HTTP 映射守护 |

### 关键契约

**§3.1 响应信封**。所有非流式响应统一：

```json
{
  "ok": true,
  "data": { },
  "error": null,
  "meta": {
    "request_id": "req_01H...",
    "latency_ms": 842,
    "usage": {
      "prompt_tokens": 1204,
      "completion_tokens": 388,
      "cached_tokens": 1024,
      "cost_usd": 0.0004,
      "is_off_peak": true
    }
  }
}
```

规则：

- `meta` 三个字段**永远存在**，即使出错。缺 `usage` 时**填零值而非省略**。
- `usage.cached_tokens` **必须单独统计**。命中与未命中的输入价差 50 倍，合并成一个 `prompt_tokens` 会让成本报表严重偏低。`cost_usd` 由 `llm/tracing.py` 分别计价后求和，并按 `is_off_peak` 套半价。
- `error.code` 只能取 §3.2 的枚举，**不得自造字符串**。
- `data` 永远是对象或 `null`，**不要裸数组**（列表放 `data.items` + `data.total`）。
- 分页统一 `?limit=&cursor=`，响应带 `data.next_cursor`。

**§3.2 错误码 → HTTP**。三个映射到 **200** 的项要特别注意，它们是**正常结果不是故障**：

| 异常 | HTTP | 备注 |
|---|---|---|
| `ConfigError` | 500 | |
| `ValidationError` | 422 | |
| `RetrievalEmpty` | **200** | 回答"文档中没有"是正常结果 |
| `CorpusMissing` | 503 | |
| `LLMTimeout` | 504 | 可重试 |
| `LLMRateLimited` | 429 | 可重试，透传 retry-after |
| `LLMInsufficientBalance` | 503 | DeepSeek 402，**不可重试**，需人工充值 |
| `LLMModelNotFound` | 500 | 模型名已停用/拼错 |
| `LLMBadResponse` | 502 | 流中断、tool_calls JSON 解析失败 |
| `SandboxTimeout` | **200** | 超时是实验结果，用 `data.status="timeout"` 表达 |
| `SandboxRejected` | **200** | 安全策略拦截，同上 |
| `StoreError` | 500 | |

处理原则：

- **捕获链条，不要捕获大类**。按 `NotFoundError` → `RateLimitError` → `APIStatusError` → `APIConnectionError` 从具体到宽泛的顺序写。**禁止一个 `except Exception` 吞掉全部**（§4 也禁止 `except Exception: pass`）。
- 重试用 SDK 自带的 `max_retries`（默认 2），**不要手写 `for i in range(3)`**。
- 每次异常必须 `structlog` 打点并带 `request_id`，但**不记 API Key、不记完整用户代码**（只记哈希与长度）。

**§4 禁区**：`api/schemas.py` 契约冻结 —— 改字段 = 破坏性变更，必须 bump `API_VERSION` 并同步更新 `frontend/`。`api/errors.py` 是唯一异常映射点，**router 内禁止 `raise HTTPException(500)`**。

### 验收方式

```bash
uv run pytest tests/test_envelope.py tests/test_error_mapping.py
```

- 信封：成功路径与失败路径都返回完整的 `ok/data/error/meta`；`meta.usage` 存在且含 `cached_tokens`；出错时 `meta` 仍在。
- 错误映射：§3.2 每个异常映射到表里声明的状态码，其中 `RetrievalEmpty` / `SandboxTimeout` / `SandboxRejected` 单独断言 == **200**。
- 契约快照守护：字段集合与提交的快照一致；`API_VERSION` 常量存在。
- `curl /health` 返回合规信封。

### 阻塞项

- ✅ **已关闭（2026-10-03）**：§3.2 原缺 404 / 405 / 429 / 兜底 500 的错误码，
  而 §3.1 禁止自造字符串 —— "信封完备"与"`error.code` 只取枚举"这两条无法同时满足。
  已批准在 §3.2 增补 `NOT_FOUND` / `METHOD_NOT_ALLOWED` / `RATE_LIMITED` / `INTERNAL_ERROR`
  四个传输层码（属新增，不改既有语义）。见 `AGENTS.md` §3.2 与
  `docs/decisions/0002-信封与异常映射.md` 决定 1。
- 其余无。

### 为什么排在这

硬规则 2。router 一旦先存在，`HTTPException` 就会扩散，错误格式分叉后要全局回改。

---

## 阶段五 · RAG 子系统

> 对应学习阶段四（RAG 全流程）。这是 v1 的核心引擎。

### 目标

可重跑、幂等的语料索引 + 唯一检索入口 + 唯一引用构造。原则：**宁可回答"文档里没有"，也不要编造 API**（§0）。

### 产出文件

| 文件 | 说明 |
|---|---|
| `src/pyassist/rag/embedder.py` | ★ **唯一向量化出口** |
| `src/pyassist/rag/store.py` | 向量库连接。Chroma → Qdrant 的**唯一切换点** |
| `src/pyassist/rag/ingest.py` | ★ **唯一写向量库的地方**，幂等可重跑 |
| `src/pyassist/rag/retriever.py` | ★ **唯一检索入口** |
| `src/pyassist/rag/citations.py` | ★ **唯一引用构造与校验** |
| `src/pyassist/evals/datasets/qa.jsonl` | **只能新增，禁止修改已有用例**（§4） |
| `tests/test_ingest_idempotent.py` | 幂等守护 |
| `tests/test_retriever.py`、`tests/test_citations.py` | 检索与引用守护 |

### 关键契约

- **§1 向量化选型**：DeepSeek **不提供 embedding 接口**。必须另选（本地 `sentence-transformers` 或第三方 API），模型 id 落在 `config.py`。详见「待定项」。
- **§5.2 幂等**：ingest 可重跑且幂等。chunk id 由**内容哈希 + 来源 URL** 派生，用 upsert 而非盲目 insert —— 重跑后 chunk id 集合与数量都应一致。
- **§4 禁区**：`data/corpus/**` 禁止手工编辑、禁止删除（语料是一切回答的事实来源）；`data/chroma/**` 禁止手改。**重建 = 删目录 + 重跑 ingest，不允许"局部修补"**。
- **§4 禁区**：`evals/datasets/**` 只能**新增**，禁止修改已有用例。评估集是回归基线，**为了提分改测试集是本项目最严重的作弊**。
- **§5.2 引用**：引用由 `rag/citations.py` 生成，**不是 LLM 自由发挥的文本**。
- **§3.5 打点**：`retrieval.done` 带 top-k 文档 id 与分数。
- **§1 运行时禁令**：`services/*` 不得在运行时往向量库 upsert —— ingestion 是独立的显式步骤。

### 验收方式

```bash
uv run pytest tests/test_ingest_idempotent.py tests/test_retriever.py tests/test_citations.py
uv run python -m pyassist.rag.ingest --corpus data/corpus     # 手动，需向量模型就位
```

- 幂等测试：同一 fixture 语料跑两次，断言 chunk id 集合与集合数量**完全一致**。（CI 里把 embedding 用确定性 fake 桩掉，保持离线。）
- 引用测试：实质回答路径产出 ≥1 个可点击来源；检索为空时按 `RetrievalEmpty` 返回"文档中没有"语义。

### 阻塞项

**向量模型选型未定** —— 阶段五的硬前置。见「待定项」第 1 条。

**语料来源未定** —— 见「待定项」第 3 条。

### 为什么排在这

v1 的图（阶段六）要调用 `retriever` 与 `citations`。引擎先于壳。

---

## 阶段六 · v1 文档问答 + SSE

> 对应学习阶段四 + 七。**交付 v1。**

### 目标

v1 上线：检索 → 引用 → 生成。**100% 的实质性回答至少带 1 个可点击来源**；检索不到时必须明说"文档中没有"，**不得凭记忆作答**（§5.2）。

### 产出文件

| 文件 | 说明 |
|---|---|
| `src/pyassist/agent/state.py` | ★ **唯一图状态 schema**（`MessagesState` 扩展）。禁止每张图各定义一个互不兼容的 State |
| `src/pyassist/agent/registry.py` | ★ **唯一工具定义点**。OpenAI 风格 `tools` 数组在此生成 |
| `src/pyassist/agent/tools/docs.py` | `search_docs` / `read_doc`，`retriever` 的薄包装 |
| `src/pyassist/agent/graphs/qa.py` | v1 图：检索 → 引用 → 生成，**手拼节点** |
| `src/pyassist/services/qa.py` | v1 业务流程 |
| `src/pyassist/api/routers/chat.py` | `POST /chat`（SSE）。★ **唯一** LangGraph 事件 → SSE 映射点 |
| `src/pyassist/api/routers/knowledge.py` | `POST /knowledge` |
| `src/pyassist/evals/run_rag_eval.py` | Recall@5、引用准确率 |
| `tests/test_graph_qa.py`、`tests/test_sse.py` | 图与流式守护 |

### 关键契约

- **§1 框架边界**：用 LangGraph 的画布，**不用** LangChain 的成品 Agent。`create_react_agent` / `create_agent` 会隐藏工具循环细节，而 v2/v3 的核心恰恰是"工具循环 + 沙箱验证"，每个节点必须能在图里显式控制。
- **§2 流式铁律**：**必须用 `await graph.astream(...)` / `astream_events(...)`**。在 async 路由里调用同步 `graph.stream()` 会阻塞事件循环 —— **一个慢请求拖垮所有连接。这条没有例外。**
- **§2 过滤后再转发**：一次 60 秒的 Agent 运行可能产生 3000+ 事件，只挑前端真正要的（token、工具开始/结束、引用、用量）映射成 §3.1 的 SSE 事件类型。
- **§2 响应头**：必须带 `X-Accel-Buffering: no`、`Cache-Control: no-cache`。**缺了这两行，本地跑得通，上 Nginx / Cloud Run 就被缓冲成"一次性吐出"。**
- **§2 心跳**：每 ~15 秒发一次注释 `: heartbeat\n\n`，撑过代理的空闲超时。
- **§2/§4 前端边界**：前端不得 `import src/pyassist`，只能通过 HTTP 说话；前端也不得直接消费 LangGraph 原始事件。
- **§2 工具命名**：LLM 看到的 `search_docs` 必须对应 `agent/tools/docs.py` 里的 `search_docs`。**不允许 `search_docs` → `do_search` 这种映射层。**
- **§3.4**：结构化输出用 `model.with_structured_output(...)` 或 `response_format`（JSON 模式），**不要自己 `json.loads` 后正则清洗**。
- **§1 长输出**：用 `graph.astream()` 避免 HTTP 超时。

### 验收方式

```bash
uv run pytest tests/test_graph_qa.py tests/test_sse.py
uv run python -m pyassist.evals.run_rag_eval
```

- `test_graph_qa.py`（mock LLM，用 `respx`）：检索为空走 `RetrievalEmpty` 且 HTTP 200、**不编造答案**；实质路径 ≥1 个引用。
- `test_sse.py`：断言两个响应头在位；慢图上能收到心跳。
- 评估门（§5.2）：**Recall@5 ≥ 0.80**、**引用准确率 ≥ 0.90**（在 `evals/datasets/qa.jsonl` 上）。把分数记进 README。

### 阻塞项

依赖阶段一 ~ 五全部就位。

### 为什么排在这

这是第一张图，也是 `ChatDeepSeekPatched` 第一次经真实工具循环承重 —— 阶段二的回归测试必须已经是绿的。

---

## 阶段七 · 沙箱 + v2 错误诊断

> 对应学习阶段三（Tool Use）+ 六（LangGraph）。**交付 v2。**

### 目标

v2 上线：用户贴报错 → 检索文档 → 解释 → 修复 → **沙箱验证通过**（失败回环）。"看起来对"不算，必须跑过（§5.3）。

### 产出文件

| 文件 | 说明 |
|---|---|
| `src/pyassist/sandbox/runner.py` | ★ **唯一执行用户代码的地方**。内含超长输出截断（保留头尾各 N 行） |
| `src/pyassist/sandbox/Dockerfile.sandbox` | 沙箱镜像，**安全边界。改动需人工 review**（§4） |
| `src/pyassist/agent/tools/sandbox_tools.py` | `run_code` / `run_tests` |
| `src/pyassist/agent/graphs/diagnose.py` | v2 图：检索 → 解释 → 修复 → 沙箱验证 →（失败回环） |
| `src/pyassist/services/diagnose.py` | v2 业务流程 |
| `src/pyassist/api/routers/diagnose.py` | `POST /diagnose` |
| `src/pyassist/llm/prompts/diagnose.jinja2` | 诊断 prompt |
| `src/pyassist/evals/datasets/errors.jsonl` | 20 个真实 Python 报错（**只增不改**） |
| `tests/test_sandbox_contract.py`、`tests/fakes/sandbox.py` | 离线接口测试与测试替身 |

### 关键契约

- **§4 禁区**：`src/` 内**禁止 `exec()` / `eval()` 执行用户代码**（只能经 `sandbox/runner.py`）；**禁止在没有沙箱隔离的情况下跑任何来自用户或 LLM 的代码片段**。
- **§1 部署**：沙箱容器**必须独立于 api 容器**。
- **§5.3 安全约束（必须实测验证，不是只配置）**：无网络、只读根文件系统、CPU/内存/时间上限、非 root。
- **§3.2 关键语义**：**用户代码执行失败不是服务错误。** 报错、超时、被拦截都要以**正常响应**返回，前端展示为实验结果（`data.status`）。**只有沙箱本身挂了才是 `SandboxError` 的服务端故障。**
- **§3.4**：沙箱执行是阻塞的，必须 `await asyncio.to_thread(...)` 或进程池，**不得阻塞事件循环**。
- **§2 检查点膨胀**：`sandbox_tools.py` **必须截断超长输出**（保留头尾各 N 行），否则带大工具结果的检查点可达 10MB 级。
- **§1 模型路由**：难诊断、代码修复用 `deepseek-v4-pro`，但模型名**只允许写在 `config.py` 一处**。
- **本阶段的真正考验**：诊断图会出现「模型 → tool_calls → ToolMessage → 再调模型」。**第二轮请求若丢掉 `reasoning_content`，DeepSeek 直接返回 400** —— 这正是阶段二补丁承重的地方。

### 验收方式

```bash
uv run pytest tests/test_graph_diagnose.py tests/test_sandbox_contract.py
uv run pytest -m docker      # 需真实 Docker
```

- `test_graph_diagnose.py`（mock LLM + fake 沙箱）：用户代码报错/超时返回 **HTTP 200** + `data.status`，**不产生 5xx**。
- 错误类型识别准确率 **≥ 85%**（在 `errors.jsonl` 上）。
- 修复后代码在沙箱内实际执行通过率 **≥ 70%**（**需真实 Docker**）。
- 恶意用例全部被拦截且**服务不崩**：死循环、`os.system("rm -rf /")`、读 `/etc/passwd`、fork 炸弹。
- grep 守护：`src/` 内不出现 `exec(` / `eval(` / `subprocess`。

### 阻塞项

**本机未装 Docker，WSL 也未装。** 没有真实隔离，§5.3 的恶意用例拦截与 §5.3 的修复通过率**无法验收**。

**不接受用 `subprocess` 降级顶替** —— 它满足不了隔离要求，且 §4 明令禁止。

可行的推进方式：先定 `runner.py` 的接口（`run_code` / `run_tests`，返回结构化 `SandboxResult`），测试期用**放在 `tests/fakes/` 里**（绝不放 `src/`）的 fake 实现顶替，经 `api/deps.py` 注入 —— 这样 v2 的图逻辑与测试可以离线推进，真实 Docker 实现与安全验收随后补上。fake **绝不能执行用户代码**。

见「待定项」第 2 条。

### 为什么排在这

需要阶段六的图骨架与工具循环模式先跑通；沙箱工具是 `registry.py` 里的新增工具，依赖阶段六建立的唯一定义点。

---

## 阶段八 · v3 批改 + v4 学习路径 + Web UI + 部署

> 对应学习阶段五（Memory）+ 八（全栈实战）。**交付 v3 与 v4。**

### 目标

v3、v4 上线；Streamlit 界面跑通完整链路（提问 → 引用 → 贴报错 → 沙箱验证 → 做题 → 批改 → 推荐）；公网可访问。

### 产出文件

| 文件 | 说明 |
|---|---|
| `src/pyassist/agent/graphs/exercise.py` | v3 图：出题 → 自校验 → 跑测试 → 批改 |
| `src/pyassist/agent/tools/grading.py` | `generate_exercise` / `grade_code` |
| `src/pyassist/llm/prompts/exercise.jinja2`、`grade.jinja2` | 出题与批改 prompt |
| `src/pyassist/agent/graphs/progress.py` | v4 图：读错题 → 推荐 |
| `src/pyassist/agent/tools/progress.py` | `recommend_next` |
| `src/pyassist/services/memory.py` | ★ 阶段五记忆的加工逻辑（短期/长期/摘要），**持久化一律经 `agent/store.py`** |
| `src/pyassist/api/routers/exercises.py` | `POST /exercises`、`POST /exercises/{id}/submit` |
| `src/pyassist/api/routers/history.py` | `GET /history` |
| `src/pyassist/api/routers/stats.py` | `GET /stats` |
| `frontend/streamlit_app.py` | **只调 HTTP API**，不得 `import pyassist` |
| `frontend_telegram/bot.py` | 可选适配器（v4 之后） |

### 关键契约

**v3（§5.4）**：

- 覆盖四个主题：**列表、字典、装饰器、异步**。
- 生成的每道题**自带测试用例，且参考答案能让测试全绿** —— **100% 自洽，CI 里自动校验**。
- 用户代码在沙箱内跑测试，输出结构化结果（通过/失败用例、stdout、stderr、耗时）。
- 评分含**可解释反馈**：指出错在哪、为什么错、指向哪份文档。**不得只给分数。**
- 评分与人工评分一致率 ≥ 80%（抽 30 份人工标注）。
- 练习记录与错题落库，`GET /history` 可查。

**v4（§5.5）**：

- `recommend_next` 输入错题记录，输出下一步主题 + 理由 + 对应文档链接。
- **推荐不是空话：必须引用该用户实际做错的题目 id。**
- Streamlit 跑通完整链路。
- 流式输出可见（SSE token 逐字上屏），不是转圈等 30 秒。
- **页面刷新/重新打开后，历史与进度仍在（落库，不靠 `session_state`）。**
- Token 用量与成本在 `/stats` 可见，**单日超限有熔断**。

**记忆归属（§2）**：长期记忆与用户画像走 LangGraph `Store`（`agent/store.py`），**不走 `db/repositories.py`，也不另开第二个向量库**。`services/memory.py` 只管加工。

**前端边界（§2/§4）**：前端不得 `import pyassist`，只能通过 HTTP 说话 —— 否则 API 契约形同虚设，部署时也拆不开。

**部署（§5.6）**：`docker compose up` 一键起三件套（`api` / `web` / `sandbox`）；README 含架构图、环境变量清单、一键启动命令、`evals/` 跑分方法；3 分钟演示录像或 GIF。

### 验收方式

```bash
uv run pytest
uv run python -m pyassist.evals.run_v3_eval     # 评分一致率
docker compose up                                # 干净环境
```

- 自洽性 **100%**（CI 自动校验：参考答案过自己的测试）。
- 评分与人工一致率 **≥ 80%**。
- `GET /history` 返回落库记录。
- UI 全链路走通；刷新后历史与进度仍在。
- `/stats` 成本可见，熔断生效。
- 干净环境 `docker compose up` 可起；README 步骤照做可复现。

### 阻塞项

依赖阶段七的真实 Docker（v3 的沙箱跑测试同理）。

### 收尾

按 §1 要求用 `uv pip list` 把实际版本回填到 `AGENTS.md` 末尾的"实际锁定版本"一段。注意：回填是契约预留的动作，但改动契约本身应先说明（§6）。

---

## 附录 A · 跨阶段 DoD 门（§5.1）

每条通用完成定义从哪个阶段起强制：

| DoD 门 | 从哪个阶段起强制 |
|---|---|
| `mypy --strict` / `ruff check` / `ruff format` 无 diff | 阶段一 |
| 测试不依赖真实 LLM 或网络（用 `respx` / mock） | 阶段二 |
| `tests/test_deepseek_reasoning.py` 通过（涉及工具调用的功能） | 阶段二建立，阶段六/七/八复跑 |
| 关键节点有 `structlog` 打点，`request_id` 能串起完整一次调用 | 阶段一（`logging.py`）、阶段四（中间件） |
| 所有对外响应符合 §3.1 信封，`meta.usage` 有值 | 阶段四 |
| 新错误码已登记进 §3.2 枚举，并由 `api/errors.py` 映射 | 阶段四 |
| 图的节点幂等：同一 thread 从任一检查点恢复重跑，不产生重复副作用 | 阶段三建立，阶段六起每个图复跑 |
| 没有 `MemorySaver`；检查点与业务表指向同一个库 | 阶段三 |
| SSE：`X-Accel-Buffering: no` 已设，前端逐字看到输出 | 阶段六 |
| `docker compose up` 从干净环境能起 | 阶段八 |
| 没有触碰 §4 禁区 | 全程 |

---

## 附录 B · 单点所有权 → 落位阶段（§2）

契约 §2 的每一项能力，只在一个文件实现，且只在**一个阶段**创建：

| 能力 | 唯一归属 | 创建于 |
|---|---|---|
| 构造模型实例 | `llm/client.py` | 阶段二 |
| 发 LLM 请求 | `llm/client.py` 暴露的模型实例 | 阶段二 |
| 模型 ID 常量 | `config.py` | 阶段一 |
| 修复 `reasoning_content` | `llm/deepseek_chat.py` | 阶段二 |
| 写 Prompt 文本 | `llm/prompts/*.jinja2` | 阶段二 ~ 八 |
| 统计 Token / 成本 | `llm/tracing.py` | 阶段二 |
| 定义工具 | `agent/registry.py` | 阶段六 |
| 图状态 schema | `agent/state.py` | 阶段六 |
| 拼装图与节点 | `agent/graphs/*.py` | 阶段六 / 七 / 八 |
| 检查点构造 | `agent/checkpointer.py` | 阶段三 |
| 长期记忆读写 | `agent/store.py` | 阶段三（构造）/ 八（使用） |
| 流式事件 → SSE 映射 | `api/routers/chat.py` | 阶段六 |
| 向量化文本 | `rag/embedder.py` | 阶段五 |
| 写向量库 | `rag/ingest.py` | 阶段五 |
| 读向量库 | `rag/retriever.py` | 阶段五 |
| 生成引用对象 | `rag/citations.py` | 阶段五 |
| 执行用户代码 | `sandbox/runner.py` | 阶段七 |
| 访问数据库 | `db/repositories.py` | 阶段三 |
| 异常 → HTTP 状态码 | `api/errors.py` | 阶段四 |
| 响应外壳 | `api/schemas.py` | 阶段四 |
| 异常层级 | `errors.py` | 阶段四 |
| 日志初始化 | `logging.py` | 阶段一 |

### 要避免的脚手架

这些"顺手加一下"会变成事实上的第二所有者，**从一开始就不要建**：

- 不建 `utils.py` / `helpers.py` / `common.py`，不建第二个 settings 模块。
- `db/models.py` 里**不出现 messages 表**。
- service 里**不随手 `@tool`** 装饰新函数。
- 测试 helper 里**不直接构造 `ChatDeepSeek`**。
- `ingest` 与 `retriever` **不各用一套 embedding**。
- `retriever.py` 之外**不出现 `collection.query()`**。
- 不在节点里自己拼 `chat.completions.create`。
- 不在别处 `raise HTTPException`。

### 工具清单覆盖核对

v1–v4 的全部工具，各自落位：

| 工具 | 归属文件 | 阶段 | 版本 |
|---|---|---|---|
| `search_docs` / `read_doc` | `agent/tools/docs.py` | 六 | v1 |
| `explain_error` / `run_code` | `agent/tools/` + `sandbox_tools.py` | 七 | v2 |
| `generate_exercise` / `run_tests` / `grade_code` | `agent/tools/grading.py` | 七 / 八 | v3 |
| `recommend_next` | `agent/tools/progress.py` | 八 | v4 |

---

## 待定项

以下不阻塞写文档，但**阻塞施工**。每条给出推荐值，需要人工拍板。

### 1. 向量模型（阻塞阶段五）

契约 §1 明确：DeepSeek 不提供 embedding 接口，必须单独选型。

- **推荐：本地 `sentence-transformers`**（如 `BAAI/bge-m3`）。理由：§5.1 要求测试离线可跑，本地模型能端到端满足，无需为 CI 额外搭 mock 或缓存。
- 若中文 query 需要匹配英文文档，需选多语模型。
- 无论选哪个，模型 id 必须是 `config.py` 的常量，且 CI 里 embedding 要用确定性 fake 桩掉以保持离线。

### 2. ~~Docker（阻塞阶段七的验收）~~ ✅ 已解决（2026-10-03）

Docker Desktop（WSL2 后端）已装并实测跑通：引擎 **29.8.1**、Compose **v5.5.1（插件形态）**、WSL2 Ubuntu。`docker run --rm hello-world` 端到端通过（§5.3 / §5.4 的沙箱验收前提已具备）。

- **仍须处理**：Docker Hub 直连超时，必须在 Docker Desktop → Settings → Docker Engine 配 `registry-mirrors`。**阿里云加速器已于 2026 年下线对 Docker Hub 的代理**（返回 403 并指向功能调整公告），别再用它。
- WSL2 资源上限写在 `C:\Users\<用户>\.wslconfig`（本机设 `memory=6GB` / `processors=8`），**这是阶段七 `SANDBOX_LIMITS` 的物理天花板**：容器内存上限必须明显小于它，否则 OOM 时无法区分是限额生效还是宿主内存耗尽。

### 3. 语料来源（阻塞阶段五）

§5.2 要求覆盖"Python 官方文档核心章节 + PEP 索引"，且 ingest 幂等可重跑。

- **推荐：下载官方 HTML 归档**（`docs.python.org/3.12` 的 docs-html zip）+ PEP 索引快照。理由：版本固定、评估可复现、一次性下载后全离线、不需要爬虫也不会被限流。
- 实时爬取的问题：慢、易被限流、上游改版即断，且 eval 基线会随上游漂移，破坏回归基线。
- 落点：`data/corpus/`，由 `rag/ingest.py` 消费。**禁止手工编辑**（§4）。

### 4. ~~`DEEPSEEK_API_KEY` 未设置~~ ✅ 已解决（2026-10-30）

本机已配置 key，模型名校验真机通过：`--check-models` 退出码 0，
`available=['deepseek-flash', 'deepseek-v4-pro']`，`/health.model_check_ok=true`
（`status=degraded` 仅因本机未起 Docker，`sandbox_ok=false`）。缺 key 时的策略已定为
**警告不硬失败**：`/health` 恒 200、`model_check_ok=false`。手动跑校验的命令与结论见
`docs/decisions/0001-模型名校验结论.md`。

### 5. 单日 Token 熔断阈值与错峰调度

§5.5 要求单日超限有熔断，§1 要求单价来自 `config.PRICE_TABLE`。

- 需人工定一个数（`config.py` 常量）。
- **评估跑分与批量任务应排在非峰值时段** —— 峰值时段为 UTC 周一至周五 01:00–04:00 与 06:00–10:00，其余时段半价。这是本项目最大的一笔免费节省。
- 契约里的价格表**仅用于量级判断与成本告警阈值设定**，不要硬编码进逻辑（§1）。

### 6. 两个延后的产品决定

- **是否切换到 Qdrant**：生产切 Qdrant 时只应改 `rag/store.py`（§1）。何时切是产品决定。
- **`frontend_telegram/` 是否在范围内**：契约 §2 标注为"可选适配器（v4 之后）"。
