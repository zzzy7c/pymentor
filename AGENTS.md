# AGENTS.md — Python AI 学习助手

本文件是本仓库的**实现契约**。任何 Agent（人或 AI）在改动代码前必须先读本文件。
与 `ai-clever-bubble.md`（学习路线）冲突时，以本文件为准 —— 路线图描述"学什么"，本文件描述"怎么建"。

---

## 0. 项目定位

一个帮你学 Python 的 AI 应用，四个版本递进交付：

| 版本 | 能力 | 核心工具 | 完成标志 |
|------|------|----------|----------|
| v1 | 文档问答 + 引用 | `search_docs` / `read_doc` | 基于 Python 官方文档与 PEP 的 RAG，答案必带来源链接 |
| v2 | 错误诊断 + 代码执行 | `explain_error` / `run_code` | 用户贴报错 → 检索文档 → 解释 → 修复 → 沙箱验证通过 |
| v3 | 练习生成 + 自动批改 | `generate_exercise` / `run_tests` / `grade_code` | 按主题出题，跑测试用例，给评分与反馈 |
| v4 | 学习路径 + Web UI | `recommend_next` | 根据错题记录推荐下一步，Web 界面可用 |

**产品原则**：宁可回答"文档里没有"，也不要编造 API。所有解释必须可溯源。

---

## 1. 技术栈

### 语言与运行时

| 项 | 值 | 说明 |
|----|-----|------|
| 语言 | Python **3.12+** | 本机为 3.12.7。低于 3.12 不保证可用（依赖 `tomllib`、新版 typing 语法） |
| 包管理 | `pyproject.toml` + `uv`（回退 `pip`） | 禁止提交 `requirements.txt`，单一来源 |
| 环境 | `venv` | 禁止把 `.env` 和虚拟环境目录提交进仓库 |

### 框架与依赖

**版本策略**：本表锁定大版本与选型理由；精确版本由 `pyproject.toml` 唯一决定。
安装后用 `uv pip list` 把实际版本回填到本节末尾的"实际锁定版本"一行。

| 依赖 | 版本约束 | 用途 | 为什么是它 |
|------|----------|------|------------|
| `langgraph` | `>=1.1,<2.0` | **Agent 编排框架** | 图状态机 + 检查点持久化 + 流式事件 |
| `langchain-core` | `>=1.3,<2.0` | 消息 / 工具 / 提示词基础类型 | langgraph 的依赖，同时提供 `@tool` 与 `ChatPromptTemplate` |
| `langchain-deepseek` | `>=1.0` | DeepSeek Chat 模型封装 | 官方集成包。**有已知缺陷，见「LangChain 集成缺陷」** |
| `langgraph-checkpoint-sqlite` | `>=2.0,<3.0` | 开发环境检查点 | 指向同一个 `data/app.db` |
| `langgraph-checkpoint-postgres` | `>=2.0,<3.0` | 生产环境检查点 | 指向同一 PG 实例 |
| `openai` | `>=1.50,<2.0` | **仅用于**启动时模型名校验与底层传输 | 业务代码不得直接调用，见 §2 单点所有权 |
| `fastapi` | `>=0.115,<1.0` | 后端 API | 异步、类型安全、自动 OpenAPI |
| `uvicorn[standard]` | `>=0.30` | ASGI 服务器 | — |
| `pydantic` | `>=2.9,<3.0` | 数据校验 + 结构化输出 | v2 是 FastAPI、OpenAI SDK 与 settings 的共同基础 |
| `pydantic-settings` | `>=2.4,<3.0` | 配置加载 | 与 pydantic v2 同源 |
| `sqlalchemy` | `>=2.0,<3.0` | ORM | 2.0 风格（`select()` 而非 legacy Query） |
| `alembic` | `>=1.13` | 数据库迁移 | 禁止手改表结构 |
| `chromadb` | `>=0.5` | 向量库（开发/入门） | 零配置、本地持久化。生产切 Qdrant 时只换 `rag/store.py` |
| `streamlit` | `>=1.57` | 前端 | 纯 Python，无 JS 工具链 |
| `httpx` | `>=0.27` | HTTP 客户端（前端调后端、语料抓取） | — |
| `sse-starlette` | `>=2.1` | SSE 流式响应 | 阶段七要求 |
| `structlog` | `>=24.1` | 结构化日志 | JSON 日志，便于检索 |
| `slowapi` | `>=0.1.9` | 速率限制 | 成本控制 |
| `jinja2` | `>=3.1` | Prompt 模板 | 禁止用 f-string 拼 prompt |
| `python-dotenv` | `>=1.0` | 本地密钥加载 | — |
| `docker` | `>=7.1` | 沙箱容器编排 | v2/v3 代码执行 |
| `pytest` / `pytest-asyncio` / `respx` | 最新 | 测试 | 测试必须能离线跑（mock LLM） |

**框架选型：LangGraph**（用户指定）。它承担三件事：Agent 循环、状态持久化、流式事件。
`langchain-core` 作为其基础库一并引入。

**仍不引入**：

| 不引入 | 理由 |
|--------|------|
| LangChain 的预置 Agent（`create_react_agent` / `create_agent`） | 会隐藏工具循环细节。v2/v3 的核心恰恰是"工具循环 + 沙箱验证"，每个节点必须能在图里显式控制 |
| LlamaIndex | 检索是本项目的评估对象（Recall@5、引用准确率），必须自己可控可测 |
| Gradio | 前端已定 Streamlit，不引入第二个 UI 框架 |

**用 LangGraph 的画布，不用 LangChain 的成品 Agent。** 图由我们自己拼节点。

