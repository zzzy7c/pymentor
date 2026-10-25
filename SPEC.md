# SPEC — 阶段八 · v3 批改 + v4 学习路径 + Web UI + 部署 · 实施规格

> **归档声明**：阶段一（工程底座）、阶段二（LLM 基座）、阶段三（持久化与状态双源）、
> 阶段四（API 契约与错误体系）、阶段五（RAG 子系统）、阶段六（v1 文档问答 + SSE）、
> 阶段七（沙箱 + v2 错误诊断）的规格均已归档到 `docs/specs/`：
> `阶段一-工程底座.md`、`阶段二-LLM基座.md`、`阶段三-持久化与状态双源.md`、
> `阶段四-API契约与错误体系.md`、`阶段五-RAG子系统.md`、`阶段六-v1文档问答与SSE.md`、
> `阶段七-沙箱与v2错误诊断.md`。本文只描述**阶段八**。阶段七的归档件与原 `SPEC.md`
> **逐行一致**（`cmp` 校验通过）。
>
> 本文件是施工规格，不是契约。契约是 `AGENTS.md`；两者冲突时以 `AGENTS.md` 为准。
> 本文的每一条设计决定都能追到 `AGENTS.md` 的某个条款号或本文件的某条核实事实（§2）。

**阶段八 = v3 练习批改 + v4 学习路径 + 记忆 + Web UI + 部署**
（`ROADMAP.md`「阶段八 · v3 批改 + v4 学习路径 + Web UI + 部署」）。
它交付 `AGENTS.md` §5.4、§5.5、§5.6 的全部内容，并把项目从"本地能跑"推到"公网可访问"。

本阶段有三个与阶段七性质不同的风险：

1. **第一次有真正的持久业务写入。** 练习提交、错题、Token 账本都要落库（`AGENTS.md` §2）。
   写入一旦不是幂等的，节点恢复重跑就会制造重复记录；身份一旦没有定义，
   带外键的三张表根本插不进去（§2.2 S6/S7）。
2. **第一次有独立进程的前端。** `frontend/` 不 import `pyassist`，只经 HTTP 说话
   （`AGENTS.md` §2/§4）。契约从"测试里成立"变成"跨进程成立"，前端用的每个路径
   都必须是真实存在的端点。
3. **第一次真的部署。** 沙箱需要宿主 Docker，而 api 容器要访问它（阶段七 §10.1 的遗留项）。
   这条直接排除了不支持 Docker-in-Docker 的 PaaS，部署形态必须显式选择、显式写威胁模型。

> **前置阻塞（诚实声明）**：阶段七的真实隔离验收与跑分**尚未产生数字**
> （`docs/evals/阶段七-跑分.md` §7：`-m docker` 的 10 条未跑、`fix_pass_rate` 待填）。
> 阶段八的 v3 要在同一个沙箱里跑测试，**继承同一个未验证前提**。
> T0 的第一件事就是补上它；在补上之前，本阶段任何"沙箱内测试通过"的说法都只能算**未验证**。

---

## 0. 定位与硬指标

| 项 | 值 | 出处 | 本阶段怎么证明 |
|---|---|---|---|
| 交付版本 | v3（练习生成 + 批改）、v4（学习路径 + Web UI） | `AGENTS.md` §0 | 端点 + UI + 落库 |
| 覆盖主题 | **列表、字典、装饰器、异步** | §5.4 | `EXERCISE_TOPICS` 枚举 + 评估集 |
| 生成题自洽性 | **100%**：参考答案能让自己的测试全绿 | §5.4 | 生成图的 `self_check` 节点 + 跑分 |
| 测试结果结构化 | 通过/失败用例、stdout、stderr、耗时 | §5.4 | 测试结果 JSON 协议（D4） |
| 评分可解释 | 指出错在哪、为什么错、指向哪份文档 | §5.4 | 结构化 `GradeDraft` + 确定性引用 |
| 评分一致率 | **≥ 80%**（30 份人工标注） | §5.4 | 导出样本 → 人工标注 → `--agreement`（**手动**） |
| 历史与进度 | 刷新/重开后仍在，落库不靠 `session_state` | §5.5 | `GET /history` + `GET /stats` |
| 推荐可核对 | 必须引用该用户**实际做错**的题目 id | §5.5 | 引用校验节点 + 判据 9 |
| 流式可见 | SSE token 逐字上屏 | §5.5 | `/chat` 保持阶段六格式；UI 逐字渲染 |
| 成本 | `/stats` 可见；单日超限**熔断** | §5.5 | `services/usage.py` + 配额依赖 |
| 部署 | 干净环境 `docker compose up`；公网可访问 | §5.6 | `deploy/` + runbook；公网为**人工确认** |

**本阶段完成后，以下六件事必须同时成立：**

1. `POST /exercises` 的响应里**没有**测试用例、没有参考答案 —— 判分材料只在服务端；
2. 提交结果只认沙箱：`is_passed` 由测试报告决定，模型说"很好"不算；
3. 同一份 `(exercise_id, code)` 重放，`exercise_attempts` 与 `mistakes` 都**不会多出一行**；
4. `GET /history` 在页面刷新后仍能回放该会话的提交（数据来自库，不来自 Streamlit 状态）；
5. 单日 Token 达到上限后，所有会调用模型的端点返回 429 且**不再发起模型请求**；
6. `docker compose up` 从干净环境起得来，`/health` 的 `data.status == "ok"`（或明确降级原因）。

---

## 1. 范围边界

### 1.1 做

| 交付 | 对应 `AGENTS.md` |
|---|---|
| `agent/tools/grading.py`：`generate_exercise` / `grade_code` + 测试脚本拼装 + 结果解析 + 分数公式 | §0 v3 工具、§2「定义工具」 |
| `agent/tools/sandbox_tools.py` 增量：`run_tests`（复用 `execute_cached`） | §5.4「用户代码在沙箱内跑测试」 |
| `agent/tools/progress.py`：`recommend_next` 的实现（候选筛选 + 引用校验） | §0 v4 工具 |
| `agent/graphs/exercise.py`：生成图（`generate ⇄ self_check`）与批改图（`run_tests → grade`） | §1「用 LangGraph 的画布，不用 LangChain 的成品 Agent」 |
| `agent/graphs/progress.py`：错题 → 候选 → 结构化推荐 | §5.5 |
| `agent/state.py` 增量：`ExerciseState` / `GradingState` / `ProgressState` | §2「图状态 schema」唯一归属 |
| `agent/registry.py` 增量：注册四个新工具；按图取子集沿用 D7 | §2「定义工具」 |
| `services/exercise.py`：出题、提交、落库、错题翻转、记忆更新 | §2 数据流 |
| `services/progress.py`：读错题 → 调图 → 追加确定性依据 | §5.5 |
| `services/memory.py`：用户画像的读/写/渲染（持久化经 `agent/store.py`） | §2「记忆系统归属」 |
| `services/usage.py`：**唯一的配额检查与账本写入出口** | §2「统计 Token / 成本」 |
| `api/routers/exercises.py`：`POST /exercises`、`POST /exercises/{id}/submit` | §2 目录结构 |
| `api/routers/history.py`：`GET /history`（游标分页） | §5.4 / §5.5 |
| `api/routers/stats.py`：`GET /stats`（用量、通过率、错题分布、配额） | §5.5 |
| `api/routers/progress.py`：`POST /recommend`（**AGENTS §2 未列，见 §9.1**） | §5.5 |
| `api/schemas.py` 增量：v3/v4 的请求与响应模型；`HealthData` 增 `sandbox_ok` | §3.1 / §6「新增不是破坏性变更」 |
| `frontend/api_client.py` + `frontend/streamlit_app.py`：五页 UI，只经 HTTP | §2 / §4 |
| `Dockerfile.api` / `Dockerfile.web` / `deploy/prod.compose.yml` / `deploy/Caddyfile` | §1 部署 / §5.6 |
| `README.md` + `deploy/README.md`（架构、env 清单、一键启动、跑分方法、威胁模型） | §5.6 |
| `evals/datasets/exercises.jsonl`（8 条夹具，**只增不改**） + `evals/run_v3_eval.py` | §5.4 / §4 冻结 |
| 测试：图、API、仓储、配额、记忆、前端契约、守护 #38–#45 | §5.1 |
| `config.py` / `db/repositories.py` / `errors.py` / `api/deps.py` / `api/main.py` / `pyproject.toml` / `.env.example` / `docker-compose.yml` 增量 | —— |

### 1.2 不做（明确排除，验收时按此清单反查）

| 不建 | 归哪 | 理由 |
|---|---|---|
| 鉴权、注册、多租户 | 阶段八之后 | §5.4/§5.5 没有要求；固定 `user_id="local"`（D9），且必须在 README 明说"不可直接公网多租户" |
| 新业务表 / 新迁移 | 永不需要 | `exercise_attempts` / `mistakes` / `token_ledger` / `sessions` / `users` 已经够用（F4）；练习定义进检查点（D2） |
| `exercises` 表或"题目缓存表" | —— | 会让"题目真相"有两个来源，违反 §2 的双源划分 |
| 语义向量记忆 / 第二个向量库 | 永不 | §2 明禁；Store `index=None` 保持（D6） |
| Qdrant 切换 | 阶段八之后 | `ROADMAP` 待定项 6，产品决定 |
| Telegram 适配器 | 阶段八之后 | `AGENTS.md` §2 标注"可选适配器" |
| `/diagnose` 流式化 | 阶段八之后 | 阶段七 D8 的延续；UI 用 spinner + 最终信封（D11） |
| 前端 `import pyassist` | 永不 | §2 / §4；守护 #38 |
| 沙箱内使用 pytest | 永不 | 镜像只有标准库（F10），且不该为测试框架扩大镜像 |
| 用 `subprocess` / `exec()` / `eval()` 跑用户代码 | 永不 | §4；守护 #4 |
| 为后续留的空函数 / TODO 占位 | —— | 缝只以"唯一入口 + 已归一化的数据 + 守护测试"形式存在 |

---

## 2. 前置核查结果（2026-10-22 读代码 / 只读命令）

**本节所有结论都来自当前工作区的实际代码与命令输出，不是推测。**
未经验证的行为另列 §2.3，施工第一步（T0）必须先测，测完把结果回填。

### 2.1 已核实、可直接复用的缝（不要重造）