### LLM 供应商与模型

**供应商：DeepSeek**（唯一）。接口地址 `https://api.deepseek.com`，密钥变量 `DEEPSEEK_API_KEY`。
协议为 **OpenAI 兼容**，因此统一用 `openai` SDK + `base_url` 覆写，不要直接写 HTTP。

#### 已停用的模型名（写进代码就会 400）

`deepseek-chat` 与 `deepseek-reasoner` 已于 **2026-07-24** 停用，**无宽限期**。
这两个字符串出现在代码、`.env`、CI 变量、docker-compose 里都会让请求直接失败。
代码中一律不得出现这两个名字，包括注释和测试。

#### 当前模型

| 用途 | 模型 ID | 规格 |
|------|---------|------|
| 默认主力 | `deepseek-v4-flash` | 284B 总参 / 13B 激活，1M 上下文。便宜快，多数场景够用 |
| 强推理路由 | `deepseek-v4-pro` | 1.6T 总参 / 49B 激活，1M 上下文，最大输出 384K。用于难诊断、代码修复 |
| 语料向量化 | **非 DeepSeek** | DeepSeek 不提供 embedding 接口。见 `rag/embedder.py`，需单独选型（本地 `sentence-transformers` 或第三方） |

> **模型名需启动时校验。** 官方定价页当前把 Flash 列为 `deepseek-flash`（= DeepSeek-V4.1-Flash），
> 而 `deepseek-v4-flash` 作为旧名仍被透明路由。两个名字都可能变。
> **要求**：`llm/client.py` 在启动时调一次 `client.models.list()`，把可用模型与配置里的
> 期望值比对，不一致就打 `error` 级日志并在 `/health` 暴露。
> 模型名只允许写在 `config.py` 一处。

#### 参数与调用约定

- **思考模式由 `thinking` 控制，不是靠换模型**：`extra_body={"thinking": {"type": "enabled"}}`。
  V4 **默认开启**思考；要旧的 `deepseek-chat` 式非思考行为必须显式关闭。
- **思考模式下 `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` 全部不生效**。
  设了不报错但会被忽略 —— 不要靠调 temperature 解决输出问题，那是在自欺。
- 工具调用用 OpenAI 风格 `tools` 数组，支持并行调用与 `strict` 模式（schema 强约束）。
  工具定义集中在 `agent/registry.py` 生成，不要在各 service 里手搓 JSON Schema。
- 结构化输出优先用 `response_format`（JSON 模式），不要自己 `json.loads` 后正则清洗。
  **构造结构化输出只经 `llm/client.py::get_structured_output`**，不要直接
  `model.with_structured_output(...)`：它默认的 `method="function_calling"` 会把
  `tool_choice` 绑成强制工具调用，而**思考模式不支持 `tool_choice`**，真实调用直接返回
  `400 Thinking mode does not support this tool_choice`（2026-10-25 在 v2 评估中实测）。
  该函数改用 json_mode，并把 Pydantic schema 的格式说明并进末条 human 消息
  （json_mode 不随请求发 schema）。回归测试：`tests/test_structured_output.py`。
- 长输出用 `graph.astream()`（见 §2 流式约定），避免 HTTP 超时。
- 重试交给底层 SDK 的 `max_retries`（默认 2），不要手写 `for i in range(3)`。

#### LangChain 集成缺陷：`reasoning_content` 会被丢弃（必须自行修复）

**这是本项目最高风险的技术点。不处理则 v2/v3 的 Agent 循环在第二轮必崩。**

- **现象**：`langchain-deepseek` 的 `ChatDeepSeek` 入站方向能正确把 `reasoning_content`
  存进 `AIMessage.additional_kwargs`；但**出站序列化**（`_get_request_payload` →
  `_convert_message_to_dict`，继承自 OpenAI 基类）不读 `additional_kwargs`，字段被**静默丢弃**。
- **后果**：单轮问答完全正常，测不出问题。一旦出现
  「模型 → tool_calls → ToolMessage → 再调模型」，第二轮请求缺少 `reasoning_content`，
  DeepSeek 直接返回 **400**：`The reasoning_content in the thinking mode must be passed back to the API.`
- **上游状态**：有社区 PR（#37461 出站镜像、#35093 在 tool_calls 场景回填 `""`），
  但**不能假定已合进你装到的版本**。装好后先验证。

**本项目要求**：

1. `llm/deepseek_chat.py` 定义 `ChatDeepSeekPatched(ChatDeepSeek)`，重写 `_get_request_payload`，
   把 `additional_kwargs["reasoning_content"]` 写回出站 assistant 消息；消息带 `tool_calls` 时，
   字段缺失则回填 `""`（空串也必须传，不能省略）。
2. **全项目只允许经这个子类创建模型实例**（`llm/client.py`）。禁止直接 `ChatDeepSeek(...)`。
3. `tests/test_deepseek_reasoning.py` 必须有一条**连续两轮工具调用**的回归测试，
   断言出站 payload 里 `reasoning_content` 确实存在。
4. 若上游已修复，子类退化成空壳也可接受，但**测试必须留着** —— 它是防回归的唯一保险。

**兜底**（仅在阻塞进度时启用，且必须记录）：在 Agent 图中关闭思考模式
（`extra_body={"thinking": {"type": "disabled"}}`）。代价是诊断质量下降，
需在 `docs/decisions/` 写下该决定与恢复计划。

#### 上下文缓存（成本大头，必须用对）

DeepSeek 对**重复前缀自动命中缓存**（≥1024 token，逐字节匹配），cache-hit 与 cache-miss 输入价差
可达 **50 倍**。这意味着：

- Prompt 前缀必须**逐字节稳定**：`llm/prompts/*.jinja2` 里禁止出现时间戳、随机 id、`datetime.now()`。
- 渲染顺序固定为 `tools` → `system` → `messages`，易变的用户输入永远放最后。
- `llm/tracing.py` **必须分别记录命中与未命中的 token**（见 §3.1），否则成本报表会严重失真。

#### 价格与错峰

峰值时段为 **UTC 周一至周五 01:00–04:00 与 06:00–10:00**，其余时段**半价**。
评估跑分（`evals/`）与批量任务应安排在非峰值时段执行 —— 这是本项目最大的一笔免费节省。

| 单位：USD / 1M tokens | 非峰值 | 峰值 |
|---|---|---|
| Flash 输入（命中缓存） | $0.003 | $0.006 |
| Flash 输入（未命中） | $0.15 | $0.30 |
| Flash 输出 | $0.60 | $1.20 |
| Pro 输入（命中缓存） | $0.022 | $0.044 |
| Pro 输入（未命中） | $0.66 | $1.32 |
| Pro 输出 | $1.98 | $3.96 |

> 价格变动频繁（2026-08 官方已公告将整体调价），第三方转载的数字与官网有出入。
> 上表仅用于量级判断与成本告警阈值设定；**以官网定价页为准**，不要把它硬编码进逻辑，
> 单价必须来自 `config.py` 的可配置常量（`config.py` 中的 `PRICE_TABLE`）。

### 数据库

| 环境 | 选型 | 说明 |
|------|------|------|
| 开发 | SQLite（`data/app.db`） | 零配置 |
| 生产 | PostgreSQL | 通过 `DATABASE_URL` 切换，代码不得出现方言分支 |

存什么：用户、会话、消息、练习记录、错题、Token 用量账本。
不存什么：向量（进 ChromaDB）、语料原文（进 `data/corpus/`）。

### 部署

`docker compose up` 一键起三件套：`api`（FastAPI）、`web`（Streamlit）、`sandbox` 基础镜像。
沙箱容器**必须**独立于 api 容器，见 §5 禁区。

---

## 2. 核心服务地图

### 目录结构

```
python助手/
├── AGENTS.md                    # 本文件
├── pyproject.toml               # 依赖与工具配置的唯一来源
├── docker-compose.yml
├── .env.example                 # 变量清单（不含真实值）
├── data/                        # 运行时产物，不提交
│   ├── corpus/                  # 原始语料：Python 文档 / PEP
│   ├── chroma/                  # 向量库持久化
│   └── app.db                   # SQLite
├── src/pyassist/
│   ├── config.py                # ★ 唯一配置入口
│   ├── logging.py               # ★ 唯一日志初始化
│   ├── llm/
│   │   ├── client.py            # ★ 唯一模型实例工厂 + 模型名启动校验
│   │   ├── deepseek_chat.py     # ★ ChatDeepSeekPatched：修复 reasoning_content 丢失
│   │   ├── tracing.py           # ★ 唯一 Token/成本记账（须区分缓存命中/未命中）
│   │   └── prompts/             # 全部 prompt 模板（jinja2，经 ChatPromptTemplate 加载）
│   │       ├── qa.jinja2
│   │       ├── diagnose.jinja2
│   │       ├── exercise.jinja2
│   │       └── grade.jinja2
│   ├── rag/
│   │   ├── ingest.py            # ★ 唯一写入向量库的地方
│   │   ├── corpus.py            # ★ 唯一语料抓取与解析（2026-10-13 新增，见 §6）
│   │   ├── store.py             # 向量库连接（Chroma/Qdrant 切换点）
│   │   ├── embedder.py          # ★ 唯一向量化出口
│   │   ├── retriever.py         # ★ 唯一检索入口
│   │   └── citations.py         # ★ 唯一引用构造与校验
│   ├── agent/
│   │   ├── state.py             # ★ 图状态 schema（MessagesState 扩展）
│   │   ├── checkpointer.py      # ★ 唯一检查点构造点（Sqlite/Postgres 切换处）
│   │   ├── store.py             # ★ 唯一长期记忆 Store 构造点
│   │   ├── registry.py          # ★ 唯一工具定义点（@tool）
│   │   ├── graphs/              # 每版本一张图，图内节点显式拼装
│   │   │   ├── qa.py            # v1：检索 → 引用 → 生成
│   │   │   ├── diagnose.py      # v2：检索 → 解释 → 修复 → 沙箱验证 →（失败回环）
│   │   │   ├── exercise.py      # v3：出题 → 自校验 → 跑测试 → 批改
│   │   │   └── progress.py      # v4：读错题 → 推荐
│   │   └── tools/
│   │       ├── docs.py          # search_docs / read_doc（retriever 的薄包装）
│   │       ├── sandbox_tools.py # run_code / run_tests
│   │       ├── grading.py       # generate_exercise / grade_code
│   │       └── progress.py      # recommend_next
│   ├── sandbox/
│   │   ├── runner.py            # ★ 唯一执行用户代码的地方
│   │   └── Dockerfile.sandbox   # 沙箱镜像，安全边界
│   ├── db/
│   │   ├── session.py           # 引擎与 Session
│   │   ├── models.py            # SQLAlchemy ORM
│   │   └── repositories.py      # ★ 唯一 SQL 出口
│   ├── services/                # 业务流程，一个版本一个文件
│   │   ├── qa.py                # v1
│   │   ├── diagnose.py          # v2
│   │   ├── exercise.py          # v3
│   │   ├── progress.py          # v4
│   │   ├── memory.py            # 阶段五：短期/长期/摘要记忆
│   │   └── usage.py             # ★ 唯一写 Token 账本 / 配额检查（ADR 0003 决定 2）
│   ├── api/
│   │   ├── main.py              # FastAPI app 工厂
│   │   ├── deps.py              # 依赖注入
│   │   ├── schemas.py           # ★ 响应契约（冻结）
│   │   ├── errors.py            # ★ 唯一异常 → HTTP 映射
│   │   └── routers/
│   │       ├── chat.py          # POST /chat（SSE）
│   │       ├── knowledge.py     # POST /knowledge
│   │       ├── diagnose.py      # POST /diagnose
│   │       ├── exercises.py     # POST /exercises, POST /exercises/{id}/submit
│   │       ├── history.py       # GET  /history
│   │       ├── progress.py      # POST /recommend（ADR 0003 决定 1）
│   │       └── stats.py         # GET  /stats
│   └── evals/
│       ├── run_rag_eval.py      # Recall@5、引用准确率
│       ├── run_v2_eval.py       # 错误类型准确率、修复通过率
│       ├── run_v3_eval.py       # 自洽率、生成自洽率、标注导出/一致率
│       └── datasets/            # 评估集
├── frontend/
│   ├── streamlit_app.py         # 只调 HTTP API
│   └── api_client.py            # 前端唯一 HTTP 客户端（路径由守护 #44 对齐 OpenAPI）
├── frontend_telegram/           # 可选适配器（v4 之后）
│   └── bot.py
├── scripts/
│   └── prune_threads.py         # 手动清理过期检查点（默认 dry-run）
├── deploy/                      # 生产 compose、Caddyfile、部署手册
├── docs/decisions/              # ADR：记录偏离本文件的决定（如临时关闭思考模式）
├── experiments/                 # 一次性对比实验，不参与部署
└── tests/
```