| 缝 | 位置 | 阶段八怎么用 |
|---|---|---|
| `get_chat_model(*, tier="flash", thinking=True, ...)` | `llm/client.py:57` | 生成走 `tier="pro"`、批改走 `tier="flash"`（D5 / config 常量） |
| `ModelTier` + `_TIER_TO_MODEL_ID` | `llm/client.py:31-33` | 新图里只认 `tier`，模型 ID 仍只写在 `config.py` |
| `check_models()` / `get_last_model_check()` | `llm/client.py:94-152` | D13：启动时异步跑一次，结果喂 `/health` |
| `SandboxResult` / `sandbox_key` / `truncate_output` | `sandbox/runner.py:49-119` | `run_tests` 直接复用；测试结果标记放**尾部**，截断保留尾行 |
| `SandboxRunner` 协议 | `sandbox/runner.py:122-133` | **不扩签名**（S2）；测试脚本拼成单文件后走 `run(source)` |
| `execute_cached` | `agent/tools/sandbox_tools.py:26-40` | `run_tests` 与 `run_code` 共用同一份缓存语义 |
| `InjectedToolArg` 注入模式 | `agent/tools/docs.py:155-202`、`sandbox_tools.py:43-58` | 模型只看 `topic` / `code`；`exercise` / `report` / `model` / `runner` 全部隐藏 |
| `tool_specs(names)` / `get_tool()` / `TOOLS` | `agent/registry.py:39-66` | 追加四个工具；按图取子集（D7 沿用） |
| `AppState` + `_overwrite` reducer | `agent/state.py:52-64` | 三个新 State 继承，只加键；入口节点必须补齐 `AppState` 的五个键 |
| `ToolMessage.artifact` 当轮读取 | `agent/graphs/qa.py:168-206` | 测试结果只在该轮用，不跨轮依赖（S3） |
| `_trim_history` / `_is_complete_group` | `agent/graphs/qa.py:325-373` | 若生成图需要历史裁剪，**直接 import**，不复制 |
| `extract_usage` / `log_llm_response` / `add_usage` / `model_id_of` | `llm/tracing.py:88-168` | 每次结构化输出都要记账；`include_raw=True` 才有 usage（S5） |
| `build_citations` / `render_reference_block` | `rag/citations.py` | v4 的文档链接只由这里产生 |
| `retrieve()` / `assert_index_ready()` | `rag/retriever.py:153-219` | 通过 `Searcher` 注入；图不碰 Chroma |
| `record_attempt` / `record_mistake` / `append_token_usage` | `db/repositories.py:55-142` | 落库唯一入口；幂等权威是 `idempotency_key` 唯一约束 |
| `ensure_user` / `create_chat_session` | `db/repositories.py:29-52` | 落库前必须先建父行（S6/S7） |
| `list_attempts` / `count_attempts` | `db/repositories.py:160-175` | 阶段八扩成游标分页；`init` 已有 |
| `ExerciseAttempt` / `Mistake` / `TokenLedger` | `db/models.py:56-113` | 列名与 `UsageSnapshot` / 契约逐字对应，不需要映射层 |
| `open_store` / `setup_store` | `agent/store.py:37-72` | D6：lifespan 里打开；`index=None` 不动 |
| `open_checkpointer` | `agent/checkpointer.py:44-89` | 练习定义与生成状态都走这个检查点 |
| `create_app(...)` 全默认参数注入 | `api/main.py:116-136` | 新参数必须带默认值（S10）；测试靠它注入 fake |
| `app.router.routes.extend(...)` 挂载法 | `api/main.py:164-167` | 新 router 必须沿用；`include_router` 会让 slowapi 静默失效 |
| `get_qa_graph` 的"缺失即 `ConfigError`" | `api/deps.py:65-78` | `get_store` / `get_exercise_graphs` / `get_progress_graph` 照抄 |
| `build_meta` / `usage_to_schema` | `api/deps.py:45-48`、`api/schemas.py:168-180` | 新路由的信封与用量转换 |
| `Page[T]`（items/total/next_cursor） | `api/schemas.py:69-74` | `/history` 的唯一列表口径（S9） |
| SSE 事件白名单 | `api/routers/chat.py:37-43` | 封闭；记账只能走 `stream_answer` 钩子（S8） |
| `stream_answer` 的 usage 汇总 | `services/qa.py:66-110` | D8：加 `on_usage` 钩子，不新增事件 |
| `ScriptedChatModel` | `tests/fakes/llm.py:27-109` | 新图测试的基础；需补 `with_structured_output` 脚本 |
| `FakeSandboxRunner` | `tests/fakes/sandbox.py` | 按脚本返回 `SandboxResult`，**绝不执行代码** |
| `create_app` 的夹具注入先例 | `tests/conftest.py:96-182` | 新 app 夹具照此写 |
| `docker>=7.1` / `docker` marker / `-m 'not docker'` | `pyproject.toml:57,149-157` | 真实自洽测试沿用同一 marker |

**结论：阶段八不改任何已发布的 SSE 结构，不改既有 schema 字段，不新增业务表。**

### 2.2 已核实的实现约束（S1–S14，写错就不过验收）

| # | 事实（来源） | 对实现的强制后果 |
|---|---|---|
| S1 | 沙箱镜像是 `python:3.12-slim`，以 `python -I -u -c <引导脚本>` 启动：代码经 **stdin** 送进可写的 `tmpfs /tmp`，落成 `/tmp/main.py` 后 `execv` 到 `python -I -u /tmp/main.py`（`runner.py`；只读根文件系统下 `put_archive` 被 daemon 拒绝，见 ADR 0004 决定 2 第 3 条）。只有标准库，`/work` 不写入任何文件 | 测试必须用 `unittest`；用户代码与测试必须拼成**一个** `main.py`；`-I` 下不能 `import` 同目录模块 |
| S2 | `SandboxRunner` 协议只有 `run(source, *, stdin, timeout_seconds)`（`runner.py:122-133`） | `run_tests` 不扩协议；拼装发生在 `grading.py`，执行仍走 `run()` |
| S3 | `ToolMessage.artifact` 过检查点后变 `dict`（阶段六 L3/L4） | 测试报告只在当轮读；需要跨轮时用 `TestReport` dataclass 写进状态，而不是复用 artifact |
| S4 | 检查点 serde 对 frozen dataclass 与 `Decimal` 原样还原（阶段六 L3 实测） | `ExerciseSpec` / `TestReport` 用 frozen dataclass，不用 dict 穿层 |
| S5 | `with_structured_output` 默认走"绑工具 + 解析"，**默认丢 usage**；`include_raw=True` 才返回原始 `AIMessage`（阶段七 S10）。且默认的 `method="function_calling"` 会绑 `tool_choice`，**思考模式不支持**（2026-10-25 实测 400 `Thinking mode does not support this tool_choice`） | 生成、批改、推荐、修复四处结构化输出一律经 `llm/client.py::get_structured_output`（json_mode + `include_raw=True`），既保住 usage，也不带 `tool_choice` |
| S6 | `exercise_attempts.session_id` 是 `sessions.id` 的外键（`db/models.py:61-62`、`0001_init.py:42-57`） | 落库前必须 `ensure_user` + `create_chat_session`；否则 SQLite 上看似能插，切 PG 立刻炸 |
| S7 | `token_ledger.session_id` 同样是外键（`db/models.py:100-103`） | 记账前必须有会话行；匿名请求也要先建档（D8/D9） |
| S8 | `QaEvent` 与 `_PAYLOAD_KEYS` 是封闭白名单（`agent/state.py:25-44`、`api/routers/chat.py:37-43`） | `/chat` 的账本写入只能挂在 `stream_answer` 的完成钩子上；新增事件类型 = 改线上格式 |
| S9 | `Page[T]` 固定为 `items/total/next_cursor`（`api/schemas.py:69-74`） | `/history` 不能返回裸数组；`next_cursor` 必须能续页 |
| S10 | `create_app` 的所有注入参数都有默认 `None`（`api/main.py:116-126`） | 新参数必须同样带默认值，否则既有 299 个测试全红 |
| S11 | 守护 #4 用 AST 禁止 `src/**` 的 `exec()` / `eval()` / `os.system` / `import subprocess`（`tests/test_contract_guardrails.py:134`） | 测试脚本只能作为**普通字符串**拼装后送进沙箱；`src/` 里不得出现任何执行路径 |
| S12 | `data/**` 是禁区（`AGENTS.md` §4） | 前端与部署脚本不得写 `data/`；容器只通过卷挂载读写 |
| S13 | `settings.daily_token_limit` 已声明但**无读取方**；`token_ledger` 从未在请求路径写入（`config.py:324`、`repositories.py:113-142`） | 本阶段第一次接线的就是它；`/stats` 与熔断读同一张表 |
| S14 | `agent/store.py::open_store` 已实现但**未接进 lifespan**，`services/memory.py` 不存在（`main.py:84-113`） | 记忆功能的第一步是接线，不是写第二套存储 |

### 2.3 待 T0 实测（**未验证，施工前不得当作事实**）

| # | 待测项 | 为什么要测 | 若结论为否的退路 |
|---|---|---|---|
| U1 | 阶段七遗留：Docker daemon、`pyassist-sandbox:1`、`-m docker` 10 条、真实跑分 | v3 的"跑测试"与 v2 的"跑代码"是同一个沙箱；隔离没验过，v3 的通过率也没有意义 | 阻塞 v3 验收；离线部分用 fake 推进，结果记"未验证" |
| U2 | `unittest` 自定义 `TestResult` 在 `-I` + 只读根文件系统 + `tmpfs /tmp` 下能否正常产出 JSON | 决定 D4 的结果协议是否成立 | 退回"解析 unittest 文本输出"（脆弱，需固定 `-v` 格式）；或让脚注把 JSON 写到 `/tmp` 再读日志（不可行，需改 runner，故优先保 D4） |
| U3 | 单文件拼接时用户代码顶层的 `if __name__ == "__main__"` 是否会先执行并干扰测试 | 拼装方案的已知弱点 | prompt 明令禁止 + `self_check` 发现即重生成；仍失败则 `LLMBadResponse` |
| U4 | `python:3.12-slim` 冷启动 + 一次 unittest 的总耗时是否仍落在 `timeout_seconds=10` | 测试比单脚本多几个 import；10s 可能不够 | 在 `SandboxLimits` 增 `test_timeout_seconds`（默认 15），并发跑分文档记录 |
| U5 | `with_structured_output(ExerciseDraft)` 在思考模式下是否可用 | DeepSeek 的 JSON 模式与思考模式组合在阶段七只验证过 `RepairDraft` | 按 `AGENTS.md` §1 兜底：只给生成节点 `thinking=False`，写 ADR |
| U6 | api 镜像内安装 `sentence-transformers` + CPU torch 的体积与首次模型下载 | 决定部署形态与卷布局（R7） | 用宿主预下载 + `HF_HOME` 卷；再不行改第三方 embedding（产品决定） |
| U7 | Streamlit 1.57 对 SSE 的逐字渲染观感 | §5.5「流式输出可见」是体验指标 | 改用 `st.write_stream` + 自解析；最差退回轮询式增量显示 |
| U8 | Caddy 反代下 SSE 是否被缓冲 | 缓冲会让"逐字上屏"变成"一次性吐出" | `flush_interval -1` + `X-Accel-Buffering: no`；实测后再定 |

---

## 3. 目标文件清单

`✓` 新建 ｜ `△` 修改 ｜ `★` 契约关键件