### 单点所有权（Single Owner）—— 不得创建平行实现

这是本文件最重要的一节。左列的能力**只能**在右列文件中实现，其他地方一律调用它。

| 能力 | 唯一归属 | 违反示例（会被打回） |
|------|----------|---------------------|
| 构造模型实例 | `llm/client.py` | 在节点里 `ChatDeepSeek(...)` 或 `ChatOpenAI(...)` |
| 发 LLM 请求 | `llm/client.py` 暴露的模型实例 | 节点里自己拼 `chat.completions.create` |
| 模型 ID 常量 | `config.py` | 在 tool 或 prompt 里硬写 `"deepseek-v4-pro"` |
| 构造结构化输出 | `llm/client.py::get_structured_output` | 各节点直接 `model.with_structured_output(...)` —— 默认 function_calling 绑 `tool_choice`，思考模式必 400 |
| 修复 `reasoning_content` | `llm/deepseek_chat.py` | 各节点各打一个补丁，或在图里手动补字段 |
| 写 Prompt 文本 | `llm/prompts/*.jinja2` | 在 Python 里写多行 prompt 字符串 |
| 计价（token → 成本） | `llm/tracing.py` | 各处自己累加 `usage_metadata` |
| 写 Token 账本 / 配额检查 | `services/usage.py` | 在 router 里直接 `append_token_usage(...)` |
| 定义工具 | `agent/registry.py` | 在 service 里随手 `@tool` 装饰一个新函数 |
| 图状态 schema | `agent/state.py` | 每张图各定义一个互不兼容的 State |
| 拼装图与节点 | `agent/graphs/*.py` | service 里再写一遍手搓的 `while` 工具循环 |
| 检查点构造 | `agent/checkpointer.py` | 在 router 里 `SqliteSaver.from_conn_string(...)` |
| 长期记忆读写 | `agent/store.py` | 另起一套用户画像表，或第二个向量库 |
| 流式事件 → SSE 映射 | `api/routers/chat.py` | 前端直接消费 LangGraph 原始事件 |
| 向量化文本 | `rag/embedder.py` | ingest 和 retriever 各用一套 embedding |
| 数 token（长度分桶的排序键） | `rag/embedder.py` 的 `Embedder.count_tokens` | 在 ingest 里自己摸 `tokenizer`，或按 `len(text)` 分桶 —— 后者实测只省 20.1% 而非 42.2%，见 §4 |
| 语料抓取与解析 | `rag/corpus.py` | 在 `ingest.py` 里顺手写下载 / HTML 解析，或让 `corpus.py` import `rag.store` |
| 写向量库 | `rag/ingest.py` | 运行时往库里 upsert |
| 读向量库 | `rag/retriever.py` | 直接 `collection.query()` |
| 生成引用对象 | `rag/citations.py` | 让 LLM 自由发挥引用格式 |
| 执行用户代码 | `sandbox/runner.py` | 用 `subprocess` / `exec()` 直接跑 |
| 访问数据库 | `db/repositories.py` | service 里裸写 `session.execute()` |
| 异常 → HTTP 状态码 | `api/errors.py` | router 里 `raise HTTPException(500)` |
| 响应外壳 | `api/schemas.py` | 手写 `return {"code": 0, "msg": ...}` |

> **新增「语料抓取与解析 → `rag/corpus.py`」（2026-10-13，按 §6 流程登记）**：
> 抓取/解析是新能力（网络 I/O + HTML 解析 + URL 映射），与 `ingest.py` 的「唯一写向量库」是
> 两种不同的变更理由。塞进 `ingest.py` 会让该文件同时承担抓取，守护 #23/#24 的边界跟着变糊。
> 拆开后 `parse_document` 是**纯函数**（入参是已读好的 HTML 字符串），可离线单测，
> 不需碰 Chroma / embedder。**现有代码里没有平行实现**（`rag/` 此前只有 `__init__.py`），
> 满足 §6 第 2 条。详见 `SPEC.md` §5.12 / `docs/specs/` 阶段五规格。

**工具名与实现名必须一致**：LLM 看到的 `search_docs` 必须对应 `agent/tools/docs.py` 里的 `search_docs`。
不允许出现 `search_docs` → `do_search` 这种映射层。

### 数据流（唯一正确路径）

```
Streamlit ──HTTP/SSE──> FastAPI router ──> services/*.py
                                                │
                                                ▼
                                     agent/graphs/<版本>.py
                                      （节点，由 LangGraph 调度）
                                                │
                        ┌───────────────────────┼───────────────────────┐
                        ▼                       ▼                       ▼
                  rag/retriever           sandbox/runner        db/repositories
                        │                       │                       │
                    ChromaDB              Docker 容器             SQLite/PG
                                                                            ▲
                                          agent/checkpointer.py ────────────┘
                                          （检查点写同一个库）
```

**前端不得 import `src/pyassist`**。前端只能通过 HTTP 说话 —— 否则 API 契约形同虚设，部署时也拆不开。

### 两份状态的分工（引入 LangGraph 后最易分叉的地方）

LangGraph 自带状态持久化，必须与 SQLAlchemy 划清边界，否则会出现两份互相矛盾的对话历史。

| 状态 | 归属 | 存什么 |
|------|------|--------|
| **图执行状态** | LangGraph Checkpointer（`agent/checkpointer.py`） | 消息轨迹、工具中间结果、节点产物。"对话上下文"的**唯一真相** |
| **业务记录** | SQLAlchemy（`db/repositories.py`） | 用户、练习提交、评分、错题、Token 账本。**不重复存对话消息** |

硬性规则：

- **同一个数据库。** `agent/checkpointer.py` 必须指向与 `db/session.py` 相同的库：
  开发 `SqliteSaver` → `data/app.db`，生产 `PostgresSaver` → 同一个 PG 实例。
  不允许出现第二个状态库，**禁止 `MemorySaver`**（重启即丢）。
- **对话历史只读 checkpoint。** 回放历史用 `app.get_state_history(config)`，
  不要在业务表里再存一份 `messages`。
- **恢复时不要传状态。** checkpointer 是 state authority —— `invoke()` 传入的 state 会被丢弃，
  续跑只需 `config={"configurable": {"thread_id": <会话 id>}}`。
- **节点必须幂等。** 恢复会重跑被中断的节点；副作用（写业务表、调沙箱）要带去重键。
- **检查点会膨胀。** 带大工具结果的检查点可达 10MB 级。`sandbox_tools.py` 必须截断超长输出
  （保留头尾各 N 行），并定期清理过期 thread。
- `checkpointer.setup()` / `store.setup()` 只需在建库时跑一次，写进迁移脚本，不要在请求路径里调。

**记忆系统（阶段五）的归属**：长期记忆与用户画像走 LangGraph `Store`（`agent/store.py`），
不走 `db/repositories.py`，也不另开第二个向量库。`services/memory.py` 只负责
"从对话里抽取偏好"这类加工逻辑，持久化一律经 `agent/store.py`。

### 流式约定

- **必须用 `await graph.astream(...)` / `astream_events(...)`。** 在 async 路由里调用同步
  `graph.stream()` 会阻塞事件循环 —— 一个慢请求拖垮所有连接。这条没有例外。
- 服务端**过滤后再转发**。一次 60 秒的 Agent 运行可能产生 3000+ 事件，
  只挑前端真正要的（token、工具开始/结束、引用、用量）映射成 §3.1 的 SSE 事件类型。
- 响应头必须带 `X-Accel-Buffering: no`、`Cache-Control: no-cache`。缺了这两行，
  本地跑得通，上 Nginx / Cloud Run 就被缓冲成"一次性吐出"。
- 每 ~15 秒发一次心跳注释（`: heartbeat\n\n`），撑过代理的空闲超时。

---

## 3. 代码约定

### 3.1 API 响应格式

所有非流式响应统一信封，成功失败都是 200 之外的语义靠 `ok` + HTTP 状态码双重表达：

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