```
src/pyassist/
├── config.py                       △  阶段八常量：主题、档位、分页、配额、用户（§5.1）
├── errors.py                       △  ExerciseNotFound / QuotaExceeded（复用既有 ErrorCode，D10）
├── agent/
│   ├── state.py                    △  ExerciseState / GradingState / ProgressState（§5.2）
│   ├── registry.py                 △  注册 generate_exercise / run_tests / grade_code / recommend_next
│   ├── store.py                    ×  **不改**：构造点已在，本阶段只接线
│   ├── tools/
│   │   ├── grading.py              ✓ ★ 测试脚本拼装 / 结果解析 / 分数 / 两个工具实现（§5.3）
│   │   ├── sandbox_tools.py        △  run_tests（复用 execute_cached，§5.4）
│   │   └── progress.py             ✓  recommend_next 的实现（§5.5）
│   └── graphs/
│       ├── exercise.py             ✓ ★ 生成图与批改图（§5.6）
│       └── progress.py             ✓  v4 图：候选 → 结构化推荐 → 校验（§5.7）
├── services/
│   ├── exercise.py                 ✓ ★  出题 / 提交 / 落库 / 错题 / 记忆（§5.8）
│   ├── progress.py                 ✓  recommend_for_user（§5.9）
│   ├── memory.py                   ✓ ★  画像读写与渲染（§5.10）
│   └── usage.py                    ✓ ★  配额 + 账本（唯一出口，§5.11）
├── api/
│   ├── schemas.py                  △  只追加 v3/v4 模型 + HealthData.sandbox_ok（§5.12）
│   ├── deps.py                     △  get_store / get_exercise_graphs / get_progress_graph / require_quota
│   ├── main.py                     △  lifespan 开 store、编译三张新图、异步模型校验、挂 4 个 router
│   └── routers/
│       ├── exercises.py            ✓  POST /exercises、POST /exercises/{id}/submit（§5.13）
│       ├── history.py              ✓  GET /history（§5.13）
│       ├── stats.py                ✓  GET /stats（§5.13）
│       └── progress.py             ✓  POST /recommend（§5.13；AGENTS §2 未列）
├── db/
│   ├── models.py                   ×  **不改**：没有新表（§1.2）
│   └── repositories.py             △  错题查询 / 翻转 / 游标分页 / 统计 / 当日用量（§5.14）
└── evals/
    ├── datasets/exercises.jsonl    ✓  8 条夹具（每主题 2 条，只增不改，§5.18）
    ├── metrics.py                  △  self_consistency_rate / grading_agreement
    └── run_v3_eval.py              ✓  CLI 跑分器（四种模式，§5.18）

frontend/
├── api_client.py                   ✓  仅 HTTP；SSE 解析；信封 → ApiError（§5.15）
└── streamlit_app.py                ✓  五页：问答 / 诊断 / 练习 / 历史与进度 / 统计

Dockerfile.api                      ✓  api 镜像（非 root，§5.16）
Dockerfile.web                      ✓  Streamlit 镜像（只装 web 依赖，§5.16）
deploy/
├── prod.compose.yml                ✓  api + web + caddy；api 挂 docker.sock
├── Caddyfile                       ✓  TLS + SSE 不缓冲
└── README.md                       ✓  部署 runbook + 威胁模型 + 发布清单

README.md                           ✓  架构图、env 清单、一键启动、跑分方法（§5.17）
docs/decisions/0003-阶段八契约增补.md ✓  记录 §9 的增补与批准状态
docs/decisions/0004-部署形态与沙箱边界.md ✓  记录 D12 与 docker.sock 威胁模型
docs/evals/阶段八-跑分.md           ✓  方法与结果（结果留空，未跑不填）
scripts/prune_threads.py            ✓  手动清理过期 thread（待定项 11）

tests/
├── conftest.py                     △  exercise_app / progress_app / fake store 夹具
├── fakes/llm.py                    △  结构化输出脚本（ExerciseDraft / GradeDraft / NextStepDraft）
├── fakes/store.py                  ✓  内存版 Store 替身（若上游无现成）
├── test_grading_contract.py        ✓  拼装 / 解析 / 分数 / 截断后仍可取结果
├── test_exercise_graph.py          ✓  生成自校验回环 / 批改判定 / 失败回环
├── test_exercise_api.py            ✓  信封 / 不泄露测试 / 404 / 配额
├── test_progress_graph.py          ✓  非法引用被剥掉 / 必须引用真实错题
├── test_progress_api.py            ✓  信封 / 无错题时的空推荐
├── test_history_api.py             ✓  游标分页 / 刷新可回放
├── test_stats_api.py               ✓  与账本一致 / 通过率 / 错题分布
├── test_usage_quota.py             ✓  429 / Retry-After / 账本幂等 / 不再调模型
├── test_memory.py                  ✓  画像写入读取 / 确定性渲染 / 不碰第二个库
├── test_frontend_contract.py       ✓  无 pyassist import / 路径 ∈ OpenAPI
├── test_exercise_eval.py           ✓  数据集 schema + 指标纯函数
├── test_exercise_self_consistency.py ✓ 夹参考答案过自身测试（`-m docker`）
└── test_contract_guardrails.py     △  追加 #38–#45

pyproject.toml                      △  web 依赖组 + 可选 CI 配置说明
docker-compose.yml                  △  api 挂 docker.sock + HF 卷 + healthcheck（开发）
.env.example                        △  DAILY_TOKEN_LIMIT 默认值、PAYASSIST_API_BASE_URL
```

**不创建**：任何新表 / 迁移、`frontend_telegram/`、状态库替身、`exercises` 表、
第二个向量库、`/diagnose` 的 SSE 版本、任何占位空函数。

---

## 4. 关键设计决定（D1–D14，施工时不可协商）

### D1 · 三个里程碑，每段独立可验收

- **8A（v3）**：`generate → self_check`、`run_tests → grade`、两个端点、落库与错题。
- **8B（v4 + 记忆 + 账本）**：`recommend`、`/history`、`/stats`、配额、`services/memory.py`。
- **8C（UI + 部署）**：`frontend/`、镜像、compose prod、Caddy、README、演示。

理由：本阶段体量是阶段七的两倍以上，把"能演示的 UI"绑在"数据库、记忆、配额"全部完成之后，
会让任何一处卡住都无法交付。8B 不依赖 UI；8C 只消费已经冻结的 HTTP 契约。
**里程碑不是"可以跳过验收"，而是"可以按段回滚"。**

### D2 · 练习定义只存检查点，不下发客户端，不建表

`exercise_id` **就是**生成图那一次的 `thread_id`。题目、测试用例、参考答案、自校验次数
全部在 `ExerciseState` 里，由 LangGraph 检查点持久化。`POST /exercises` 只返回
`exercise_id / topic / title / prompt / starter_code`。

- 依据：`AGENTS.md` §2「图执行状态 = 检查点」「业务表不重复存对话消息」。
- 提交时由 `services/exercise.py` 用 `graph.aget_state({"configurable": {"thread_id": exercise_id}})`
  取回题目；取不到就是 `ExerciseNotFound`（404）。
- **不建 `exercises` 表**：那会让"题目真相"有两个来源，并且必须再写一套清理/迁移。
- **不下发测试**：客户端拿到测试就能改判分材料；这是评分完整性的底线。
- 代价：检查点会多出题目与测试（约几 KB/题）。可接受，§8 R5 有上限。

### D3 · 一个模块两张图，共享 State 与拼装函数

`agent/graphs/exercise.py` 导出两个构建函数：

```python
def build_exercise_graph(*, checkpointer, model=None, runner=None) -> ExerciseGraphs: ...
# ExerciseGraphs(generation=..., grading=..., model_id=...)
```

- **生成图**：`START → generate → self_check →(通过) finalize → END`
  `self_check` 不通过且 `attempts < MAX_EXERCISE_ATTEMPTS` 时回到 `generate`（带失败反馈）；
  达到上限直接 `raise LLMBadResponse(..., details={"stage": "self_check"})`。
- **批改图**：`START → run_tests → grade → finalize → END`。输入是 `(ExerciseSpec, submitted_code)`，
  由服务层从生成图的检查点取出后传入 —— 批改图**不读**另一条 thread。
- 为什么同文件：两者共享 `compose_test_script` / `TestReport` / `ExerciseState` 的字段语义；
  拆两个文件会造两个所有者，而 `AGENTS.md` §2 最忌讳平行实现。
- 为什么批改图不自己去读生成检查点：图应该是输入到输出的纯变换；跨图读状态会让
  "哪张图是真相"再次分叉。

### D4 · 沙箱内用 `unittest` + 单文件 + JSON 结果协议

沙箱镜像只有标准库，且只写入一个 `main.py`（落点是 `tmpfs /tmp`，见 S1）。因此：

```
main.py = 用户提交代码
        + "\n\n# ---- tests ----\n"
        + 题目测试类（ExerciseTests，unittest.TestCase）
        + 结果脚注（固定模板，见 §5.3）
```

结果脚注用 `unittest.TestResult` 子类收集每条用例的 `name / status / message / duration_ms`，
在**最后一行**打印 `__PYASSIST_RESULT__` + JSON。退出码恒为 0 ——
"跑完了"和"过了"是两件事，用报告表达后者，不靠进程退出码。

- 不给 `SandboxRunner` 加参数（S2）；不改镜像（`AGENTS.md` §4 要求人工 review）。
- **已知限制**（写进 U3 与 prompt）：用户代码顶层若写 `if __name__ == "__main__"` 会先执行；
  prompt 明令禁止，生成器的 `self_check` 会捕获"参考答案跑不过"的情况并重试。
- 结果标记放尾部：`truncate_output` 保留头尾各 N 行，尾部标记不会丢。
  解析失败（没有标记 / JSON 损坏 / 进程非零退出）一律视为 `is_complete=False`。

### D5 · 分数确定性，反馈模型化

- `is_passed = (total > 0 and passed == total and failed == 0 and errors == 0)`；
- `score = round(100 * passed / total)`；
- 模型只产 `GradeDraft`：`{issues: [{where, why, hint}], doc_queries: [str], summary: str}`，
  **没有分数字段**。模型无权改判。
- 依据：§5.4 要求"评分与人工评分一致率 ≥ 80%"，而人工一致率只有在一个**可复现**的基线
  上才有意义；让模型自由打分，同一份代码两次提交会给出两个分数，无法回归。
- 文档引用不由模型写 URL：模型只给 `doc_queries`，由 `rag/retriever.py` + `rag/citations.py`
  生成链接（`AGENTS.md` §2「生成引用对象」唯一归属）。

### D6 · 记忆 = Store 的 key-value 画像，确定性聚合

- 命名空间 `("profiles", user_id)`，键 `"summary"`，值是一个纯 dict：
  `{user_id, topics: {topic: {"attempts": n, "failures": n}}, unresolved_mistakes: n,
    recent_exercise_ids: [...], updated_at: ISO8601}`。
- `services/memory.py` 只做三件事：`load_profile` / `update_profile` / `render_profile_block`。
  持久化一律经 `agent/store.py`（`AGENTS.md` §2）。
- **不调模型做摘要**：摘要内容是可数的（尝试数、失败数、未解决数），模型的"总结"只会
  引入不稳定。ROADMAP 的"摘要记忆"以确定性聚合兑现。
- 注入位置：需要画像时放在 **human 消息尾部**，绝不进 system。
  依据：`AGENTS.md` §1 要求 prompt 前缀逐字节稳定；把随用户变化的画像塞进 system 会击穿前缀缓存。
- 短期记忆 = 检查点（已有），长期记忆 = Store；不新增第三处。

### D7 · 推荐必须可核对，引用由确定性代码生成

候选集来自 `repositories.list_mistakes(user_id, only_unresolved=True)`（确定性、可测）。
模型只产 `NextStepDraft {topic, reason, exercise_ids, next_steps}`：

1. 校验 `exercise_ids ⊆ 候选集的 exercise_id`；非法 id 剥掉并打 `progress.invalid_refs`；
2. 若剥完为空，用候选集里最近一条补上（推荐不能是空话）；
3. 文档链接由 `build_citations(检索片段)` 产生，模型不写 URL；
4. 输出末尾追加确定性"依据"块：`- 你在 ex_xxx（topic）上未通过`。

依据：§5.5「必须引用该用户实际做错的题目 id」。**模型不可能知道哪些 id 真实存在** ——
它只能从候选集里挑，校验是必须的。

### D8 · 配额与账本只有 `services/usage.py` 一个出口

```python
async def assert_daily_quota(session, *, user_id, limit) -> None: ...
async def record_usage(session, *, request_id, session_id, model_id, usage) -> bool: ...
async def quota_status(session, *, user_id, limit) -> QuotaStatus: ...
```

- 账本幂等键：`f"req:{request_id}"`。同一 `request_id` 重放只落一行。
- `model_id` 从图对象上取：`QAGraph` / `DiagnoseGraph` / `ExerciseGraphs` / `ProgressGraph`
  都暴露 `model_id: str`（构建时由 `model_id_of(model)` 得到）。账本需要 `model_id` 列，
  而 `UsageSnapshot` 不含它 —— 不在记账处猜模型名。
- `/chat` 的 usage 只有流结束时才知道：`services/qa.py::stream_answer` 增加
  `on_usage: Callable[[UsageSnapshot], Awaitable[None]] | None = None`，在 `done` 之前调用一次。
  **不新增 SSE 事件**（S8）。
- 并发竞态的诚实描述：`assert → 跑模型 → 记账` 不是一个事务，两个并发请求可能同时通过检查。
  上限因此是**软上限**：最坏超额 = 并发请求数 × 单请求最大消耗。不假装它是硬闸门；
  真正的硬闸门是部署层的 slowapi 速率限制。
- 配额检查挂成 router 级依赖（`APIRouter(dependencies=[Depends(require_quota)])`），
  新路由自动继承；`/history`、`/stats` 不挂（不产生模型调用）。

### D9 · 身份：固定用户 + 客户端会话；明说不是安全边界

- `DEFAULT_USER_ID = "local"`（`config.py`）。没有鉴权、没有注册。
- `session_id` 由客户端给出（`st.query_params`），缺省用 `request_id`。
- 所有会写账本/练习的路由先 `ensure_user` + `create_chat_session`（S6/S7）。
- **`sessions.id` 是 `String(32)`，而契约里的 `session_id` 上限是 64。**
  落库前统一经 `services/usage.py::db_session_id(raw)`：合法且 ≤32 原样用，否则
  `"s" + sha256(raw).hexdigest()[:31]`。绝不为这件事改表结构。
- README 与 `deploy/README.md` 必须写明：**这套身份不是安全边界，公网只允许单人使用**。

### D10 · 错误码零新增，两个新异常类复用既有码

| 异常 | code | HTTP | 场景 |
|---|---|---|---|
| `ExerciseNotFound` | `NOT_FOUND` | 404 | `exercise_id` 在检查点里不存在 |
| `QuotaExceeded` | `RATE_LIMITED` | 429 + `Retry-After` | 当日 Token 达到上限 |

- 不新增 `ErrorCode` 取值：`NOT_FOUND` 与 `RATE_LIMITED` 已在 §3.2 登记。
- 但两个异常类需要进 `api/errors.py` 的 `_MAPPING`，并由 `test_error_mapping.py` 穷尽覆盖。
- `AGENTS.md` §3.2 需补两句语义（`NOT_FOUND` 可用于业务资源、`RATE_LIMITED` 也承载配额），
  按 §6 流程留 ADR（§9.3）。`API_VERSION` 不变。
- 为什么不用 `LLMRateLimited`：那是**上游**限我们；配额是**我们**限调用方。
  §3.2 明令两者不能混。

### D11 · 前端只经 HTTP，SSE 自己解析，刷新靠 URL 参数

- `frontend/api_client.py` 只用 `httpx`，**不 import `pyassist`**（守护 #38）。
- `/chat` 的 SSE 用 `httpx.Client.stream("POST", ...)` + 逐行解析 `event:` / `data:`；
  遇到 `done` 结束。解析器是纯函数，可离线单测。
- `session_id` 放 `st.query_params["sid"]`：刷新页面后从 URL 取回，历史与进度从
  `GET /history` 重新拉 —— 满足 §5.5「不靠 session_state」。
- 错误信封（`ok=false`）统一转成 `ApiError(code, message)`，UI 展示 `code + message`，
  不展示 traceback。
- `/diagnose` 非流式：`st.spinner` + 最终信封（阶段七 D8 的延续）。
  唯一要求"逐字上屏"的是 `/chat`（§5.5 的措辞就是 token），它已经具备。

### D12 · 部署 = 单台 VM + compose + Caddy

沙箱容器由宿主 Docker 创建，而 api 运行在容器里 → api 必须挂 `/var/run/docker.sock`。
这等价于把宿主 root 交给 api 进程，因此：

- **不用 Railway / Fly 这类 PaaS**：没有 Docker-in-Docker，`/diagnose` 与 `/submit` 直接不可用。
- 目标形态：单台 Linux VM（≥4 vCPU / 8GB，沙箱 `mem_limit=512m` 是它的子集），
  `deploy/prod.compose.yml` 起 `api` / `web` / `caddy`；沙箱镜像在**宿主**预构建。
- **威胁模型必须写进 `deploy/README.md`**：
  - 被挂 socket 的 api 容器若被攻破，等于宿主 root；
  - 缓解：api 只跑我们的代码、不执行用户输入；用户代码只经 `sandbox/runner.py` 进独立容器，
    容器参数由 `SANDBOX_LIMITS` 固定（S1/S11）；
  - 不接受"为了省事让 api 直接 `docker run` 用户代码"或"合并 api 与 sandbox 容器"。
- TLS 由 Caddy 自动签发；SSE 需要 `flush_interval -1` 与 `X-Accel-Buffering: no`（阶段六已设响应头）。
- **公网可访问是人工验收项**：Agent 能交付配置、能证明本地 `up` 可起，但不能替用户买域名/开端口。
  未实际访问过就写"未验证"（§7.2 判据 18）。

### D13 · `/health` 增 `sandbox_ok`，启动时异步校验模型名

- `HealthData` 追加 `sandbox_ok: bool | None`（追加字段，`API_VERSION` 仍 `v1`）。
  `None` = 尚未探测；`False` = 沙箱不可用但 v1 仍活；`status` 相应为 `degraded`。
  这是阶段七 §10.4 的延期项，本阶段兑现。
- `AGENTS.md` §1 要求"启动时调一次 `models.list()`"。同步会阻塞启动（网络超时最坏几十秒），
  因此用 `asyncio.create_task` + 15s 超时：成功/失败都写进 `get_last_model_check()`，
  `/health` 读它；shutdown 时 cancel。ADR 记录这是等价实现（§9.5）。
- 模型名仍只写在 `config.py`；本阶段不新增模型名。

### D14 · 评估的诚实边界

| 能自动证明的（CI / 默认 pytest） | 只能手动证明的 |
|---|---|
| 数据集结构、分数公式、幂等键、解析器、图与 API 的离线行为 | 生成题自洽率（要真实模型） |
| 夹具参考答案过自身测试（`-m docker`，不要模型） | 评分与人工一致率（要人标注） |
| 前端路径 ∈ OpenAPI、无 `import pyassist` | 公网可访问、演示录像 |

- **未跑的数字不填。** `docs/evals/阶段八-跑分.md` 的表格初始写"待填"，与阶段七同一纪律。
- 禁止为了分数好看修改 `examples.jsonl` 已有用例、抽样简单题、或改 `metrics.py` 的判定规则
  （`AGENTS.md` §4 评估集纪律）。

---

## 5. 关键文件规格

### 5.1 `config.py` 增量

```python
# ---------- 阶段八：v3 / v4 / 配额（SPEC §5.1）----------
EXERCISE_TOPICS: Final[tuple[Literal["list", "dict", "decorator", "async"], ...]] = (
    "list", "dict", "decorator", "async",
)
MAX_EXERCISE_ATTEMPTS: Final = 2          # 生成自校验回环上限
EXERCISE_MODEL_TIER: Final[Literal["pro"]] = "pro"
GRADE_MODEL_TIER: Final[Literal["flash"]] = "flash"
PROGRESS_MODEL_TIER: Final[Literal["pro"]] = "pro"

EXERCISE_PROMPT_NAME: Final = "exercise"
EXERCISE_CONTEXT_PROMPT_NAME: Final = "exercise_context"
GRADE_PROMPT_NAME: Final = "grade"
GRADE_CONTEXT_PROMPT_NAME: Final = "grade_context"
PROGRESS_PROMPT_NAME: Final = "progress"
PROGRESS_CONTEXT_PROMPT_NAME: Final = "progress_context"

EXERCISE_TOOL_NAMES: Final[tuple[str, ...]] = (
    "generate_exercise", "run_tests", "grade_code",
)
PROGRESS_TOOL_NAMES: Final[tuple[str, ...]] = ("recommend_next",)
"""两张新图的允许清单。定义仍只在 `agent/registry.py`（D7 沿用）。"""

DEFAULT_USER_ID: Final = "local"
HISTORY_PAGE_SIZE: Final = 20
MAX_HISTORY_PAGE_SIZE: Final = 100
DEFAULT_DAILY_TOKEN_LIMIT: Final = 500_000   # 待定项 3；留空/None = 不熔断

ERROR_SIGNATURE_MAX_CHARS: Final = 256
MAX_EXERCISE_CODE_CHARS: Final = 8_000
MAX_TESTS_PER_EXERCISE: Final = 12
MAX_TEST_METHOD_CHARS: Final = 4_000
MISTAKE_LIST_LIMIT: Final = 50
PROFILE_NAMESPACE: Final = "profiles"

TEST_RESULT_MARKER: Final = "__PYASSIST_RESULT__"
"""测试结果协议的唯一标记（§5.3）。只允许出现在 `grading.py` 与测试里（守护 #41）。"""
```

- `Settings.daily_token_limit` 的语义改为：`None` = 取 `DEFAULT_DAILY_TOKEN_LIMIT`；
  显式设 `0` = 关闭熔断（仅供本地调试，README 要写明）。
- 所有常量只在此处定义；`services/` 与图里不得出现字面量。

### 5.2 `agent/state.py` 增量

```python
Topic = Literal["list", "dict", "decorator", "async"]

@dataclass(frozen=True, slots=True)
class TestCaseSpec:
    name: str            # 只能是 [A-Za-z0-9_] 的方法名，不含 "def "
    source: str          # 一个完整的测试方法源码

@dataclass(frozen=True, slots=True)
class ExerciseSpec:
    exercise_id: str
    topic: Topic
    title: str
    prompt: str
    starter_code: str
    reference_solution: str
    tests: list[TestCaseSpec]
    self_check_attempts: int

@dataclass(frozen=True, slots=True)
class TestOutcome:
    name: str
    status: Literal["passed", "failed", "error", "skipped"]
    message: str          # 失败/错误时的 traceback 尾段；通过时为空
    duration_ms: int

@dataclass(frozen=True, slots=True)
class TestReport:
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    outcomes: list[TestOutcome]
    duration_ms: int
    is_complete: bool     # False = 没拿到 JSON 标记（用户代码崩在顶层 / 超时）

class ExerciseState(AppState):
    topic: Annotated[Topic, _overwrite]
    exercise: Annotated[ExerciseSpec | None, _overwrite]
    self_check_report: Annotated[TestReport | None, _overwrite]
    generation_attempts: Annotated[int, _overwrite]
    generation_feedback: Annotated[str, _overwrite]
    error_message: Annotated[str | None, _overwrite]

class GradingState(AppState):
    exercise: Annotated[ExerciseSpec | None, _overwrite]
    submitted_code: Annotated[str, _overwrite]
    report: Annotated[TestReport | None, _overwrite]
    grade_summary: Annotated[str, _overwrite]
    issues: Annotated[list[FeedbackIssue], _overwrite]
    doc_queries: Annotated[list[str], _overwrite]

class ProgressState(AppState):
    user_id: Annotated[str, _overwrite]
    mistakes: Annotated[list[MistakeRecord], _overwrite]
    profile_block: Annotated[str, _overwrite]
    topic: Annotated[str | None, _overwrite]
    reason: Annotated[str, _overwrite]
    exercise_ids: Annotated[list[str], _overwrite]
    next_steps: Annotated[list[str], _overwrite]
```

- 三个 State 都继承 `AppState`，**不重定义** `messages/chunks/citations/usage/tool_rounds/answer`。
- 入口节点必须返回这五个键（哪怕全是空值），否则 `StateGraph` 首次执行会缺键。
- `MistakeRecord` 是 `db/models.Mistake` 的只读投影（frozen dataclass），
  不让 ORM 对象进检查点 —— 检查点不该依赖 SQLAlchemy 的 session 生命周期。

### 5.3 `agent/tools/grading.py`

**纯函数（离线可测）**：

```python
def compose_test_script(solution: str, tests_source: str) -> str: ...
def parse_test_report(result: SandboxResult) -> TestReport: ...
def score_of(report: TestReport) -> int: ...
def is_passed(report: TestReport) -> bool: ...
def render_feedback_block(issues: Sequence[FeedbackIssue]) -> str: ...
```

**结果脚注（逐字模板，`_RESULT_FOOTER`）**：