失败：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "RETRIEVAL_EMPTY",
    "message": "未在语料中检索到相关内容",
    "details": { "query": "asyncio.gather 异常处理" }
  },
  "meta": {
    "request_id": "req_01H...",
    "latency_ms": 120,
    "usage": {
      "prompt_tokens": 0,
      "completion_tokens": 0,
      "cached_tokens": 0,
      "cost_usd": 0.0,
      "is_off_peak": true
    }
  }
}
```

（上面的 `usage` 是"这次请求没消耗模型"的**零值填法**，与下面的规则一致：
失败时 `usage` 仍然在位，缺就填零，**不省略字段**。）

规则：
- `meta` 三个字段**永远存在**，即使出错。缺 `usage` 时填零值而非省略。
- `usage.cached_tokens` **必须单独统计**。DeepSeek 缓存命中与未命中的输入价差 50 倍，
  合并成一个 `prompt_tokens` 会让成本报表严重偏低。`cost_usd` 由 `llm/tracing.py` 按
  命中/未命中分别计价后求和，并按 `is_off_peak` 套用半价。
- `error.code` 只能取 §3.2 的错误码枚举，不得自造字符串。
- `data` 永远是对象或 `null`，**不要裸数组**（列表放 `data.items` + `data.total`）。
- 分页统一 `?limit=&cursor=`，响应带 `data.next_cursor`。

### 3.2 错误码与异常

异常层级（`src/pyassist/errors.py`），`api/errors.py` 是唯一把它翻译成 HTTP 的地方：

```
AppError
├── ConfigError                 → 500
├── ValidationError             → 422
├── RetrievalError
│   ├── RetrievalEmpty          → 200（回答"文档中没有"是正常结果，不是错误）
│   └── CorpusMissing           → 503
├── LLMError
│   ├── LLMTimeout              → 504（可重试）
│   ├── LLMRateLimited          → 429（可重试，透传 retry-after）
│   ├── LLMInsufficientBalance  → 503（DeepSeek 402。**不可重试**，需人工充值）
│   ├── LLMModelNotFound        → 500（模型名已停用/拼错，见 §1 停用名单）
│   └── LLMBadResponse          → 502（流中断、tool_calls JSON 解析失败）
├── SandboxError
│   ├── SandboxTimeout          → 200（超时是实验结果，用 data.status="timeout" 表达）
│   └── SandboxRejected         → 200（安全策略拦截，同上）
└── StoreError                  → 500
```

以上是**业务**异常树。还有一类异常不在这棵树里 —— 它们由框架在路由匹配、方法匹配、
限流这些**传输层**环节抛出，根本不经过业务代码。为了让 §3.1 的"任何失败都带完整信封"
成立（否则 `error.code` 无值可取，而 §3.1 又禁止自造字符串），下面四个码**登记为枚举的
一部分，与上面的树同级**（2026-10-03 批准。属**新增**，不改动上面任何一条的语义）：

```
NOT_FOUND             → 404（未知路由）
METHOD_NOT_ALLOWED    → 405（方法不匹配）
RATE_LIMITED          → 429（本服务对调用方限流，slowapi）
INTERNAL_ERROR        → 500（兜底：未分类异常。不外泄 message 与 traceback）
```

- **`RATE_LIMITED` 与 `LLMRateLimited` 是两件事，必须分开**：前者是我们限调用方，
  后者是上游 DeepSeek 限我们。状态码同为 429，但**码不同** —— 混用会让"谁被限流了"
  这件事在报表里消失。
- 本次增补发生在 `API_VERSION` 首次发布之前（尚无已发布契约、无 `frontend/` 消费方），
  故**不涉及** §6 的破坏性变更流程；增补之后，这四个码与树内各码受同样的冻结约束。
- 因此**完整的 `error.code` 取值集合** = 业务异常树的全部叶子码 + 上面四个传输层码。

处理原则：
- **捕获链条，不要捕获大类**。按 `NotFoundError` → `RateLimitError` → `APIStatusError` → `APIConnectionError` 从具体到宽泛的顺序写，禁止一个 `except Exception` 吞掉全部。
- 重试用 SDK 自带的 `max_retries`（默认 2），不要手写 `for i in range(3)`。
- **用户代码执行失败不是服务错误**。报错、超时、被拦截都要以正常响应返回，前端展示为实验结果。只有沙箱本身挂了才是 `SandboxError` 的服务端故障。
- 每次异常必须 `structlog` 打点，带 `request_id`，但**不记 API Key、不记完整用户代码**（只记哈希与长度）。

### 3.3 命名规范

| 对象 | 规范 | 示例 |
|------|------|------|
| 模块 / 包 | `snake_case`，单数 | `retriever.py` 不是 `retrievers.py` |
| 函数 / 变量 | `snake_case` | `build_citations()` |
| 类 | `PascalCase` | `SandboxRunner` |
| 常量 | `UPPER_SNAKE` | `MAX_SANDBOX_SECONDS` |
| Pydantic 模型 | 后缀表明角色 | `ChatRequest` / `ChatResponse` / `MessageRecord` |
| SQLAlchemy 表 | 复数 `snake_case` | `messages`、`exercise_attempts` |
| 工具函数 | 与 LLM 所见工具名完全一致 | `run_tests` |
| Prompt 模板 | `<service>.jinja2` | `diagnose.jinja2` |
| 测试文件 | `test_<被测模块>.py` | `test_retriever.py` |

命名细节：
- 布尔值用 `is_` / `has_` / `should_` 前缀，不用 `flag`、`status` 这类含糊词。
- 不用缩写，除非是领域通用词（`llm`、`api`、`db`、`rag`、`ctx`）。
- 时间字段一律 `*_at` 后缀，存 UTC，类型 `datetime`（不是字符串）。
- 异步函数不加 `async_` 前缀（`async def` 已说明一切）。

### 3.4 类型与结构

- **公共函数必须有类型注解**，包括返回类型。`mypy --strict` 对 `src/pyassist/` 通过。
- 跨模块传递的数据用 Pydantic 模型或 `dataclass`，禁止裸 `dict` 穿过三层以上。
- 结构化 LLM 输出：用 `model.with_structured_output(SomeModel)` 直接拿 Pydantic 实例
  （底层走 DeepSeek 的 JSON 模式）。不要手写 JSON Schema 字符串，也不要 `json.loads` 后正则清洗。
- 禁止可变默认参数（`def f(x=[])`）。
- 异步优先：I/O 密集路径（LLM、HTTP、DB）一律 `async def`。沙箱执行是阻塞的，必须 `await asyncio.to_thread(...)` 或进程池，不得阻塞事件循环。

### 3.5 日志

- 只用 `structlog`，禁止 `print()`（`experiments/` 除外）。
- 关键节点必打：`llm.request`、`llm.response`（带 usage）、`tool.call`、`tool.result`、`retrieval.done`（带 top-k 文档 id 与分数）、`sandbox.exec`。
- 日志字段名固定：`request_id`、`session_id`、`user_id`、`latency_ms`、`tool_name`、`doc_ids`。
- 日志级别：`info` 记业务里程碑，`debug` 记 prompt 全文（生产默认关）。

---

## 4. 禁区

以下内容**禁止修改**或修改需走显式审批。Agent 遇到这些路径应停下并询问，不要"顺手改一下"。

| 路径 / 对象 | 规则 | 理由 |
|-------------|------|------|
| `data/corpus/**` | 禁止手工编辑、禁止删除 | 语料是一切回答的事实来源。要更新只能跑 `rag/ingest.py` 的抓取流程 |
| `data/chroma/**` | 禁止手改 | 向量索引产物。重建 = 删目录 + 重跑 ingest，不允许"局部修补" |
| `config.HNSW_SYNC_THRESHOLD` / `config.HNSW_BATCH_SIZE`（2026-10-13 新增，见 §6） | 禁止调回小值（尤其 Chroma 默认的 1000） | 调小 = 写入期间每 N 条把**整个索引**重写一遍，每次都是一次"写坏了就不可恢复"的机会。2026-10-13 全量 ingest 就是这样丢掉了前 16384 条向量（4.8 小时白跑，且 CLI 报成功）。阈值调大换来的是：磁盘上不再存在写了一半的索引，最坏情况降级为"从 WAL 现场重建"。详见 `SPEC.md` §10 第 15 条 |
| `src/pyassist/evals/datasets/**`（旧写作 `evals/datasets/**`，2026-10-13 补全路径） | 只能**新增**，禁止修改已有用例 | 评估集是回归基线。为了提分改测试集是本项目最严重的作弊 |
| `src/pyassist/sandbox/Dockerfile.sandbox` | 改动需人工 review | 这是隔离用户代码的唯一安全边界。放宽 = 把宿主机暴露给任意代码 |
| `src/pyassist/api/schemas.py` | 契约冻结 | 改字段 = 破坏性变更。必须 bump `API_VERSION` 并同步更新 `frontend/` |
| `src/pyassist/api/errors.py` | 唯一异常映射点 | 在别处 `raise HTTPException` 会让错误格式分叉 |
| `.env`、`*.db`、`data/**` | 禁止提交 | 含密钥与本地产物。`.env.example` 只列变量名 |
| `ai-clever-bubble.md` | 只读 | 学习路线，不是代码。要改目标请改本文件 |
| `frontend/**` 中的 `import pyassist` | 禁止 | 前端必须走 HTTP，见 §2 |

**其他硬性禁止**：
- 禁止在代码中出现 `deepseek-chat` / `deepseek-reasoner`（已停用，见 §1）。
- **思考模式下的 Agent 循环必须完整回传 `reasoning_content`**（修复点只在 `llm/deepseek_chat.py`）。
  发生工具调用后，下一轮请求若丢掉该字段，DeepSeek 返回 400。只测"第一步能调工具"不算通过 ——
  必须测"连续两轮工具调用"。这是本项目最容易踩且最难排查的坑。
- 禁止在 prompt 模板里插入时间戳 / 随机 id / 未排序的 dict，会击穿上下文缓存让成本涨 50 倍。
- 禁止在 `src/` 内使用 `exec()` / `eval()` 执行用户代码（只能经 `sandbox/runner.py`）。
- 禁止把 API Key 写进代码、日志、错误消息、测试快照。
- 禁止在没有沙箱隔离的情况下跑任何来自用户或 LLM 的代码片段。
- 禁止为通过测试而修改测试或跳过测试（`pytest.skip` 需注释说明原因）。
- 禁止直接 `ChatDeepSeek(...)` / `ChatOpenAI(...)`，必须经 `llm/client.py` 拿 `ChatDeepSeekPatched`。
- 禁止直接 `model.with_structured_output(...)`，必须经 `llm/client.py::get_structured_output`
  （思考模式不支持 `tool_choice`，见 §1）。
- 禁止在业务表里再存一份对话消息（对话历史的唯一真相是 LangGraph 检查点，见 §2）。
- 禁止在 async 路由里调用同步的 `graph.stream()`；禁止让前端消费 LangGraph 原始事件。
- 禁止引入 LlamaIndex / Gradio / LangChain 预置 Agent 到 `src/`。
- 禁止 `except Exception: pass`。
- **禁止拿 `count()` 当"索引可用"的判据**（2026-10-13 补，见 §4 表与 `SPEC.md` §10 第 15 条）。
  `count()` 读的是**元数据段**，`query()` 才真正走 HNSW —— 两者撕裂正是事故的形态：
  实测一个坏库上 `count()` 报 17366，而检索只找得到 983 条。要判可用性用
  `rag/retriever.py::probe_index_depth`（关掉→重开→多要结果，1% 容差）。
- **禁止绕过 `rag/retriever.py` 直接 `collection.query()` / `collection.get()`**（§2 单点所有权）。
  2026-10-13 写 ingest 验收时就这么干过，被 `test_contract_guardrails.py::test_chroma_query_only_in_rag_retriever`
  抓住 —— 探针因此住在 `retriever.py`。守护 #24 是真的会红的，别试着绕。

---

## 5. 验收标准

### 5.1 通用完成定义（Definition of Done）

任何 v1–v4 功能，声明"完成"前必须同时满足：

- [ ] `pytest` 全绿，新增逻辑有对应测试，**测试不依赖真实 LLM 或网络**（用 `respx` / mock）
- [ ] `mypy --strict src/pyassist` 无错误，`ruff check` / `ruff format` 无 diff
- [ ] 所有对外响应符合 §3.1 信封，`meta.usage` 有值
- [ ] 关键节点有 `structlog` 打点，`request_id` 能串起完整一次调用
- [ ] 新错误码已登记进 §3.2 枚举，并由 `api/errors.py` 映射
- [ ] **涉及工具调用的功能，`tests/test_deepseek_reasoning.py` 通过**（连续两轮工具调用不触发 400）
- [ ] 涉及流式的功能：`X-Accel-Buffering: no` 已设，前端能逐字看到输出，不是等完再吐
- [ ] 图的节点幂等：同一 thread 从任一检查点恢复重跑，不产生重复副作用
- [ ] 没有 `MemorySaver`；检查点与业务表指向同一个库
- [ ] `docker compose up` 从干净环境能起，README 里的步骤照做可复现
- [ ] 没有触碰 §4 禁区

### 5.2 v1 — 文档问答 + 引用

- [ ] 语料覆盖：Python 官方文档核心章节 + PEP 索引，ingest 可重跑且幂等
- [ ] **Recall@5 ≥ 0.80**（在 `evals/datasets/qa.jsonl` 上）
- [ ] **引用准确率 ≥ 0.90**：答案中每个引用链接指向的文档，确实包含支撑该结论的片段
- [ ] **100% 的实质性回答至少带 1 个可点击来源**；检索不到时必须明说"文档中没有"，不得凭记忆作答
- [ ] 引用由 `rag/citations.py` 生成，不是 LLM 自由发挥的文本

### 5.3 v2 — 错误诊断 + 代码执行

- [ ] 测试集：20 个真实 Python 报错（`evals/datasets/errors.jsonl`）
- [ ] 错误类型识别准确率 ≥ 85%
- [ ] **修复后代码在沙箱内实际执行通过率 ≥ 70%**（"看起来对"不算，必须跑过）
- [ ] 沙箱约束生效并经验证：无网络、只读根文件系统、CPU/内存/时间上限、非 root
- [ ] 恶意用例（死循环、`os.system("rm -rf /")`、读 `/etc/passwd`、fork 炸弹）全部被拦截且服务不崩
- [ ] 用户代码报错/超时以正常响应返回（`data.status`），不产生 5xx

### 5.4 v3 — 练习生成 + 自动批改

- [ ] 覆盖四个主题：列表、字典、装饰器、异步
- [ ] 生成的每道题**自带测试用例，且参考答案能让测试全绿**（100% 自洽，CI 里自动校验）
- [ ] 用户代码在沙箱内跑测试，输出结构化结果（通过/失败用例、stdout、stderr、耗时）
- [ ] 评分含可解释反馈：指出错在哪、为什么错、指向哪份文档；不得只给分数
- [ ] 评分与人工评分一致率 ≥ 80%（抽 30 份人工标注）
- [ ] 练习记录与错题落库，`GET /history` 可查

### 5.5 v4 — 学习路径 + Web UI

- [ ] `recommend_next` 输入错题记录，输出下一步主题 + 理由 + 对应文档链接
- [ ] 推荐不是空话：必须引用该用户实际做错的题目 id
- [ ] Streamlit 界面跑通完整链路：提问 → 引用 → 贴报错 → 沙箱验证 → 做题 → 批改 → 推荐
- [ ] 流式输出可见（SSE token 逐字上屏），不是转圈等 30 秒
- [ ] 页面刷新/重新打开后，历史与进度仍在（落库，不靠 session state）
- [ ] Token 用量与成本在 `/stats` 可见，单日超限有熔断

### 5.6 阶段八整体

- [ ] `docker compose up` 后，公网可访问（Railway / Fly.io / 自建任一）
- [ ] README 含：架构图、环境变量清单、一键启动命令、`evals/` 跑分方法
- [ ] 演示脚本：一个从提问到推荐的完整 3 分钟录像或 GIF

---

## 6. 变更本文件

本文件是契约，不是文档。修改需满足：
1. 说明改哪一节、为什么现在不对；
2. 若是新增禁区或单点所有权，先确认现有代码里没有平行实现；
3. 改 §3.1 / §3.2 属破坏性变更，需同步 bump `API_VERSION` 并更新 `frontend/`。
   **例外：新增枚举值不是破坏性变更** —— 既有取值不变、消费方无需改动。
   删码、改状态码、改既有码的语义才是。凡新增，在 §3.2 就地标注日期与理由，
   并在 `docs/decisions/` 留一份 ADR（例：ADR 0002 决定 1）。

---

**实际锁定版本**（2026-10-22 回填，来源 `uv.lock` / `importlib.metadata`）：
```
openai                          1.109.1      langgraph                       1.1.10
fastapi                         0.142.2      langchain-core                  1.6.6
pydantic                        2.13.5       langchain-deepseek              1.1.0
pydantic-settings               2.15.0       langgraph-checkpoint-sqlite     2.0.11
sqlalchemy                      2.1.3        langgraph-checkpoint-postgres   2.0.25
alembic                         1.20.0       chromadb                        1.5.9
uvicorn                         0.54.0       sentence-transformers           3.4.1
httpx                           0.28.1       streamlit                       1.65.0
structlog                       26.1.0       docker                          7.2.0
slowapi                         0.1.10       sse-starlette                   3.5.0
jinja2                          3.1.6        python-dotenv                   1.2.4
pytest                          9.1.1        pytest-asyncio                  1.4.0
respx                           0.23.1
```

> 该表由 `uv.lock` 唯一决定；升级依赖后必须同步回填，否则本文件会与锁文件对不上。
> 注意 `streamlit` 在 `[dependency-groups] web` 里，不在 `project.dependencies`
> （ADR 0004 决定 3）：它不属于后端运行时依赖。