```python
if __name__ == "__main__":
    import json as _json
    import time as _time
    import unittest as _unittest

    class _PyassistResult(_unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.records = []
            self._started = {}

        def startTest(self, test):
            self._started[test.id()] = _time.perf_counter()
            super().startTest(test)

        def _record(self, test, status, message):
            started = self._started.pop(test.id(), None)
            elapsed = 0 if started is None else int((_time.perf_counter() - started) * 1000)
            self.records.append({
                "name": test.id().split(".")[-1],
                "status": status,
                "message": str(message)[-4000:],
                "duration_ms": elapsed,
            })

        def addSuccess(self, test):
            super().addSuccess(test)
            self._record(test, "passed", "")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            self._record(test, "failed", self._exc_info_to_string(err, test))

        def addError(self, test, err):
            super().addError(test, err)
            self._record(test, "error", self._exc_info_to_string(err, test))

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            self._record(test, "skipped", reason)

    _suite = _unittest.defaultTestLoader.loadTestsFromTestCase(ExerciseTests)
    _result = _PyassistResult()
    _started = _time.perf_counter()
    _suite.run(_result)
    _payload = {
        "total": _result.testsRun,
        "records": _result.records,
        "duration_ms": int((_time.perf_counter() - _started) * 1000),
    }
    print("__PYASSIST_RESULT__" + _json.dumps(_payload, ensure_ascii=False))
```

- 退出码永远 0：**不** `sys.exit(not wasSuccessful())`。失败由报告表达。
- `parse_test_report` 只认 stdout **最后一行**以标记开头的记录；找不到 / JSON 坏 / 沙箱状态非
  `"ok"` → `is_complete=False`、各计数为 0。
- `truncate_output` 保留尾部，所以标记不会因为输出过长而丢失。

**模型入口**：

```python
class ExerciseDraft(BaseModel):
    title: str
    prompt: str
    starter_code: str
    reference_solution: str
    tests: list[TestCaseDraft]

class GradeDraft(BaseModel):
    summary: str
    issues: list[FeedbackIssue]   # where / why / hint
    doc_queries: list[str]        # 1–3 条，用于确定性检索

async def generate_exercise(
    topic: str, *, attempt_feedback: str = "",
    model: Annotated[BaseChatModel | None, InjectedToolArg] = None,
) -> tuple[ExerciseDraft, UsageSnapshot]: ...

async def grade_code(
    code: str, *,
    exercise: Annotated[ExerciseSpec | None, InjectedToolArg] = None,
    report: Annotated[TestReport | None, InjectedToolArg] = None,
    model: Annotated[BaseChatModel | None, InjectedToolArg] = None,
) -> tuple[GradeDraft, UsageSnapshot]: ...
```

- 两者都用 `with_structured_output(..., include_raw=True)`（S5），返回 usage。
- 模型不可能触发 `exercise is None` / `model is None` 的分支：那是构图/注入错误，抛 `ConfigError`。
- `generate_exercise` 返回的是**草稿**，`exercise_id` 由服务层生成，不让模型编 id。
- 校验草稿：`tests` 条数 1..`MAX_TESTS_PER_EXERCISE`、方法名字符合法、
  `reference_solution` 非空、总长不超限；不合法直接 `LLMBadResponse(details={"stage": "generate"})`。

### 5.4 `agent/tools/sandbox_tools.py` 增量：`run_tests`

```python
async def run_tests(
    code: str, *,
    exercise: Annotated[ExerciseSpec | None, InjectedToolArg] = None,
    runner: Annotated[SandboxRunner | None, InjectedToolArg] = None,
    cache: Annotated[Mapping[str, SandboxResult] | None, InjectedToolArg] = None,
) -> tuple[str, TestReport]: ...
```

- 内部：`tests_source = "\n\n".join(t.source for t in exercise.tests)` →
  `compose_test_script(code, tests_source)` → `execute_cached(..., runner, cache)` →
  `parse_test_report(result)`。
- 返回 `(给模型/日志看的文本, TestReport)`；文本包含 total/passed/failed 与失败用例名，
  不把完整 traceback 塞给下一次模型调用（截断到 `max_output_chars` 以内）。
- **不新增超时旋钮**：沿用 `SANDBOX_LIMITS.timeout_seconds`；U4 若证明不够，
  在 `config` 加 `test_timeout_seconds` 并在本文回填，不在工具里硬写。

### 5.5 `agent/tools/progress.py`

```python
class NextStepDraft(BaseModel):
    topic: str
    reason: str
    exercise_ids: list[str]
    next_steps: list[str]

async def recommend_next(
    *, user_id: str,
    mistakes: Annotated[Sequence[MistakeRecord] | None, InjectedToolArg] = None,
    profile_block: Annotated[str, InjectedToolArg] = "",
    model: Annotated[BaseChatModel | None, InjectedToolArg] = None,
) -> tuple[NextStepDraft, UsageSnapshot]: ...
```

- 只做"候选过滤 + 调模型 + 基本形状校验"；**引用 id 的严格校验在图层**（D7），
  因为那里能同时拿到候选集与状态。
- 候选为空时抛 `RetrievalEmpty(query="mistakes")`？**不。** 没有错题是正常的新用户状态，
  由服务层直接返回"先做一组练习"的确定性推荐，不进模型（省钱且可测）。

### 5.6 `agent/graphs/exercise.py`

```
生成图（thread_id = exercise_id）
START ──▶ generate ──▶ self_check ──(通过)──▶ finalize ──▶ END
              ▲             │
              └──(未通过且 attempts < MAX)──┘
                            │
                            └──(达到上限)──▶ raise LLMBadResponse(stage="self_check")

批改图（thread_id = submission:<uuid>）
START ──▶ run_tests ──▶ grade ──▶ finalize ──▶ END
```

```python
@dataclass(frozen=True, slots=True)
class ExerciseGraphs:
    generation: CompiledStateGraph[Any, Any, Any, Any]
    grading: CompiledStateGraph[Any, Any, Any, Any]
    model_id: str

def build_exercise_graph(
    *, checkpointer: BaseCheckpointSaver[str] | None,
    model: BaseChatModel | None = None,
    runner: SandboxRunner | None = None,
) -> ExerciseGraphs: ...
```

- `runner is None` → `ConfigError`（与 `build_diagnose_graph` 一致）。
- `generate` 节点：`model = get_chat_model(tier=EXERCISE_MODEL_TIER)`，调 `generate_exercise`，
  组装 `ExerciseSpec`；`generation_attempts += 1`。
- `self_check` 节点：用**参考答案**跑 `run_tests`，把 `TestReport` 写进状态与
  `sandbox_results`（缓存键 = 参考答案 + 测试脚本）。不通过时把失败摘要写进
  `generation_feedback`（供下一次 generate 的 human 消息使用）。
- `finalize`（生成图）：只写 `error_message=None`；不落库（落库是服务层的事）。
- `run_tests`（批改图）：读 `exercise` 与 `submitted_code`，产出 `report`。
- `grade` 节点：调 `grade_code`；用 `report` + `doc_queries` 做确定性检索（最多 2 条 query，
  top-k=3），`build_citations` 生成引用；把 `issues / summary / citations` 写进状态。
- `finalize`（批改图）：写 `answer`（给日志/调试的可读摘要），不写库。
- 两张图都用 `get_stream_writer()` 发 `tool` 事件（`phase=start/end`, `name=run_tests`），
  复用阶段六的白名单，**不新增事件类型**。

### 5.7 `agent/graphs/progress.py`

```
START ──▶ collect ──▶ recommend ──▶ finalize ──▶ END
```

```python
@dataclass(frozen=True, slots=True)
class ProgressGraph:
    graph: CompiledStateGraph[Any, Any, Any, Any]
    model_id: str
    preflight: Callable[[], Awaitable[None]]

def build_progress_graph(
    *, checkpointer, model=None, search: Searcher | None = None,
    fetch: Fetcher | None = None, preflight=None,
) -> ProgressGraph: ...
```

- `collect`：把 `mistakes` 去重成候选（按 `exercise_id`），写 `profile_block`（来自记忆服务，
  由入口状态传入）；空候选直接短路到 `finalize`，不调模型。
- `recommend`：调 `recommend_next`，得草稿。
- `finalize`：严格校验 `exercise_ids`（D7），检索并生成 citations，追加"依据"块。
- 检索依赖用与 qa/diagnose **同一组** `Searcher/Fetcher/preflight`（`_resolve_deps` 直接 import，
  不复制）。无索引时 `preflight` 在路由层先失败，图不会跑到一半。

### 5.8 `services/exercise.py`

```python
@dataclass(frozen=True, slots=True)
class ExerciseResult:
    exercise: ExerciseSpec
    usage: UsageSnapshot

@dataclass(frozen=True, slots=True)
class SubmissionResult:
    exercise_id: str
    report: TestReport
    score: int
    is_passed: bool
    summary: str
    issues: list[FeedbackIssue]
    citations: list[Citation]
    usage: UsageSnapshot

async def create_exercise(
    *, topic: str, graph: CompiledStateGraph[Any, Any, Any, Any],
    exercise_id: str, request_id: str,
) -> ExerciseResult: ...

async def submit_exercise(
    *, exercise_id: str, code: str,
    graphs: ExerciseGraphs, session: AsyncSession,
    user_id: str, session_id: str, request_id: str,
) -> SubmissionResult: ...
```

- `create_exercise`：`ainvoke({"topic": topic, ...}, {"configurable": {"thread_id": exercise_id}})`
  → `aget_state` 取 `exercise`；没有 → `LLMBadResponse(stage="generate")`。
  生成图**不写库**。
- `submit_exercise` 的顺序（固定）：
  1. `await ensure_user(session, user_id=user_id)`；
  2. `await create_chat_session(session, session_id=db_session_id(session_id), user_id=user_id)`；
  3. 从生成图检查点取 `ExerciseSpec`；取不到 → `ExerciseNotFound`（404）；
  4. `assert_daily_quota` 已在路由依赖里做过，这里不重复；
  5. 跑批改图，thread_id = `f"{exercise_id}:{uuid4().hex}"`（每次提交独立；节点无副作用）；
  6. `record_attempt(idempotency_key=f"attempt:{exercise_id}:{sha256(code)[:16]}", ...)`；
     返回 `False` 表示重放，**结果仍照常返回**（结果由代码决定，重算无害）；
  7. 通过 → `mark_mistake_resolved(user_id, exercise_id)`；未通过 →
     `record_mistake(idempotency_key=f"mistake:{user_id}:{exercise_id}", ...)`；
  8. `update_profile(store, ...)`（失败不影响响应，只打 `memory.update_failed`）。
- **幂等键公式逐字固定**：
  - 提交：`attempt:{exercise_id}:{sha256(code).hexdigest()[:16]}`
  - 错题：`mistake:{user_id}:{exercise_id}`（每个用户每道题一行）
  依据：§2「节点必须幂等」；§5.4 没有要求"每次失败一行"。
- 所有 DB 调用都在 `repositories.py`，本文件不写 SQL。
- `session` 由路由的 `get_session` 依赖传入；本文件不 import fastapi。

### 5.9 `services/progress.py`

```python
@dataclass(frozen=True, slots=True)
class Recommendation:
    topic: str
    reason: str
    exercise_ids: list[str]
    next_steps: list[str]
    citations: list[Citation]
    usage: UsageSnapshot

async def recommend_for_user(
    *, user_id: str, graph: ProgressGraph, store: BaseStore,
    session: AsyncSession, request_id: str,
) -> Recommendation: ...
```

- 读 `list_mistakes(user_id, only_unresolved=True, limit=MISTAKE_LIST_LIMIT)`；
- 读 `load_profile(store, user_id)`，渲染成 `profile_block`；
- 候选为空 → 返回确定性推荐（`topic` 取四个主题里尝试最少的一个，
  `reason="你还没有未解决的错题"`，`exercise_ids=[]`，`citations=[]`），**不调用模型**；
- 有候选 → 跑图 → 校验 → 追加依据块。
- 日志：`progress.done`，字段 `user_id / candidate_count / cited_ids / latency_ms`。

### 5.10 `services/memory.py`

```python
@dataclass(frozen=True, slots=True)
class Profile:
    user_id: str
    topics: dict[str, TopicStat]
    unresolved_mistakes: int
    recent_exercise_ids: tuple[str, ...]
    updated_at: str

async def load_profile(store: BaseStore, user_id: str) -> Profile | None: ...
async def update_profile(
    store: BaseStore, *, user_id: str,
    mistakes: Sequence[MistakeRecord], attempts: Sequence[AttemptRecord],
) -> Profile: ...
def render_profile_block(profile: Profile | None) -> str: ...
```

- Store 键：`namespace=("profiles", user_id)`, `key="summary"`。
- `update_profile` 完全确定性：计数、最近 5 个 exercise_id、未解决数；`updated_at` 用 UTC ISO8601。
  **不调模型**（D6）。
- `render_profile_block` 输出固定格式的多行文本（≤ 800 字符），供 human 消息尾部使用；
  不包含时间戳以外的易变内容（时间戳只在 block 内，不进 system）。
- 失败只记日志：记忆是增强，不是主路径。

### 5.11 `services/usage.py`

```python
@dataclass(frozen=True, slots=True)
class QuotaStatus:
    limit: int | None
    used_today: int
    is_exhausted: bool

async def assert_daily_quota(
    session: AsyncSession, *, user_id: str, limit: int | None,
) -> None: ...   # 超限 raise QuotaExceeded(retry_after=到 UTC 次日 0 点的秒数)

async def record_usage(
    session: AsyncSession, *, request_id: str, session_id: str,
    model_id: str, usage: UsageSnapshot,
) -> bool: ...

async def quota_status(
    session: AsyncSession, *, user_id: str, limit: int | None,
) -> QuotaStatus: ...

def db_session_id(raw: str) -> str: ...   # D9 的 32 字符归一化
```

- `assert_daily_quota`：`sum(prompt_tokens + completion_tokens)` from `token_ledger`
  join `sessions` where `sessions.user_id == user_id` and `created_at >= 今日 UTC 00:00`。
  达到上限：`QuotaExceeded`，`retry_after` 是到次日 0 点的秒数（≥1）。
- `record_usage`：`append_token_usage(idempotency_key=f"req:{request_id}", ...)`。
- 配额是**软上限**（D8），README 要写清楚。
- 不缓存、不维护第二份计数器：唯一真相是账本表（`AGENTS.md` §2「持久化经 repositories」）。

### 5.12 `api/schemas.py` 增量（**只追加**）

```python
class ExerciseRequest(BaseModel):
    topic: Literal["list", "dict", "decorator", "async"]
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    session_id: str | None = Field(default=None, max_length=64)

class ExerciseData(BaseModel):
    exercise_id: str
    topic: Literal["list", "dict", "decorator", "async"]
    title: str
    prompt: str
    starter_code: str
    # 刻意没有 tests / reference_solution（D2 / 守护 #39）

class SubmitRequest(BaseModel):
    code: str = Field(min_length=1, max_length=8_000)
    session_id: str | None = Field(default=None, max_length=64)

class TestOutcomeData(BaseModel):
    name: str
    status: Literal["passed", "failed", "error", "skipped"]
    message: str
    duration_ms: int

class FeedbackItem(BaseModel):
    where: str
    why: str
    hint: str

class SubmissionData(BaseModel):
    exercise_id: str
    status: Literal["passed", "failed"]
    is_passed: bool
    score: int
    tests: list[TestOutcomeData]
    stdout: str
    stderr: str
    duration_ms: int
    summary: str
    feedback: list[FeedbackItem]
    citations: list[CitationData]

class HistoryItem(BaseModel):
    attempt_id: str
    exercise_id: str
    topic: str
    is_passed: bool
    score: int | None
    submitted_code: str
    stdout: str | None
    stderr: str | None
    duration_ms: int | None
    created_at: str

class TopicCount(BaseModel):
    topic: str
    count: int

class StatsData(BaseModel):
    today_prompt_tokens: int
    today_completion_tokens: int
    today_cached_tokens: int
    today_cost_usd: Decimal
    total_attempts: int
    passed_attempts: int
    pass_rate: float
    mistake_total: int
    unresolved_mistakes: int
    mistakes_by_topic: list[TopicCount]
    quota_limit: int | None
    quota_used: int
    quota_remaining: int | None
    is_exhausted: bool

class RecommendRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)

class RecommendationData(BaseModel):
    topic: str
    reason: str
    exercise_ids: list[str]
    next_steps: list[str]
    citations: list[CitationData]
```

- `HealthData` 追加 `sandbox_ok: bool | None`（默认 `None`）。
- 全部是追加：既有字段一个不改、`API_VERSION` 保持 `v1`（`AGENTS.md` §6 第 3 条）。
- `SubmissionData.status` 只有 `passed/failed`：沙箱基础设施故障仍走 `SandboxError`（500），
  "用户代码在顶层崩了"是 `failed` + `tests[].status="error"`，不是服务错误（§3.2）。

### 5.13 四个 router

**`exercises.py`**

```python
router = APIRouter(dependencies=[Depends(require_quota)])

@router.post("/exercises", response_model=Envelope[ExerciseData])
async def create_exercise_endpoint(...): ...

@router.post("/exercises/{exercise_id}/submit", response_model=Envelope[SubmissionData])
async def submit_exercise_endpoint(...): ...
```

- `exercise_id` 由路由生成：`"ex_" + uuid4().hex`（35 字符，`mistakes.exercise_id` 是 String(64)）。
- `submit` 的 `session_id` 缺省：`request_id`；经 `db_session_id` 归一到 32 字符后再落库。
- 响应 200；不 `raise HTTPException`；`ExerciseNotFound` / `QuotaExceeded` 由 `api/errors.py` 翻译。
- 日志：`exercise.created`（`exercise_id / topic / attempts / latency_ms`）、
  `exercise.submitted`（`exercise_id / is_passed / score / passed / total / latency_ms`），
  **不记完整代码**（只记 `sha256` 前 16 位）。

**`history.py`**

```python
@router.get("/history", response_model=Envelope[Page[HistoryItem]])
async def history(
    request: Request,
    session_id: str | None = Query(default=None, max_length=64),
    user_id: str = Query(default=DEFAULT_USER_ID, max_length=64),
    limit: int = Query(default=HISTORY_PAGE_SIZE, ge=1, le=MAX_HISTORY_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=128),
) -> Envelope[Page[HistoryItem]]: ...
```

- `cursor` 是不透明字符串：`f"{created_at.isoformat()}|{id}"`；下一页用
  `(created_at, id) < cursor` 的**稳定排序**（时间相同用 id 兜底）。
- `total` 是该筛选条件下的总数，不是本页条数（`Page` 的契约含义）。

**`stats.py`**

- `GET /stats?user_id=local`；只读三张业务表，**不调模型**；`meta.usage` 为零值。
- `pass_rate` 在 `total_attempts == 0` 时为 `0.0`，不返回 `NaN`（JSON 不接受 NaN）。

**`progress.py`**

```python
router = APIRouter(dependencies=[Depends(require_quota)])

@router.post("/recommend", response_model=Envelope[RecommendationData])
async def recommend(...): ...
```

- **`AGENTS.md` §2 没有这个 router**：见 §9.1 与 §6 的契约增补流程。
- 请求体只有 `session_id`（用户固定）；服务端从库读错题，不让客户端传"我的错题"。

### 5.14 `db/repositories.py` 增量

全部是 `select()`（2.0 风格），仍然只有本文件写 SQL：

```python
async def list_mistakes(
    session, *, user_id: str, only_unresolved: bool = True, limit: int = 50,
) -> Sequence[Mistake]: ...

async def mark_mistake_resolved(
    session, *, user_id: str, exercise_id: str,
) -> bool: ...

async def list_attempts_page(
    session, *, user_id: str | None = None, session_id: str | None = None,
    limit: int = 20, cursor: tuple[datetime, str] | None = None,
) -> tuple[Sequence[ExerciseAttempt], bool]: ...   # (本页, 是否还有下一页)

async def count_attempts_for(session, *, user_id: str | None, session_id: str | None) -> int: ...

async def sum_tokens_since(
    session, *, user_id: str, since: datetime,
) -> tuple[int, int, int, Decimal]: ...   # prompt / completion / cached / cost

async def get_stats(
    session, *, user_id: str, since: datetime,
) -> StatsAggregate: ...   # frozen dataclass，服务层不拼 SQL
```

- `list_attempts_page` 的 `user_id` 通过 `sessions` 关联；两个筛选条件同时给时以 `session_id` 为准。
- `StatsAggregate` 是 `db/repositories.py` 里定义的 frozen dataclass（不是 ORM 行）。
- **不改表结构**：`mistakes.is_resolved` 已有，`exercise_attempts.created_at` 已有索引条件
  （`ix_exercise_attempts_session_id`）；用户级查询走 `sessions.user_id` 索引。

### 5.15 `frontend/`

**`api_client.py`**

```python
class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None: ...

class ApiClient:
    def __init__(self, base_url: str, timeout: float = 600.0) -> None: ...
    def health(self) -> dict[str, Any]: ...
    def chat_stream(self, question: str, session_id: str) -> Iterator[ChatEvent]: ...
    def ask(self, question: str, session_id: str) -> QAResultDict: ...
    def diagnose(self, error_text: str, code: str | None, session_id: str) -> dict[str, Any]: ...
    def create_exercise(self, topic: str, session_id: str) -> dict[str, Any]: ...
    def submit(self, exercise_id: str, code: str, session_id: str) -> dict[str, Any]: ...
    def history(self, session_id: str, limit: int = 20, cursor: str | None = None) -> dict: ...
    def stats(self, user_id: str = "local") -> dict[str, Any]: ...
    def recommend(self, session_id: str) -> dict[str, Any]: ...
```

- `chat_stream` 用 `httpx.Client.stream("POST", ...)`，逐行解析：
  ```
  event: token
  data: {"text": "..."}
  ```
  生成 `ChatEvent(type, payload)`；`done` 之后停止迭代；非 200 时先读完整信封再抛 `ApiError`。
- **不 import `pyassist`**，不复制 `api/schemas.py` 的模型（dict 即可，前端不做服务端校验）。

**`streamlit_app.py`**

| 页 | 内容 | 依赖端点 |
|---|---|---|
| 问答 | 输入框 → 逐字流 → 引用链接 | `POST /chat` |
| 诊断 | 报错文本 + 可选代码 → 结果卡片（解释/修复/沙箱输出） | `POST /diagnose` |
| 练习 | 选主题 → 出题 → 代码编辑 → 提交 → 用例结果 + 反馈 + 引用 | `POST /exercises`、`.../submit` |
| 历史与进度 | 提交列表（分页）+ 推荐卡片 | `GET /history`、`POST /recommend` |
| 统计 | 今日用量/成本、通过率、错题分布、配额 | `GET /stats` |

- `session_id` 首次生成 `uuid4().hex`，写进 `st.query_params["sid"]`；刷新后读回。
- 服务端不可达时显示"API 不可用 + 地址"，不抛裸异常。
- `st.set_page_config(page_title="Python 学习助手", layout="wide")`；
  代码编辑用 `st.text_area(height=320)`，不引入第三方编辑器。
- 流式：`st.write_stream(gen)`，`gen` 逐块 yield token；引用在流结束后渲染。

### 5.16 镜像与部署

**`Dockerfile.api`**

```dockerfile
FROM python:3.12-slim AS base
# uv 安装（复制官方静态二进制），非 root 用户 10001
# uv sync --frozen --no-dev
# ENV HF_HOME=/app/data/hf  PYTHONUNBUFFERED=1
# COPY src/ /app/src/  pyproject.toml uv.lock /app/
# CMD ["uv", "run", "uvicorn", "pyassist.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- 非 root；不安装 dev 依赖；`data/` 走卷，不 COPY。
- `sentence-transformers` 与 CPU torch 走 `pyproject` 已有的 pytorch-cpu 索引。
- healthcheck 用 `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"`，
  不依赖 curl。

**`Dockerfile.web`**

- 只装 web 所需：`streamlit` + `httpx`。用 `[dependency-groups] web` +
  `uv sync --only-group web --no-install-project`；**不装 torch/chroma**。
- `CMD ["streamlit", "run", "frontend/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]`。

**`deploy/prod.compose.yml`**

- `api`：挂 `./data:/app/data`、`/var/run/docker.sock:/var/run/docker.sock`、
  `./data/hf:/app/data/hf`；`depends_on` 无（sandbox 是宿主构建的镜像，不是服务）。
- `web`：`PYASSIST_API_BASE_URL=http://api:8000`。
- `caddy`：80/443，`Caddyfile` 反代 `web:8501` 与 `api:8000`（API 可选直接暴露 `/api/*`）。
- **沙箱镜像构建步骤写进 README**（宿主执行，compose 不负责）：
  `docker build -f src/pyassist/sandbox/Dockerfile.sandbox -t pyassist-sandbox:1 .`
- 开发用的 `docker-compose.yml` 同步补 `api` 的 socket 与 HF 卷，保持"本地与生产同形"。

**`deploy/Caddyfile`**

```caddyfile
{$DOMAIN} {
    reverse_proxy /api/* api:8000
    reverse_proxy * web:8501
    flush_interval -1
}
```

### 5.17 `README.md` / `.env.example`

`README.md` 必须有：架构图（ASCII，与 `AGENTS.md` §2 数据流一致）、环境变量清单、
一键启动（开发与生产两套）、`evals/` 跑分方法、已知限制（单人使用、软配额、沙箱需要 Docker）。

`.env.example` 追加：

```dotenv
# 单日 Token 熔断（留空 = 用 config.DEFAULT_DAILY_TOKEN_LIMIT；0 = 关闭，仅本地调试）
DAILY_TOKEN_LIMIT=

# 前端调用后端的地址（web 容器里是 http://api:8000）
PYASSIST_API_BASE_URL=http://127.0.0.1:8000
```

### 5.18 评估：`exercises.jsonl` / `metrics.py` / `run_v3_eval.py`

**数据集**（`src/pyassist/evals/datasets/exercises.jsonl`，8 条，**只增不改**）：

```json
{"id":"list-01","topic":"list","title":"...","prompt":"...","starter_code":"...","reference_solution":"...","tests":[{"name":"test_append","source":"def test_append(self):\n    ..."}]}
```

- 字段集合**恰好**：`{"id","topic","title","prompt","starter_code","reference_solution","tests"}`。
- 每主题 2 条；测试只用 `unittest` 与标准库；参考答案必须能在沙箱里通过自己的测试。
- 采集方式：人工编写 + `-m docker` 验证；**禁止**用模型生成后直接入集（评估集会漂移）。

**`metrics.py` 增量（纯函数）**：

```python
def self_consistency_rate(passed: Sequence[bool]) -> float: ...
def grading_agreement(scores: Sequence[int], human_scores: Sequence[int], tolerance: int = 10) -> float: ...
```

- `grading_agreement` 的判定：`abs(score - human) <= tolerance` 记为一致；分母是全部样本。

**`run_v3_eval.py`（四种模式）**：

```bash
uv run python -m pyassist.evals.run_v3_eval --self-consistency   # 夹具参考答案过自身测试（需 Docker）
uv run python -m pyassist.evals.run_v3_eval --generate --per-topic 5  # 真实生成 + 自校验（需 key+Docker）
uv run python -m pyassist.evals.run_v3_eval --export-labels 30   # 导出匿名样本 + 空白标注 CSV
uv run python -m pyassist.evals.run_v3_eval --agreement labels.csv # 计算一致率
```

- `--self-consistency` 输出一行：`records=8 self_consistency=1.000 timeouts=n`；
- `--generate` 输出：`records=20 self_consistency=0.xxx failed=n`；
- `--export-labels` 写 `docs/evals/阶段八-标注样本-<日期>.csv`（**不进 git**，`.gitignore` 加一行），
  字段 `sample_id, prompt, submitted_code, test_summary, model_score, human_score(空)`；
- `--agreement` 读人工填好的 CSV，输出 `samples=30 agreement=0.xxx`；
- 需要硬件/密钥的条件不满足时以**退出码 2** 结束并打印缺什么（照抄阶段七 `run_v2_eval`）。

### 5.19 测试矩阵

| 文件 | 覆盖 | 离线？ |
|---|---|---|
| `test_grading_contract.py` | 拼装顺序、标记解析、坏 JSON、非零退出、截断后仍在尾部、分数与通过判定、空报告 | 是 |
| `test_exercise_graph.py` | 生成 happy path、自校验失败重试、达到上限报 502、批改通过/失败、`reasoning_content` 不被重建 | 是 |
| `test_exercise_api.py` | 信封、**响应无 tests/reference**、422、404、配额 429 | 是 |
| `test_progress_graph.py` | 非法 id 剥掉、空候选不调模型、引用来自 `build_citations` | 是 |
| `test_progress_api.py` | 信封、无错题时的确定性推荐 | 是 |
| `test_history_api.py` | 分页游标、排序稳定、跨会话、`Page` 形状 | 是 |
| `test_stats_api.py` | 与固定账本一致、`pass_rate` 边界、错题分布 | 是 |
| `test_usage_quota.py` | 429 + `Retry-After`、账本幂等、超限后模型未被调用（脚本模型调用次数断言） | 是 |
| `test_memory.py` | Store 读写、确定性渲染、无第二个存储 | 是 |
| `test_frontend_contract.py` | 无 `import pyassist`、客户端路径 ⊆ `app.openapi()["paths"]` | 是 |
| `test_exercise_eval.py` | 数据集 schema、指标纯函数 | 是 |
| `test_exercise_self_consistency.py` | 8 条夹具的参考答案过自身测试 | `-m docker` |
| `test_contract_guardrails.py` | #38–#45 | 是 |

**脚本模型替身**：`tests/fakes/llm.py` 的 `ScriptedChatModel` 增加 `with_structured_output` 的脚本队列
（按调用顺序返回 `ExerciseDraft` / `GradeDraft` / `NextStepDraft`，并附带伪造的 `AIMessage.usage_metadata`），
否则结构化节点在测试里拿不到 usage（S5）。

### 5.20 契约守护 #38–#45

| # | 断言 | 防的是什么 |
|---|---|---|
| #38 | `frontend/**` 的 AST 里没有 `import pyassist` / `from pyassist` | 前端绕开 HTTP（§2/§4） |
| #39 | `ExerciseData` 的字段集合**恰好**是 `{exercise_id, topic, title, prompt, starter_code}` | 测试或参考答案泄露给客户端（D2） |
| #40 | 分数与通过判定的实现只出现在 `agent/tools/grading.py` | 第二套评分逻辑（D5） |
| #41 | `TEST_RESULT_MARKER` 只出现在 `grading.py` 与 `tests/` | 第二个结果协议 |
| #42 | 每个会调用模型的 router 模块都构造了带 `require_quota` 的 `APIRouter` | 新路由漏挂配额（D8） |
| #43 | `append_token_usage(` 的调用点白名单只有 `services/usage.py` | 账本被旁路写入 |
| #44 | 前端 `api_client.py` 里的路径字符串 ⊆ `create_app().openapi()["paths"]` | 契约漂移（D11） |
| #45 | `db/models.py` 的 `__tablename__` 集合冻结为现有五张表 | 偷偷加表（§1.2） |

- #44 是运行时测试（导入 app、读 OpenAPI），不是 grep；它同时证明路由真的挂上了。
- 新守护一律写进 `tests/test_contract_guardrails.py`，编号接在 #37 后面。

---

## 6. 任务分解

| 任务 | 内容 | 产物 | 预估 |
|---|---|---|---|
| **T0** | 归档阶段七 SPEC（`cmp` 通过）+ **补阶段七遗留验收**（Docker daemon、镜像、`-m docker` 10 条、v2 跑分）+ U2–U5 spike | 归档件 + `docs/evals/阶段七-跑分.md` 数字 + §2.3 回填 | **大（阻塞）** |
| T1 | `config.py` 常量 + `state.py` 三个 State + `errors.py` 两个异常 + `api/errors.py` 映射 + `repositories.py` 六个查询 | 5 文件 | 中 |
| T2 | `grading.py` 的纯函数（拼装/解析/分数/校验）+ `test_grading_contract.py` | 2 文件 | 中 |
| T3 | `sandbox_tools.run_tests` + `registry.py` 注册四个工具 + `test_registry` 增量 | 2 文件 | 小 |
| T4 | 生成图（`generate ⇄ self_check`）+ 两份生成 prompt + `test_exercise_graph`（生成部分） | 4 文件 | **大** |
| T5 | 批改图 + `services/exercise.py` + 落库/错题/记忆调用 + 图测试（批改部分） | 4 文件 | **大** |
| T6 | `schemas.py` v3 模型 + `routers/exercises.py` + `deps.py` / `main.py` 接线 + `test_exercise_api` | 5 文件 | 中 |
| T7 | `services/usage.py` + `require_quota` + 四个 LLM 路由接入 + `routers/stats.py` + 测试 | 6 文件 | 中 |
| T8 | `repositories` 分页 + `routers/history.py` + `test_history_api` | 3 文件 | 中 |
| T9 | v4：`tools/progress.py` + `graphs/progress.py` + `services/progress.py` + `routers/progress.py` + 测试 | 6 文件 | **大** |
| T10 | `services/memory.py` + `tests/fakes/store.py` + lifespan 打开 Store + `test_memory` | 4 文件 | 中 |
| T11 | `frontend/api_client.py` + `streamlit_app.py` + `test_frontend_contract` | 3 文件 | **大** |
| T12 | `Dockerfile.api` / `Dockerfile.web` / `deploy/*` / 开发 compose 增量 | 6 文件 | 中 |
| T13 | `README.md` + `deploy/README.md`（威胁模型、发布清单、演示脚本） | 2 文档 | 中 |
| T14 | `exercises.jsonl`（8 条）+ `metrics.py` + `run_v3_eval.py` + `test_exercise_eval` + 跑分文档 | 5 文件 | 中 |
| T15 | 守护 #38–#45 + `test_exercise_self_consistency` + 全量回归 | 2 文件 + 回归 | 中 |
| T16 | 收尾：`AGENTS.md` 版本回填 / ADR 0003 / 0004 / 演示分镜 / `scripts/prune_threads.py` | 5 文件 | 小 |

**硬顺序**：T0 → T1 → T2 → T3 → T4 → T5 → T6 →（T7/T8 可并行）→ T9 → T10 → T11 → T12/T13 → T14 → T15 → T16。
**T4 / T5 / T9 / T11 是本阶段的主要工作量；T0 不完成，T5 之后的验收一律只能标"未验证"。**

---

## 7. 验收

### 7.1 命令

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src/pyassist
uv run pytest                                   # 默认 -m 'not docker'，全程不触网

# 阶段八专属（离线）
uv run pytest tests/test_grading_contract.py tests/test_exercise_graph.py \
              tests/test_exercise_api.py tests/test_progress_graph.py \
              tests/test_progress_api.py tests/test_history_api.py \
              tests/test_stats_api.py tests/test_usage_quota.py \
              tests/test_memory.py tests/test_frontend_contract.py \
              tests/test_exercise_eval.py -v

# 既有回归（必须仍全绿）
uv run pytest tests/test_deepseek_reasoning.py tests/test_contract_guardrails.py \
              tests/test_state_ownership.py tests/test_sse.py tests/test_graph_qa.py \
              tests/test_graph_diagnose.py -v

# 真实隔离与夹具自洽（需 Docker Desktop 已启动）
docker build -f src/pyassist/sandbox/Dockerfile.sandbox -t pyassist-sandbox:1 .
uv run pytest -m docker tests/test_sandbox_docker.py tests/test_exercise_self_consistency.py -v

# 手动：真实生成自洽（需 API key + 索引 + Docker；非峰值）
uv run python -m pyassist.evals.run_v3_eval --generate --per-topic 5

# 手动：评分一致率（先导出，人工填 human_score，再算）
uv run python -m pyassist.evals.run_v3_eval --export-labels 30
uv run python -m pyassist.evals.run_v3_eval --agreement docs/evals/阶段八-标注样本-2026-10-22.csv

# 手动：端到端（API + UI）
uv run uvicorn pyassist.api.main:app --reload
uv run streamlit run frontend/streamlit_app.py

# 部署
docker compose -f deploy/prod.compose.yml config
docker compose -f deploy/prod.compose.yml up -d --build
curl -s http://127.0.0.1:8000/health | python -m json.tool
```

### 7.2 判据

| # | 判据 | 由什么保证 |
|---|---|---|
| 1 | `POST /exercises` 响应**不含** `tests` / `reference_solution` | 守护 #39 + `test_exercise_api` |
| 2 | 生成自校验失败会重试；达 `MAX_EXERCISE_ATTEMPTS` 返回 502 信封而非坏题 | `test_exercise_graph` ② |
| 3 | `is_passed` 只由测试报告决定；模型输出里没有分数字段 | `test_grading_contract` + 守护 #40 |
| 4 | 同一 `(exercise_id, code)` 重放不新增 attempt / mistake | `test_usage_quota` + `test_exercise_api` |
| 5 | 后续通过会 `is_resolved=True` | `test_exercise_api` / `test_history_api` |
| 6 | `GET /history` 游标分页、`Page` 形状、刷新可回放 | `test_history_api` |
| 7 | `GET /stats` 与固定账本完全一致；`pass_rate` 无 `NaN` | `test_stats_api` |
| 8 | 配额耗尽 → 429 + `RATE_LIMITED` + `Retry-After`，且模型调用次数为 0 | `test_usage_quota` |
| 9 | 推荐的每个 `exercise_ids` 都在该用户错题集里 | `test_progress_graph` + 判据 9 的断言 |
| 10 | 推荐带至少 1 条文档链接（来自 `build_citations`） | `test_progress_api` |
| 11 | 前端无 `pyassist` import；客户端路径 ⊆ OpenAPI | 守护 #38 / #44 |
| 12 | `/chat` 仍逐字 SSE，`done` 带 usage；记账在流结束时发生一次 | `test_sse` 复跑 + `test_usage_quota` |
| 13 | `mypy --strict` / ruff / 离线 `pytest` 全绿，无 `type: ignore` | 全量命令 |
| 14 | 未新增表；既有 schema 字段未改；`API_VERSION == "v1"` | 守护 #45 + `test_envelope` |
| 15 | 夹具参考答案在真实沙箱里 100% 通过 | `-m docker` 的 `test_exercise_self_consistency` |
| 16 | 真实生成的 20 道题自洽率达标（100%） | 手动 `--generate`，写进跑分文档 |
| 17 | 30 份人工标注一致率 ≥ 80% | 手动导出/标注/`--agreement`，写进跑分文档 |
| 18 | 干净环境 `compose up` 可起，公网可访问，3 分钟演示 | **人工**；未做就写"未验证" |

---

## 8. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | **阶段七的隔离验收仍缺**（本阶段最高风险） | T0 补齐；未补齐前，v3 的"沙箱通过"只能标未验证；`-m docker` 与默认 `pytest` 继续分离 |
| R2 | 模型生成的测试本身是错的，导致"参考答案也过不了" | `self_check` 回环 + 上限 2 次；仍失败返回 502，**不下发坏题**；日志记录每次失败原因 |
| R3 | 单文件拼接与用户 `if __name__ == "__main__"` 冲突 | prompt 明令禁止；`self_check` 会暴露；U3 实测后把结论写进本文 |
| R4 | 评分作弊 / 分数漂移 | 分数由测试确定性计算（D5）；模型只给反馈；`GradeDraft` 无分数字段 |
| R5 | 检查点膨胀（题目 + 测试 + 提交） | 测试条数/长度上限（`MAX_TESTS_PER_EXERCISE` / `MAX_TEST_METHOD_CHARS`）；提交代码 ≤8k；报告只留失败消息尾段 |
| R6 | 生产环境 `docker.sock` 暴露 = 宿主 root | `deploy/README.md` 写威胁模型；api 不执行用户输入；沙箱参数固定；禁止合并容器 |
| R7 | api 镜像过大 / 首次下载 embedding 模型失败 | CPU torch 索引；`HF_HOME` 卷；部署前在宿主预下载；U6 记录实际体积 |
| R8 | Caddy/Streamlit 缓冲 SSE | `flush_interval -1` + 响应头 `X-Accel-Buffering: no`；U8 实测 |
| R9 | 配额并发竞态 | 明说软上限；部署层 slowapi 兜底；日志 `quota.soft_exceeded` 可观测 |
| R10 | 前端与服务端契约漂移 | 守护 #44 运行时比对 OpenAPI；`test_envelope` 冻结既有字段 |
| R11 | 启动模型校验拖慢/阻塞启动 | `asyncio.create_task` + 15s 超时；失败只降级 `/health`；shutdown 取消 |
| R12 | 人工标注与公网验收无法由 Agent 完成 | 提供导出脚本、CSV 模板、发布清单；结果栏留空并标"未验证"，**不编数字** |

---

## 9. 与 ROADMAP / 阶段七的差异（均需知悉）

1. **新增 `api/routers/progress.py` 与 `POST /recommend`**。`AGENTS.md` §2 的路由清单没有它，
   但 v4 的 UI 必须有一个 HTTP 入口消费 `recommend_next`；没有端点，这个工具只能存在于测试里。
   需按 §6 增补契约（新增 router 不是破坏性变更）并留 ADR 0003。
2. **新增 `services/usage.py`**。`AGENTS.md` §2 的单点所有权把"统计 Token / 成本"划给
   `llm/tracing.py`，但那条约束的是**计价**；把账本写进 `tracing.py` 会让领域层依赖 DB。
   配额检查与账本写入是一个跨版本业务动作，放在 `services/` 下，仍只有一处。
3. **`NOT_FOUND` / `RATE_LIMITED` 扩展到业务侧**。不新增枚举值（`AGENTS.md` §6「新增枚举值不是
   破坏性变更」在这里甚至用不上），但 §3.2 的注释需要补一句语义；两个异常类进 `_MAPPING`。
4. **`/health` 增 `sandbox_ok`**。阶段七 §10.4 明确"阶段八随部署一起增补"；本阶段兑现。
   属追加字段，`API_VERSION` 不变。
5. **启动模型名校验改为异步**。`AGENTS.md` §1 要求"启动时调一次"，同步会阻塞启动；
   后台任务 + 超时是等价实现，写 ADR。
6. **`run_tests` 使用 `unittest` 而非 pytest**。沙箱镜像只有标准库（S1）；引入 pytest 会扩大
   安全边界镜像，且 `AGENTS.md` §4 要求镜像改动人工 review —— 不值得。
7. **两张 exercise 图同文件**（D3）。`AGENTS.md` §2 只规定"图由我们自己拼节点"，
   没规定一图一文件；共享拼装函数使它们留在同一个所有者下。
8. **不做 `/diagnose` 的 SSE**。阶段七 D8 的延续；UI 用 spinner，只有 `/chat` 要求逐字。
9. **不加迁移**。现有五张表足够（F4）；"为完整性造表"会让双源划分重新变糊。
10. **部署选 VM + Caddy，而不是 ROADMAP 举例的 Railway / Fly**。沙箱需要宿主 Docker，
    PaaS 不提供 Docker-in-Docker；这不是偏好，是能力约束。
11. **新增守护 #38–#45**。ROADMAP 未列；它们把本阶段最容易复发的四类问题（泄露测试、
    第二套评分、漏挂配额、前端绕开 HTTP）钉成测试。
12. **评估新增 `exercises.jsonl` 与 `run_v3_eval.py`**。§5.4 的两个百分比总得有人算；
    算术放 `metrics.py`，I/O 放 CLI，才能在 CI 里离线测。

---

## 10. 待定项（每条给推荐值，均不阻塞动工）

1. **部署目标**：推荐**单台 Linux VM + compose + Caddy**。替代：自建 K8s（本阶段不推荐，
   为一个学习应用引入集群运维没有收益）。
2. **用户身份**：推荐**固定 `local` + 客户端 session**。替代：请求体传 `user_id` —— 那会把
   "谁是用户"交给调用方，且没有鉴权时毫无意义。
3. **单日额度**：推荐 **500,000 token/日**（`DEFAULT_DAILY_TOKEN_LIMIT`）。按 `PRICE_TABLE`
   粗算约 $0.1–1/日；人工可改，改配置不改代码。
4. **评分公式**：推荐**测试通过率**（D5）。替代：模型 0–100 打分 —— 不可复现，
   同一份代码两次提交会给出两个分数。
5. **生成模型档**：推荐 **pro**。自校验失败率高时可临时降 flash，但要在跑分文档记录，
   且不得因此放宽 `MAX_EXERCISE_ATTEMPTS`。
6. **错题粒度**：推荐**每个 `(user, exercise)` 一行**。替代：每次失败一行 —— 会让"未解决数"
   变成"失败次数"，推荐会反复指向同一道题。
7. **未知练习的状态码**：推荐 **404 + `NOT_FOUND`**。替代：200 + `status="not_found"`
   —— 与阶段七"用户代码失败是结果"的语义不同，这是**调用方错误**，应该 4xx。
8. **演示录像与公网地址**：**人工完成**。Agent 交付分镜脚本、发布清单、`deploy/` 配置，
   并在文档里保留"未验证"字样，直到有人真的访问过。
9. **Telegram 适配器**：**不在本阶段**（ROADMAP 待定项 6），前端只做 Streamlit。
10. **CI**：推荐新增 `.github/workflows/ci.yml`，只跑 ruff / mypy / 离线 `pytest`；
    `-m docker` 与真实跑分不进默认 CI（需要 daemon 与密钥）。没有 CI 也能交付，但回归靠人。
11. **练习检查点的保留期**：推荐**不自动清理**，提供 `scripts/prune_threads.py` 手动脚本
    （按 `created_at` 删除过期 thread）。自动清理需要与业务表对齐，风险大于收益。
12. **记忆是否用模型做摘要**：推荐**不用**（确定性聚合，D6）。替代：flash 摘要 ——
    多一次调用、多一份不确定性，而画像内容本来就是可数的。

---

## 11. 收尾与下一阶段

### 11.1 本阶段收尾（T16）

- `AGENTS.md` 末尾"实际锁定版本"用 `uv pip list` 回填（契约预留动作，按 §6 先说明）。
- `AGENTS.md` §2 的目录树补 `api/routers/progress.py`、`services/usage.py`（随契约增补一起）。
- ADR：
  - `docs/decisions/0003-阶段八契约增补.md`：新增 router、`services/usage.py`、
    `NOT_FOUND`/`RATE_LIMITED` 的业务用法、`/health.sandbox_ok`；
  - `docs/decisions/0004-部署形态与沙箱边界.md`：D12 与 docker.sock 威胁模型。
- `README.md` 的"演示脚本"一节：3 分钟分镜（提问 → 引用 → 报错 → 沙箱 → 做题 → 批改 → 推荐 → 统计）。
- 发布清单（`deploy/README.md`）：
  1. VM 装 Docker + compose；
  2. `docker build -f src/pyassist/sandbox/Dockerfile.sandbox -t pyassist-sandbox:1 .`
  3. 准备 `.env`（key / domain / 额度）；
  4. `docker compose -f deploy/prod.compose.yml up -d --build`；
  5. `/health` 为 `ok`；6. 公网访问 UI；7. 录演示。

### 11.2 明确不在本阶段承诺的事

- 多用户/鉴权、Telegram、Qdrant、`/diagnose` SSE、自动清理检查点、硬配额。
- 任何"公网已上线"的说法，除非有人真的从外部访问过并在跑分文档里记录时间与地址。

### 11.3 下一阶段的候选（按收益排序）

1. 鉴权与会话归属（把 `user_id` 从固定值换成真实用户）；
2. 硬配额（在 DB 事务里预留额度，而不是读后写）；
3. `/diagnose` 流式化（UI 的等待可感知性）；
4. Qdrant 切换（只改 `rag/store.py`）；
5. Telegram 适配器（只消费 HTTP API）。
