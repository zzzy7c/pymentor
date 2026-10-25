# SPEC — 阶段六 · v1 文档问答 + SSE · 实施规格

> **归档声明**：阶段一（工程底座）、阶段二（LLM 基座）、阶段三（持久化与状态双源）、
> 阶段四（API 契约与错误体系）、阶段五（RAG 子系统）的规格均已归档到 `docs/specs/`：
> `阶段一-工程底座.md`、`阶段二-LLM基座.md`、`阶段三-持久化与状态双源.md`、
> `阶段四-API契约与错误体系.md`、`阶段五-RAG子系统.md`。
> 本文只描述**阶段六**。阶段五的归档件与原 `SPEC.md` **逐行一致**（`cmp` 校验通过）。
>
> 本文件是施工规格，不是契约。契约是 `AGENTS.md`；两者冲突时以 `AGENTS.md` 为准。
> 本文的每一条设计决定都能追到 `AGENTS.md` 的某个条款号或本文件的某条实测事实（§2.3）。

**阶段六 = v1 文档问答 + SSE**（`ROADMAP.md`「阶段六 · v1 文档问答 + SSE」）。
它把阶段五的引擎装进壳里：**第一张 LangGraph 图**、**第一个流式接口**、**第一份跑分报告**。

`AGENTS.md` §5.2 的三个硬指标（Recall@5 ≥ 0.80、引用准确率 ≥ 0.90、100% 实质性回答带可点击来源）
在阶段五就已被引擎决定；本阶段的任务是**让它们可被观测**，并在流式路径上不把它们丢掉。
真正的新风险只有一个：**思考模式 + 工具循环 + 流式三件事第一次同时成立**。
阶段二的回归测试只证明了"两轮请求的 payload 里 `reasoning_content` 在"，
本阶段要证明"图真的跑过两轮工具循环之后它还在"。

---

## 0. 定位与硬指标

| 项 | 值 | 出处 |
|---|---|---|
| 交付版本 | v1（文档问答 + 引用） | `AGENTS.md` §0 |
| 三个硬指标 | Recall@5 ≥ 0.80 ｜ 引用准确率 ≥ 0.90 ｜ 100% 实质性回答 ≥ 1 个可点击来源 | `AGENTS.md` §5.2 |
| 产品原则 | 宁可回答"文档里没有"，也不要编造 API；所有解释可溯源 | `AGENTS.md` §0 |
| 前端 | 本阶段**不做**（Streamlit 是阶段八）；用 `curl -N` 与测试证明流式 | `AGENTS.md` §5.5 |

**本阶段完成后，以下四件事必须同时成立：**

1. `POST /chat` 逐字吐 token，且"文档里没有"是以一条 `error` 事件传达的，
   客户端能把它和"网络断了"区分开（HTTP 仍是 200，`code=RETRIEVAL_EMPTY`）；
2. `POST /knowledge` 非流式返回**同一条**答案 + 结构化引用（同一条服务路径，D10）；
3. `python -m pyassist.evals.run_rag_eval` 打印 Recall@5 与引用准确率，且**分数可复现**；
4. 一张图里 `retrieve → agent ⇄ tools → finalize` 真的跑过两轮工具调用，
   同一 `thread_id` 从任一检查点恢复都不产生重复副作用。

---

## 1. 范围边界

### 1.1 做

| 交付 | 对应 `AGENTS.md` |
|---|---|
| `agent/state.py`：唯一图状态 schema（`MessagesState` 扩展） | §2 单点所有权「图状态 schema」 |
| `agent/registry.py`：唯一工具定义点（`tool(...)` 只在这里出现） | §2「定义工具」/ §1「工具定义集中在 `agent/registry.py` 生成」 |
| `agent/tools/docs.py`：`search_docs` / `read_doc` 的**纯实现**（不含 `@tool`） | §2「工具名与实现名必须一致，不允许映射层」 |
| `agent/graphs/qa.py`：v1 图，节点手拼，**不用** `langgraph.prebuilt` | §1「用 LangGraph 的画布，不用 LangChain 的成品 Agent」 |
| `services/qa.py`：`answer()` / `stream_answer()` | §2 数据流 |
| `api/routers/chat.py`：`POST /chat`（SSE），**唯一** LangGraph 事件 → SSE 映射点 | §2「流式事件 → SSE 映射」 |
| `api/routers/knowledge.py`：`POST /knowledge`（非流式） | §2 目录结构 |
| `llm/prompts/qa_context.jinja2`：检索片段注入模板 | §2「写 Prompt 文本」+ §1 前缀缓存 |
| `evals/metrics.py` + `evals/run_rag_eval.py`：Recall@5 / 引用准确率 | §5.2 度量 |
| 4 个测试文件 + `tests/fakes/llm.py` | §5.1「测试不依赖真实 LLM 或网络」 |
| 契约守护 #27–#33 | §5.1 |
| `config.py` / `api/schemas.py` / `api/main.py` / `api/deps.py` / `tests/conftest.py` 增量 | —— |
| `rag/retriever.py` 追加 `assert_index_ready()` 一个函数（见 §9.1） | §2「读向量库」归属不变 |

### 1.2 不做（明确排除，验收时按此清单反查）

| 不建 | 归哪个阶段 | 理由 |
|---|---|---|
| Streamlit / 任何前端 | 阶段八 | `AGENTS.md` §5.5 |
| 沙箱、`run_code`、v2 诊断图 | 阶段七 | —— |
| 练习生成、批改、错题 | 阶段七之后 | —— |
| 会话建档（`create_chat_session`）、Token 账本落库（`append_token_usage`） | 阶段八接 `/stats` 时统一 | 见 §10.4；`repositories.py` 已备好函数，本阶段**不接请求路径** |
| `daily_token_limit` 熔断 | 阶段八 | `Settings` 字段已在，本阶段不判 |
| BM25 / 混合检索 / rerank / query 改写 | 阶段五之后（§10.6） | 先把稠密检索的门跑过再说 |
| 长期记忆 `agent/store.py` 的读写 | 阶段八 | 构造点已在，本阶段不打开（见 §9.6） |
| `MemorySaver` / 第二个状态库 | 永不允许 | §2 / §4 硬禁 |
| 为后续阶段预留的空函数 / TODO 占位 | —— | 缝只以「唯一入口 + 已归一化的数据 + 守护测试」形式存在 |

---

## 2. 前置核查结果（2026-10-21 本机实测 / 读 `.venv` 源码）

设计的地基。全部已核实，不是推测。**这份表是本阶段唯一允许"不重新验证就照抄"的东西。**

### 2.1 已就位的可复用缝（不要重造）

| 缝 | 位置 | 阶段六怎么用 |
|---|---|---|
| `retrieve()` / `fetch_document()` | `rag/retriever.py` | `search_docs` / `read_doc` 的**全部**实现。无命中返回 `[]` 不抛 |
| `RetrievedChunk`（frozen dataclass） | `rag/retriever.py:60` | 直接进图状态（L3 已验证过检查点仍是 dataclass） |
| `build_citations` / `render_reference_block` / `find_ungrounded_markers` | `rag/citations.py` | 编号**只**由 `build_citations` 产生（D4）；来源块由 `render_reference_block` 机械追加 |
| `RetrievalEmpty` → 200 / `CorpusMissing` → 503 / `ConfigError` → 500 | `errors.py` + `api/errors.py::_MAPPING` | **本阶段不新增错误码**，映射已就位 |
| `Envelope[T]` / `envelope_success` / `build_meta` / `get_request_context` | `api/schemas.py` / `api/deps.py` | `/knowledge` 直接套 |
| `open_checkpointer()`（`@asynccontextmanager`） | `agent/checkpointer.py:44` | lifespan 里打开一次；`AsyncSqliteSaver.__init__` 要 `get_running_loop()`，**只能**在 lifespan 里进 |
| `get_chat_model(tier=..., thinking=...)` | `llm/client.py:57` | 唯一模型工厂；模型由 lifespan 注入图（D11） |
| `extract_usage` / `log_llm_response` | `llm/tracing.py` | 节点里记账；`usage` 进图状态再进 `meta.usage` |
| `load_prompt(name)` | `llm/prompts/__init__.py` | 渲染顺序固定 `system` → `history`（前缀缓存）。**只适用于系统提示**：它强制 `system` 角色并追加必填的 `history` 占位符，片段块另走 `_context_template()`（L17，§5.5） |
| `_python_sources()` / `_call_name()` / `_call_keywords()` / `_imported_modules()` | `tests/test_contract_guardrails.py:85-470` | 追加 #27–#33 沿用同一套 AST 辅助 |
| `create_app(*, database_url, rate_limit_enabled)` | `api/main.py:84` | **新增参数必须带默认值**，否则阶段四既有测试全红 |
| `app.router.routes.extend(...)` 挂载法 | `api/main.py:105-113` 的注释 + ADR 0002 | **必须沿用**（`include_router` 在 fastapi 0.142 下产出的 `_IncludedRouter` 没有 `.endpoint`，slowapi 默认限流对它**静默失效**） |
| `HashingEmbedder` + `collection` 夹具 | `tests/fakes/embedder.py` / `tests/conftest.py:67` | 图的集成测试用**真实检索链路**，只是换了嵌入实现。测试经 `Searcher`/`Fetcher`/`preflight` 三个闭包注入，`agent/` 层不碰 `chromadb`（§5.3） |

**结论：阶段六只需新增一个 `rag/retriever.py::assert_index_ready()`（§9.1），其余全部是"用"。**

### 2.2 已装 / 未装

- **已装且本次要用**：`langgraph 1.1.10`、`langgraph-checkpoint 2.1.2`、
  `langgraph-checkpoint-sqlite 2.0.11`、`langchain-core 1.6.6`、`langchain-deepseek 1.1.0`、
  `sse-starlette 3.5.0`、`fastapi 0.142.2`、`starlette 1.7.0`、`slowapi 0.1.10`、
  `pydantic 2.13.5`、`chromadb 1.5.9`、`respx`、`pytest-asyncio`。
- **未装、本阶段也不需要**：`pytest-timeout`（心跳测试用 `asyncio.wait_for` 自带的超时控制，
  不引新依赖）。`pyproject.toml` **本阶段不改**：不新增依赖、不新增 marker（§10.9）。

### 2.3 LangGraph 1.1.10 + sse-starlette 3.5.0 实测事实（写错就不过验收）

| # | 事实（实测命令见 §7.1 末条） | 对实现的强制后果 |
|---|---|---|
| L1 | `MessagesState` 是 `TypedDict`，唯一键为 `messages: Annotated[list[AnyMessage], add_messages]`；**可以被子类继承**并追加键 | 所有图共用 `agent/state.py` 的扩展（§5.2） |
| L2 | `graph.astream(..., stream_mode=["custom","updates"])` 逐条产出 `(mode, payload)` **二元组** | 路由必须按 `mode` 分支；`updates` 只用于兜底取状态，**不转发** |
| L3 | 检查点 serde 对 **frozen dataclass** 与 **`Decimal`** 都能原样还原：`RetrievedChunk`、`Citation`、`UsageSnapshot`（含 `Decimal('0.00012345')`）过一遍 `aget_state` 后类型不变 | `chunks / citations / usage` 可以**直接**放进图状态，不需要"存 dict 再反序列化"的胶水 |
| L4 | **`ToolMessage.artifact` 不走 L3 那条路**：内存里是 `RetrievedChunk`，过检查点后回来是 **`dict`** | 状态的权威只能是 `state["chunks"]`；`tools` 节点**当轮**读 artifact，绝不跨轮依赖它（判据 6） |
| L5 | `get_stream_writer()` 在节点里（`ainvoke` 下也一样）返回 writer；在**节点外**调用抛 `RuntimeError: Called get_config outside of a runnable context` | 服务层与路由**不得**调用它；事件只能在节点里写（D5） |
| L6 | 流式工具调用：把 `AIMessageChunk` 用 `+` 聚合后 `.tool_calls` 是**已解析好的** `{"name","args","id","type"}`；首个 chunk 的 `additional_kwargs` 带 `reasoning_content` | `agent` 节点自己聚合 chunk（不靠回调），聚合体就是下一轮要回传的消息（D1） |
| L7 | `ChatDeepSeekPatched` 的流式最后一帧带 `usage_metadata`，且 `input_token_details.cache_read` 已被归一化（实测 `{'cache_read': 8}`） | 记账可以只从**聚合后的最后一块**取，不必另开非流式调用 |
| L8 | `tool(fn, response_format="content_and_artifact")` → `StructuredTool`：`.name == fn.__name__`、`.coroutine is fn`、`.func is None`；同步 `.invoke()` 抛 `NotImplementedError` | 工具必须 `await t.ainvoke(...)`（异步）；判据 9 用 `.coroutine.__name__` 钉"名字一致"。**入参形状决定能否拿到 artifact，见 L18** |
| L9 | `event_source_response` 相关：`EventSourceResponse` 默认设 `X-Accel-Buffering: no` + `Cache-Control: no-store`，**用户传的 headers 先写入再 `setdefault`**；`ping=None` → 15s，`ping=0` → 不启心跳任务；`sep` 默认 `"\r\n"` | 必须显式传 `headers={"Cache-Control": "no-cache"}`（契约要的是 `no-cache` 不是 `no-store`）、`ping=0`、`sep="\n"`（D5） |
| L10 | `EventSourceResponse` 的默认心跳是 `comment=f"ping - {datetime.now(timezone.utc)}"` —— **不是** `: heartbeat`，且带时间戳 | 契约点名字面量 `: heartbeat\n\n`，故库自带心跳**必须关掉**，自己发（D5） |
| L11 | `ServerSentEvent(comment="heartbeat", sep="\n").encode() == b': heartbeat\n\n'`；`ServerSentEvent(data="hi", event="token", sep="\n").encode() == b'event: token\ndata: hi\n\n'` | §2.4 的线上格式**逐字节可测**（`test_sse.py` 直接断言字节） |
| L12 | 经 `app.router.routes.extend(...)` 挂载的 SSE 路由，**也会**被 `_request_context_middleware` 盖上 `X-Request-ID`（实测响应头里有） | 流式响应不需要在路由里重复发 id；`done` 事件里再带一次给前端用 |
| L13 | `langgraph.prebuilt` 里确实有 `create_react_agent` / `chat_agent_executor` —— 它们**能被 import 成功**，不会有任何警告 | 只靠"没人会写"不成立，必须有守护 #27 |
| L14 | `langchain_core` 的 `GenericFakeChatModel.bind_tools()` 抛 `NotImplementedError` | 测试替身**不能用现成的 Fake 模型**，必须自写脚本化模型（§5.13）；这是个真坑，写在测试基建里 |
| L15 | **只有 `Annotated[..., InjectedToolArg]` 能把参数从工具 schema 里藏掉**；仅写成 keyword-only 不管用（`def f(q, *, top_k=5)` 的 schema 里仍有 `top_k`）。`StructuredTool._injected_args_keys` 列出被藏掉的键；注入值经 `tool_call["args"]` 传入，不传则用参数默认值；**未知键被静默丢弃**（不报错） | `search_docs` / `read_doc` 的 `top_k` / `start_index` / `searcher` / `fetcher` 全标 `InjectedToolArg`（§5.3），模型只看得见 `query` / `doc_url`；节点按工具名分别组装注入字典（§5.5） |
| L16 | `response_format="content_and_artifact"` 要求函数返回**二元组**；返回别的（如裸 `list`）会 `ValueError: ... a two-tuple of the message content and raw tool output is expected`。不传 `response_format` 时默认 `"content"`，裸 `str` 可 | `search_docs` 返回 `(文本, chunks)` 且必须带 artifact；`read_doc` 返回 `str` 且**不带** artifact（§5.3 / §5.4） |
| L17 | 现有 `load_prompt()` 把模板当 **`system` 角色**且**追加必填的 `MessagesPlaceholder("history")`**：少传 `history` → `KeyError: 'history'`；传了 → 得到**一条 `SystemMessage`**。而 `ChatPromptTemplate.from_messages([("human", text)], template_format="jinja2")` 渲染出**恰好一条 `HumanMessage`** | 片段块**不能**走 `load_prompt()`（第二个 system 会插到历史中间，前缀缓存与"system 恒在最前"同时破裂）。`qa_context.jinja2` 用 §5.5 的 `_context_template()` 独立加载，角色 `human`、不带 placeholder |
| L18 | `StructuredTool.ainvoke()` **按入参形状决定返回值**：传 `ToolCall` 形状的 dict（含 `name` / `args` / `id` / `type`）→ 返回 `ToolMessage`（带 `.artifact`、`.tool_call_id`）；传**裸 args dict** → 返回裸 `str`，`.artifact` 根本不存在。缺 `"type": "tool_call"` 的 dict 会被当成裸 args 解包而 `ValidationError`；`name` 与工具不匹配时**静默用**（不校验） | `tools` 节点必须把注入参数并进 `tc["args"]` 后**保留 `ToolCall` 形状**再 `ainvoke`（§5.5 的 `_tool_call_with_injection`）；`next_index += len(message.artifact or [])` 才有东西可加 |
| L19 | 异步生成器**在 `finally` 里 `yield`**，被 `aclose()`（客户端断开 / 上层 `task.cancel()`）时抛 `RuntimeError: async generator ignored GeneratorExit`。放在 `try/except` 之后的普通语句则正常 | `stream_answer` 的 `done` 不能写在 `finally` 里（§5.7）；`test_sse` 有一条"读到一半就 `aclose()`"的用例钉住它 |
| L20 | **`None` 结束哨兵会死锁**：`_pump` 在 `finally` 里 `await queue.put(None)`，当队列已满且消费方已 `cancel()` 时，这个 `put` 永久阻塞；`gather(task, return_exceptions=True)` 于是永不返回（实测 `maxsize` ∈ {1, 2, 8} **全部挂死**）。取消打断的是 `gather` 的等待者，不是那个已经卡在 `put` 里的协程。另外：生产者异常结束时 `_sse_stream` 只看到"task 完成"，**看不到异常**（除非查 `task.exception()`） | 不用哨兵：`_sse_stream` 用 `asyncio.wait({getter, task}, FIRST_COMPLETED)` 与生产者的**完成**竞速，`_pump` 只 `await queue.put(event)`（§5.9）。另外两条实测结论：漏掉 `getter.cancel()` → `aclose()` 超时；只 `break` 不排干队列 → 丢尾部 `done` 事件 |

### 2.4 SSE 线上格式（逐字节约定）

一个事件 = 若干 `字段: 值\n` + 一个空行。本项目**只发三种字段**：`event`、`data`、注释行。
`data` 一律是**单行 JSON**（`json.dumps(..., ensure_ascii=False)`，不带缩进）——
JSON 里的换行会被序列化成 `\n` 转义，故物理行数恒为 1，`data:` 不需要续行规则。

| 事件 | 线上字节 | 何时发 |
|---|---|---|
| token | `event: token\ndata: {"text": "..."}\n\n` | `agent` 节点每收到一段 content |
| citations | `event: citations\ndata: {"citations": [{...}]}\n\n` | `retrieve` 节点末 / `tools` 节点末（编号变化时） |
| tool | `event: tool\ndata: {"phase": "start", "name": "search_docs"}\n\n` | `tools` 节点进出各一次（`phase` ∈ `start` / `end`） |
| error | `event: error\ndata: {"code": "RETRIEVAL_EMPTY", "message": "...", "details": {...}}\n\n` | 图内抛 `AppError`（典型是 `RetrievalEmpty`） |
| done | `event: done\ndata: {"request_id": "...", "usage": {...}}\n\n` | **每一条流都以它结束**（成功与失败路径都要发） |
| 心跳 | `: heartbeat\n\n` | 每 `SSE_HEARTBEAT_SECONDS` 秒一次，与上面所有事件**可交错** |

三条硬规则（都由 `test_sse.py` 断言）：

1. **不以 `retry:` 开头也不要发 `retry:`** —— 我们不希望 `EventSource` 自动重连
   （重连会重跑整张图，等于重复计费）。前端在 `done` 时主动断开。
2. **`error` 事件之后仍然发 `done`**。没有 `done` 的流会被误读成断线。
3. **`error` 路径不产出任何 `token` 事件**（判据 1）—— "文档里没有"不该先吐半个答案。

### 2.5 评估集现状（不改，只读）

`src/pyassist/evals/datasets/qa.jsonl`：**44 条**，字段 `id / question / relevant_urls /
expected_facts / topic`；主题分布 asyncio 6、typing 6、pathlib 6、生成器 6、dataclasses 5、
json 5、异常 5、装饰器 5。`relevant_urls` 是**无锚点的 `doc_url`**，
因而 Recall@5 与引用准确率都能用**纯字符串**比对，不需要 LLM 裁判（§10.3）。
该目录被 `AGENTS.md` §4 冻结（只增不改），`test_eval_dataset.py` 已在守。

---

## 3. 目标文件清单

`✓` 新建 ｜ `△` 修改 ｜ `★` 契约关键件

```
src/pyassist/
├── config.py                  △   SSE_HEARTBEAT_SECONDS / MAX_TOOL_ROUNDS / MAX_HISTORY_MESSAGES
│                                   / MAX_GROUNDING_RETRIES / READ_DOC_MAX_CHARS / QA_PROMPT_NAME
│                                   / QA_CONTEXT_PROMPT_NAME / OUTPUT_LANGUAGE
│                                   + 回填 RETRIEVAL_MIN_SCORE（§10.2）
├── errors.py                  ×   **不改**：本阶段不新增任何错误码（§2.1 / §9.1）
├── agent/
│   ├── state.py               ✓ ★ 唯一图状态 schema（AppState / QAState）
│   ├── registry.py            ✓ ★ 唯一工具定义点（tool(...) 只在这里）
│   ├── tools/
│   │   └── docs.py            ✓    search_docs / read_doc 纯实现（async def，无 @tool）
│   └── graphs/
│       └── qa.py              ✓ ★ v1 图：retrieve → agent ⇄ tools → finalize（手拼）
├── llm/
│   └── prompts/
│       └── qa_context.jinja2  ✓   检索片段注入模板（新文件，不动 qa.jinja2 的字节）
├── rag/
│   └── retriever.py           △   追加 assert_index_ready()（§9.1，唯一改动）
├── services/
│   └── qa.py                  ✓ ★ answer() / stream_answer()（两路由共用）
├── api/
│   ├── main.py                △   lifespan 打开 checkpointer + 编译图；挂两个新 router
│   ├── deps.py                △   get_checkpointer() / get_qa_graph()
│   ├── schemas.py             △   只追加：ChatRequest / KnowledgeRequest / QAData / CitationData
│   └── routers/
│       ├── chat.py            ✓ ★ 唯一 LangGraph 事件 → SSE 映射点
│       └── knowledge.py       ✓   POST /knowledge
└── evals/
    ├── metrics.py             ✓   Recall@5 / 引用准确率（纯函数）
    └── run_rag_eval.py        ✓   CLI 跑分器

tests/
├── fakes/llm.py               ✓   脚本化 ChatModel（L14：现成 Fake 不能用）
├── conftest.py                △   追加 qa_app / chat_model / qa_graph 夹具
├── test_registry.py           ✓   工具名 == 函数名；schema 只从 registry 出
├── test_graph_qa.py           ✓   图：检索→工具→答案；空检索；两轮工具；检查点往返
├── test_sse.py                ✓   线上字节、两个响应头、心跳、事件过滤、error 路径
├── test_rag_eval.py           ✓   metrics 纯函数在固定数据上的可复现分数
└── test_contract_guardrails.py △  追加 #27–#33

docs/
└── evals/阶段六-跑分.md       ✓   跑分记录（§10.5；README 是阶段八产物）
```

**不创建**：`frontend/**`、`sandbox/**`、`services/diagnose.py`、`services/exercise.py`、
`agent/graphs/diagnose.py`、`evals/datasets/errors.jsonl`、任何迁移脚本。

---

## 4. 关键设计决定（D1–D11，施工时不可协商）

### D1 · 图必须是**真的工具循环**，不是固定流水线

节点与边（全部在 `agent/graphs/qa.py` 里手拼）：

```
START → retrieve ──有──→ agent ──有 tool_calls──→ tools ──→ agent（最多 MAX_TOOL_ROUNDS 轮）
              └──空──→ raise RetrievalEmpty      └──无 tool_calls──→ finalize → END
```

**没有 `finalize_empty` 节点**：空检索在 `retrieve` 里直接 `raise RetrievalEmpty`，
由 `services/qa.py` 接住转成 `error` 事件（§5.5 末段）。少一个节点，
就少一处"两条路径行为不一致"的可能 —— 节点里的 `raise` 与图外的 `raise` 走的是
同一套 `api/errors.py` 映射。

- `retrieve` 先跑一次预检索（构图时定下的那个 `Searcher`），**不是**让模型自己决定要不要检索。
  理由：`AGENTS.md` §5.2 要求"100% 实质性回答带来源"，
  把"要不要查文档"交给模型就是在赌它每次都愿意查。预检索保证"没有检索结果就明说没有"，
  模型只有在需要**更多**上下文时才动工具。
- `agent` 用 `model.astream(...)` 拿流，**自己聚合 chunk**（L6），不挂回调：
  聚合体 `AIMessage` 就是下一轮要回传的消息，同时也是 `finalize` 的产物。
- `tools` 节点自己写（`await t.ainvoke(tool_call)` + `ToolMessage`），
  **不用 `langgraph.prebuilt.ToolNode`**（`AGENTS.md` §1 与 #27 守护）。
  自己写才看得见"工具被调了几次、每次都往 `chunks` 里追加了什么"。
- `MAX_TOOL_ROUNDS` 到了就不再进 `tools`，直接 `finalize` 并在 answer 末尾追加
  "（已达到工具调用上限，可能有未覆盖的内容）"—— **宁可少说，不可编造**。

**为什么值得多写这几十行**：v2/v3 的核心是"工具循环 + 沙箱验证"，
本阶段不把循环写成可抄的模板，阶段七会重写一遍；而重写的时候
`reasoning_content` 会被再坑一次（见 D9 的判据）。

### D2 · 状态 schema 一处定义，检索引擎的对象**直接**放进去

`agent/state.py`（唯一）：

```python
class AppState(MessagesState):          # messages: Annotated[list[AnyMessage], add_messages]
    chunks: Annotated[list[RetrievedChunk], _overwrite]
    citations: Annotated[list[Citation], _overwrite]
    usage: Annotated[UsageSnapshot, _overwrite]
    tool_rounds: Annotated[int, _overwrite]
    answer: Annotated[str, _overwrite]

class QAState(AppState):
    question: Annotated[str, _overwrite]
```

- `_overwrite` 是 `agent/state.py` 里的**具名 reducer**（`def _overwrite(old, new): return new`），
  不是 `lambda`：打点与调试时能看见函数名，且 `mypy` 对具名函数更友好。
  默认 reducer 在 `TypedDict` 里根本没有（LangGraph 会按覆盖处理），
  但**显式写出来**才能让"这是每轮重置"成为可读的契约（D7）。
- **`RetrievedChunk` / `Citation` / `UsageSnapshot` 直接作为字段类型**（L3 已验证可往返）。
  代价是**必须有一条往返测试**（判据 6）—— 这个坑只在"恢复"时才炸，
  在"新会话"里永远看不见。
- **不把检索结果塞进 `messages`**。`messages` 只放对话（Human/AI/Tool），
  检索结果的权威是 `state["chunks"]`（L4：`ToolMessage.artifact` 过检查点会退化成 dict）。
- `usage` 是 `UsageSnapshot`（不可变、带 `Decimal`），每轮**覆盖**为当轮的值；
  多轮调用的合计在 `services/qa.py` 里做（它是唯一要对外报数的地方）。

### D3 · 工具的实现是纯函数，`@tool` 只出现在 `registry.py`

- `agent/tools/docs.py`：

  ```python
  async def search_docs(
      query: str, *,
      top_k: Annotated[int, InjectedToolArg] = DEFAULT_TOP_K,
      start_index: Annotated[int, InjectedToolArg] = 1,
      searcher: Annotated[Searcher | None, InjectedToolArg] = None,
  ) -> tuple[str, list[RetrievedChunk]]: ...
  async def read_doc(
      doc_url: str, *,
      fetcher: Annotated[Fetcher | None, InjectedToolArg] = None,
  ) -> str: ...
  ```

  两个都是**普通 `async def`**：可脱 LLM 单测、可被 `retrieve` 节点直接调用、
  可在测试里 monkeypatch。它们**不知道**自己会被包成工具 ——
  `InjectedToolArg` 只是"模型看不见这个参数"的声明，不是注册动作（完整规格见 §5.3）。
- **注入的是行为，不是 Chroma 句柄**：`Searcher` / `Fetcher` 是
  `Callable[[str, int], Awaitable[list[RetrievedChunk]]]` 这类函数类型，
  于是 `agent/` 层**不需要 import `chromadb`**（守护 #24 的白名单不动），
  而"用哪个库、哪份夹具"完全由构图方决定。这比注句柄更窄、也更好测。
- `agent/registry.py`：

  ```python
  SEARCH_DOCS = tool(search_docs, response_format="content_and_artifact")
  READ_DOC = tool(read_doc)                       # 默认 "content"
  TOOLS: Final[tuple[BaseTool, ...]] = (SEARCH_DOCS, READ_DOC)
  def tool_specs() -> list[dict[str, Any]]: ...   # [OI] tools 数组，唯一生成点
  ```

  `response_format="content_and_artifact"` 是刻意的，且**只给 `search_docs`**：
  `ToolMessage.content` 给模型看（带编号的片段文本），
  `ToolMessage.artifact` 给图看（原始 `RetrievedChunk` 列表，用来追加 `chunks`）。
  两者由**同一个**函数产出，不会漂移。
  `read_doc` 没有 artifact —— 它不产生引用编号（§5.3 已说明理由）。
- **工具名 == 函数名 == `__name__`**（L8 已实测 `.name` 与 `.coroutine` 都指向原文），
  不存在 `search_docs → do_search` 的映射层（`AGENTS.md` §2）。判据 9 钉死这一点。
- 工具描述（docstring）是**给模型看的 prompt**，必须写清"什么时候用"：
  `search_docs` = "当前上下文不足以回答时，再检索一次"；
  `read_doc` = "需要某页完整内容时按 URL 取整页"。
  描述文本同样受 §1 前缀缓存约束（字节稳定，不写时间戳）。

### D4 · 引用编号**只**由 `build_citations` 产生，且只追加不重排

- `retrieve` 节点：`chunks = await search_docs(question)` → `citations = build_citations(chunks)`。
- `tools` 节点：把新拿到的 chunk **追加**到 `state["chunks"]` 尾部，
  然后对**整个列表**再调一次 `build_citations` —— 已有条目的 `index` 不变
  （`index = position + 1`，追加在尾部），新条目接在后面。
- 给模型看的片段文本里带 `[n]`，`n` 来自同一个 `citations` 列表。
  **禁止**在模板里 `{{ loop.index }}` 自己编号（`qa_context.jinja2` 里拿到的
  就是 `citation.index`，模板只负责排版）。
- `finalize` 节点：`render_reference_block(citations)` 机械追加到答案之后 ——
  即使模型一个 `[n]` 都没写，来源块仍然在（判据 2 的兜底）。
- `find_ungrounded_markers(answer, citations)` **只判定不抛**（阶段五的设计）。
  本阶段的处置：**剥掉**越界标记 + 打 `qa.ungrounded_markers` 警告，
  **不重新生成**（重生成要再付一次钱，而剥标记已经让答案不再指向不存在的来源）。
  `MAX_GROUNDING_RETRIES` 因此在本阶段取 `0`，保留常量是为了阶段七的诊断图。

### D5 · SSE 用 `astream` + `get_stream_writer`，**在节点里**就筛好

- `stream_mode=["custom","updates"]`（L2 的二元组）。`custom` 是节点自己写的业务事件，
  `updates` **只用来取最终状态**（`answer` / `usage` 的兜底），**永不转发**。
  于是"一次运行 3000+ 事件只挑前端要的"是**在源头**做到的，不是在路由里猜类型。
- 事件类型（`agent/state.py` 里的 `QaEvent` TypedDict，或直接是 `dict`）：
  `token` / `citations` / `tool` / `error` / `done`。**没有别的类型**。
  加类型必须同时改 `services/qa.py` 与 `api/routers/chat.py`，且 `test_sse` 会红。
- 路由侧：

  ```python
  EventSourceResponse(
      stream, ping=0, sep="\n",
      headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
  )
  ```

  - `ping=0`：库自带心跳是 `ping - <utc now>`（L10），既不是契约里那个字面量，
    又带时间戳（不可断言）。关掉，自己发。
  - `sep="\n"`：库默认 `\r\n`，与 §2.4 的字节不一致（L11）。
  - `Cache-Control` 显式传 `no-cache`：库默认是 `no-store`（L9），
    两者语义不同（`no-store` 禁止存储，`no-cache` 允许存储但必须回源校验），
    契约写的是 `no-cache`，就传 `no-cache`。
  - `X-Accel-Buffering: no` 库已默认设置（L9），**仍然显式再传一次** ——
    因为哪天库换了默认值，这个失效是**静默**的（本地照样跑，上 Nginx 才一次性吐出）。
- 心跳：**自己起一个产生器**，用 `asyncio.Queue` + `wait_for(q.get(), timeout=H)` 的模式
  （已实测：超时 → 发 `: heartbeat\n\n` 并继续等，不取消生产者）。
  **不要**用 `asyncio.wait_for(anext(gen), timeout=...)` —— 那会**取消**异步生成器
  （实测打印出 `CANCELLED`），把正在跑的图干掉。
- 图在独立 task 里跑，事件经 `Queue` 交给响应生成器；task 结束时 `put(None)` 终止。

### D6 · "文档里没有"的仲裁顺序（流一旦开始就改不了状态码）

| 阶段 | 失败 | 处置 |
|---|---|---|
| 开流**前**（`POST /chat` 的 handler 里） | 索引没建 / 建错 | 调 `assert_index_ready()`：`CorpusMissing` → 503、`ConfigError` → 500，**走 §3.1 真信封**（此时还没开始流） |
| 流**内**（`retrieve` 节点） | 检索命中 0 条 | 节点抛 `RetrievalEmpty`；服务层捕获 → 发一条 `error` 事件（`code=RETRIEVAL_EMPTY`）→ 发 `done` → 结束。**不产出任何 token** |
| 非流式（`POST /knowledge`） | 检索命中 0 条 | 直接让 `RetrievalEmpty` 冒到 `api/errors.py` → **200 + `ok=true` + `data.status="empty"`** |

- 开流前的预检必须是**廉价**的：`assert_index_ready()` 只做
  "开集合 → 非空 → 索引元数据兼容"，**不嵌入、不查询**（不加载模型）。
  这一条很重要：`warm_up` 要拉 2GB 模型，绝不能放在"看索引在不在"这一步里。
- 流内出现 `CorpusMissing`（比如预检之后有人删了库）也只能发 `error` 事件 ——
  这已经是既成事实，前端按 `code` 区分展示。
- **`error` 事件的 `code` 必须取自 `ErrorCode` 枚举**（§3.2），`details` 走 `exc.details`。

### D7 · 每次 turn **重置**检索态

检查点会把上一轮的 `chunks / citations / tool_rounds / usage / answer` 带过来。
若 `retrieve` 节点是"追加"，同一 thread 的第二问就会带着上一问的片段回答 ——
既污染答案又抬成本。

- `retrieve` 节点返回**完整覆盖**：`chunks=新检索结果`、`citations=build_citations(新)`、
  `tool_rounds=0`、`usage=当轮初次检索的 usage`（检索不烧 token，故为零值快照）、
  `answer=""`；`messages` 用 `add_messages` **追加**一条 `HumanMessage`。
- `tools` 节点**只追加** `chunks`（并重算 `citations`）与 `tool_rounds += 1`。
- 判据 4 用"同一 thread 连问两轮"钉住这一点。

### D8 · 历史裁剪只在一个函数里

`agent/graphs/qa.py::_trim_history(messages, *, limit=MAX_HISTORY_MESSAGES)`：

- 目标：把老轮次裁掉，但**绝不在一组 `AIMessage(tool_calls) → ToolMessage*` 中间切**。
  切断了会让模型看到"调用了 `call_1` 却没有任何结果"，触发它的重试冲动（白烧钱）。
- 实现：从尾部往前数，**以 `HumanMessage` 为组边界**，凑满 `limit` 组就停；
  丢弃更早的组。每组的定义是 "Human → (AI, Tool*)…"，不完整就整组丢。
  尾部若停在 `AIMessage(tool_calls)` 之后、`ToolMessage` 之前，就把这一组也丢掉。
- **状态里没有 `SystemMessage`**，所以不需要"保留开头那一条"这种特例：
  系统提示在渲染时才拼上去（§5.5 的 `_render_model_messages`），
  于是"历史被裁了但 system 还在"这件事是**结构上**成立的，不靠裁剪函数记住它。
- 依据：`AGENTS.md` §2「检查点会膨胀」—— 老 `ToolMessage` 会同时撑爆上下文与检查点。

### D9 · 检索片段走**新模板**，`qa.jinja2` 一个字节都不动

- `qa.jinja2` 是**系统提示**，逐字节稳定才能吃前缀缓存（`AGENTS.md` §1）。
  本阶段**不改它**；片段与问题走 `qa_context.jinja2`，渲染成**一条 `HumanMessage`** 放最后。
- `qa_context.jinja2` 的三个约束：
  1. 只吃 `citations`（有 `index`）与 `question` 两个变量；
  2. 片段按 `citation.index` **升序**渲染（顺序固定，字节稳定）；
  3. 不写时间戳 / uuid / 未排序 dict（`test_prompt_stability.py` 会扫 `PROMPT_DIR.glob("*.jinja2")`）。
- 工具轮里 `search_docs` 返回的片段不走这个模板（那是 `ToolMessage.content`），
  由 `agent/tools/docs.py::render_chunks(chunks, *, start_index)` 渲染 ——
  **起点由节点算**（`len(chunks) + 1`），工具不自己编号，于是它渲染出的 `[n]`
  与 `build_citations` 的 `index` 恒等（D4 / §5.5 的 `tools` 节点）。
- **`qa_context.jinja2` 不引用 `output_language`**：语言由 `qa.jinja2` 末行定，
  两处都写就有两个来源。节点渲染片段块时只传 `citations` 与 `question`；
  实测同一次 `format_messages` 里多传 kwargs 会被静默忽略（jinja2 不在乎），
  所以"多传一个也不会报错"不是把变量留在模板里的理由 —— 这是本规格刻意收紧的一条。

### D10 · `/chat` 与 `/knowledge` 共用同一条服务路径

`services/qa.py`（唯一业务层）：

```python
@dataclass(frozen=True, slots=True)
class QAResult:
    answer: str
    citations: list[Citation]
    usage: UsageSnapshot
    status: Literal["ok", "empty"]

async def answer(question: str, *, graph: CompiledStateGraph, thread_id: str,
                 request_id: str) -> QAResult                      # 非流式
async def stream_answer(question: str, *, graph: CompiledStateGraph, thread_id: str,
                        request_id: str) -> AsyncIterator[QaEvent]  # 流式
```

（模型**不在这里传** —— 它在 `build_qa_graph()` 时就绑进图里了，D11。
`request_id` 只用来填 `done` 事件，不参与任何业务判断。）

- `answer()` 内部就是"把 `stream_answer()` 消费完，拼回 `QAResult`"，
  **不是**另一套 `ainvoke`。理由：两条路径若各走一遍，`reasoning_content`、
  引用编号、空检索处置就会悄悄分叉，而测试只会覆盖其中一条。
- 两个 router **只做协议翻译**：`/knowledge` 把 `QAResult` 套进 `Envelope[QAData]`；
  `/chat` 把事件映射成 SSE 字节。**任何业务判断都不在 router 里**。

### D11 · 图在 lifespan 编译一次，经 `app.state` + `deps.py` 取

```python
def create_app(
    *,
    database_url: str | None = None,
    rate_limit_enabled: bool = True,
    chat_model: BaseChatModel | None = None,
    search: Searcher | None = None,
    fetch: Fetcher | None = None,
    preflight: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI
```

- 五个新参数全带默认 `None`：阶段四的 `client` 夹具与 `main.py` 的调用点
  **一行都不用改**。
- **为什么是三个可调用对象，不是 `collection=` / `embedder=` 两个句柄**：
  ① `agent/` 层不该 `import chromadb`（守护 #24 的白名单只有三个 rag 文件，§5.16 #32）；
  ② 句柄必须**配对**才有意义，拆成两个参数就是把配错的机会交给调用方。
- `chat_model`：`None` 时 lifespan 调 `get_chat_model()`；
  无 API key 时**不崩**，置 `app.state.qa_graph = None`，`/chat` 与 `/knowledge`
  抛 `ConfigError`（500 信封）—— 与 `/health` 能用、问答不能用 的降级预期一致。
- `search` / `fetch` / `preflight`：**只为测试而存在**，原样转交 `build_qa_graph(...)`。
  它们必须是**同一组**（同一份夹具集合建的库、同一个哈希 embedder）——
  否则"预检说索引兼容、检索却漂移"这种事只在测试里发生，而生产测试不出来。
  所以不拆成 `collection=` / `embedder=` 两个参数让调用方自己配对，而是让
  **构图方负责配对**：测试夹具传 `search`/`fetch`/`preflight` 三个已经绑好的可调用对象。
  三个**必须同时给或同时不给**；半套在 `build_qa_graph` 里 `ConfigError`（§5.5 `_resolve_deps`）。
- lifespan 顺序：`configure_logging` → `open_engine` → `open_checkpointer` →
  `build_qa_graph(checkpointer=..., model=..., search=..., fetch=..., preflight=...)`
  → `yield` → 逆序关闭。
  测试传 `chat_model=ScriptedChatModel(...)` + 三个可调用对象，于是**全链路离线**。
- `open_checkpointer` 必须在 lifespan 内（`AsyncSqliteSaver.__init__` 要 running loop）。
- `deps.py` 追加 `get_qa_graph(request)`：取不到就用 `ConfigError`，
  **不在这里现编一个图**（§2 单点所有权）。

---

## 5. 关键文件规格

### 5.1 `config.py` 增量

```python
# ---------- 阶段六：图与流式（§5）----------
SSE_HEARTBEAT_SECONDS: Final = 15
"""心跳间隔。`AGENTS.md` §2 点名"每 ~15 秒一次"，故它是**契约值**，不是调优旋钮。
测试把它 monkeypatch 到 0.05 秒（D5：库自带心跳不可测，所以必须自己发）。"""

MAX_TOOL_ROUNDS: Final = 3
"""一次问答里最多几轮工具调用。到顶就 finalize，并在答案末尾声明"可能有未覆盖内容"。
取 3 而不是 5：`retrieve` 已经先检索过一次，再 3 轮已覆盖"再查一次 / 读整页 / 补一次"。"""

MAX_HISTORY_MESSAGES: Final = 12
"""喂给模型的历史消息上限（**消息条数**，不是轮数）。裁剪按 `HumanMessage` 分组（D8）。"""

MAX_GROUNDING_RETRIES: Final = 0
"""引用越界时的重生成次数。本阶段取 0（只剥标记，不重生成），常量留给阶段七的诊断图（D4）。"""

READ_DOC_MAX_CHARS: Final = 8_000
"""`read_doc` 回给模型的整页文本上限。一页文档几万字符，全塞进上下文会顶掉检索片段，
也会让检查点膨胀（`AGENTS.md` §2）。超限截断并声明"已截断"。"""

QA_PROMPT_NAME: Final = "qa"
QA_CONTEXT_PROMPT_NAME: Final = "qa_context"

OUTPUT_LANGUAGE: Final = "中文"
"""`qa.jinja2` 末行 `回答语言：{{ output_language }}` 的取值。**只在这里写一次** ——
阶段三起 `test_prompt_stability.py` 就一直在传 `"中文"`，但生产路径上没有来源；
本阶段图第一次真正渲染这个模板（§5.5 的 `_system_message()`），
所以必须有这个常量，否则节点里会写魔法字符串、且与测试里的字面量各说各话。"""
```

`RETRIEVAL_MIN_SCORE` **不在本文件里硬改**：跑分之后按 §10.2 回填，并同步改它那段
docstring（现在写的是"阈值必须等阶段六跑分后回填"）。

### 5.2 `agent/state.py`

```python
"""★ 唯一图状态 schema（AGENTS.md §2）。每张图都从这里扩展，禁止各图自定义互不兼容的 State。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import MessagesState

from pyassist.llm.tracing import UsageSnapshot
from pyassist.rag.citations import Citation
from pyassist.rag.retriever import RetrievedChunk

QaEventType = Literal["token", "citations", "tool", "error", "done"]


class QaEvent(TypedDict, total=False):
    """节点写给 `get_stream_writer()` 的事件。**这是图对外的唯一出口形状**（D5）。"""

    type: QaEventType
    text: str                          # token
    citations: list[dict[str, Any]]     # citations：已序列化的形状，路由不再加工
    phase: str                          # tool: "start" | "end"
    name: str                           # tool
    code: str                           # error（取自 ErrorCode）
    message: str                        # error
    details: dict[str, Any] | None      # error
    request_id: str                     # done
    usage: dict[str, Any]               # done


def _overwrite[T](old: T, new: T) -> T:
    """每轮**覆盖**而非累积（D2 / D7）。具名函数而非 lambda：栈里看得见名字。"""
    return new


class AppState(MessagesState):
    """所有图共享的底座。`messages` 之外的一切都是"本轮的中间产物"。"""

    chunks: Annotated[list[RetrievedChunk], _overwrite]
    citations: Annotated[list[Citation], _overwrite]
    usage: Annotated[UsageSnapshot, _overwrite]
    tool_rounds: Annotated[int, _overwrite]
    answer: Annotated[str, _overwrite]


class QAState(AppState):
    question: Annotated[str, _overwrite]
```

**为什么继承 `MessagesState` 而不是自己写 `messages` 注解**：`messages` 的 reducer
（`add_messages`）是本项目唯一不能写错的 reducer —— 它负责给消息补 id、
把 `AIMessage(tool_calls)` 与 `ToolMessage` 正确配对。抄写一份注解等于抄一份
出错的可能。继承之后 `AppState.__annotations__` 实测是
`['messages', 'chunks', ..., 'answer']`（L1 已验），键集合是 `MessagesState` 的超集 ——
判据 6 断言这一点，于是"每张图各定义一个 State"这件事在 CI 里不可发生。

其中关键一条：`chunks` / `citations` / `usage` 里的元素是检索层与记账层的**原始对象**，
不是 dict。L3 已实测它们在检查点里能原样还原（含 `Decimal`）—— 这条性质
**必须**用往返测试钉住，否则这是个"新会话永远正常、恢复才炸"的坑。

### 5.3 `agent/tools/docs.py`

```python
"""`search_docs` / `read_doc` 的**实现**（纯 async 函数，不含 `tool(...)` 工厂）。

`tool(...)` 只在 `agent/registry.py`（AGENTS.md §2「定义工具」）。
本文件只 import `InjectedToolArg` 这个**标记类型**，不 import `tool`：
前者是"这个参数由节点注入、模型看不见"的声明，后者才是"注册工具"的动作。
于是函数能被任何地方直接 await，"检索能不能用"与"LLM 会不会调"是两件独立可测的事。

**注入的是检索*行为*（`Searcher` / `Fetcher`），不是 Chroma 句柄。**
理由：`agent/` 层不该 import `chromadb`（守护 #24 的 `CHROMADB_ALLOWED` 只有三个 rag 文件，
而"给类型注解开个白名单口子"会让那条守护从此挡不住真东西）。
注入一个可调用对象后，`agent/` 对"检索怎么实现"零知识，测试也照样能塞夹具。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated

from langchain_core.tools import InjectedToolArg

from pyassist.config import DEFAULT_TOP_K, READ_DOC_MAX_CHARS
from pyassist.logging import get_logger
from pyassist.rag import retriever as retriever_module
from pyassist.rag.retriever import RetrievedChunk

logger = get_logger(__name__)

Searcher = Callable[[str, int], Awaitable[list[RetrievedChunk]]]   # (query, top_k)
Fetcher = Callable[[str], Awaitable[list[RetrievedChunk]]]         # (doc_url,)


async def _default_search(query: str, top_k: int) -> list[RetrievedChunk]:
    """生产默认：走 `retriever` 的全局依赖（真实向量库 + 真实嵌入模型）。"""
    return await retriever_module.retrieve(query, top_k=top_k)


async def _default_fetch(doc_url: str) -> list[RetrievedChunk]:
    return await retriever_module.fetch_document(doc_url)


def render_chunks(chunks: Sequence[RetrievedChunk], *, start_index: int) -> str:
    """把片段渲染成**带编号**的文本。纯函数，**起点由调用方给**（D4）。

    编号规则与 `rag/citations.build_citations` 逐字同构：`index = 全局位置 + 1`。
    `chunks` 在状态里**只追加**，所以 `start_index = len(已有 chunks) + 1`
    让"这里渲染出的 `[7]`"与"`citations[6]`"恒等 —— 不需要第二个编号来源。
    """
    return "\n\n".join(
        f"[{start_index + offset}] {chunk.title}"
        + (f" — {chunk.section}" if chunk.section else "")
        + f"\n{chunk.text}"
        for offset, chunk in enumerate(chunks)
    )


async def search_docs(
    query: str,
    *,
    top_k: Annotated[int, InjectedToolArg] = DEFAULT_TOP_K,
    start_index: Annotated[int, InjectedToolArg] = 1,
    searcher: Annotated[Searcher | None, InjectedToolArg] = None,
) -> tuple[str, list[RetrievedChunk]]:
    """检索 Python 文档，返回 `(带编号的可见文本, 原始片段)`。无命中返回 `("", [])`。

    - `query` 是**唯一**模型可见的参数（L15：其余三个都是 `InjectedToolArg`，
      不出现在 schema 里，由 `tools` 节点写进 `tool_call["args"]`）。
    - `start_index` 由节点按"这批片段的全局首号"算好传来 —— **工具自己不编号**，
      它只把节点给的起点往后排（D4）。
    - `searcher` 是节点注入的检索行为（§5.5 `_resolve_deps` 选定的那一个），
      与 `retrieve` 节点**同一个可调用对象**，于是工具轮与首轮检索走同一条路径。
      不注入时落到本模块的 `_default_search`，也就是生产路径。
    - 空串直接返回 `("", [])`（不打向量库：空查询会嵌入出零向量，cosine 距离退化为
      1.0，等于随机返回 5 条 —— 那比"没有"更危险）。
    """
    if not query.strip():
        return "", []
    chunks = await (searcher or _default_search)(query, top_k)
    return render_chunks(chunks, start_index=start_index), chunks


async def read_doc(
    doc_url: str,
    *,
    fetcher: Annotated[Fetcher | None, InjectedToolArg] = None,
) -> str:
    """按 URL 取整页文本（按 `ordinal` 升序拼接），超 `READ_DOC_MAX_CHARS` 截断。

    入参可带锚点，`retrieve.fetch_document` 内部会剥掉（阶段五已定型）。
    返回**字符串**而不是 chunk 列表：这个工具是给模型"补上下文"的，**不产生新引用编号** ——
    一页可能几十个 chunk，全塞进 `citations` 会撑爆来源块，而 grounding 的依据
    仍是 `search_docs` 给过编号的那些片段。所以它在 registry 里用 `response_format`
    默认的 `"content"`，**没有** artifact。
    """
    chunks = await (fetcher or _default_fetch)(doc_url)
    if not chunks:
        return f"文档中没有可读取的内容：{doc_url}"
    text = "\n\n".join(chunk.text for chunk in chunks)
    if len(text) > READ_DOC_MAX_CHARS:
        return text[:READ_DOC_MAX_CHARS] + "\n\n（已截断，只保留前段内容）"
    return text
```

**签名就是工具 schema**（参数名、类型、默认值都进 `convert_to_openai_tool`），
所以要分清"模型能填的"与"节点注入的"：

| 参数 | 模型可见 | 手段 |
|---|---|---|
| `query` / `doc_url` | ✅ | 普通参数 |
| `top_k` / `start_index` / `searcher` / `fetcher` | ❌ | `Annotated[..., InjectedToolArg]` |

**只有 `InjectedToolArg` 能把参数从 schema 里藏掉**（L15）。早期草案写的是
"keyword-only 参数不会进 schema" —— 实测**不成立**：`def f(query, *, top_k=5)`
生成的 schema 里 `top_k` 照样在（`{"properties": {"query": ..., "top_k": {...}}}`）。
实测加了 `InjectedToolArg` 之后是 `{"properties": {"query": {"type": "string"}},
"required": ["query"]}`，且 `SEARCH_DOCS._injected_args_keys == {"top_k",
"start_index", "searcher"}`。

注入值通过 `tool_call["args"]` 传：`{**tc["args"], "start_index": n, ...}`；
不注入时走参数默认值 —— 于是**生产不需要注入**（默认走全局向量库与真模型），
测试注入夹具。`start_index` 缺省为 1，生产路径下 `tools` 节点**总是**显式给值。

**`InjectedToolArg` 不是"安全边界"**：多余/未知的 args 键会被 pydantic 静默丢弃
（实测：传 `{"q": "x", "bogus": 1}` 不报错）。所以节点必须**按工具名分别组装**
注入字典（§5.5），不能一套塞给所有工具 —— 静默丢键正是最难查的那类 bug。

### 5.4 `agent/registry.py`

```python
"""★ 唯一工具定义点（AGENTS.md §2）。`tool(...)` 只在这个文件出现。

**工具名 == 实现名 == `__name__`**（§2「不允许 search_docs → do_search 这种映射层」）。
后者由 `test_registry.py` 用 `.coroutine.__name__` 断言（L8 实测该属性指向原函数）。
"""

from __future__ import annotations

from typing import Any, Final

from langchain_core.tools import BaseTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool

from pyassist.agent.tools.docs import read_doc, search_docs

SEARCH_DOCS: Final = tool(search_docs, response_format="content_and_artifact")
READ_DOC: Final = tool(read_doc)          # 默认 "content"：read_doc 不产生引用（§5.3）

TOOLS: Final[tuple[BaseTool, ...]] = (SEARCH_DOCS, READ_DOC)

_SPECS: Final = [convert_to_openai_tool(t) for t in TOOLS]


def tool_specs() -> list[dict[str, Any]]:
    """传给 `model.bind_tools()` 的 OpenAI 风格 tools 数组。

    **唯一生成点** —— 需要工具的节点调它，不许自己手搓 JSON Schema（`AGENTS.md` §1）。
    返回**拷贝**：调用方（`bind_tools`）可能改写，别让缓存被污染。
    """
    return [dict(spec) for spec in _SPECS]


def get_tool(name: str) -> BaseTool:
    """按名字取工具。`tools` 节点用它把 `tool_call["name"]` 映射回可调用的对象。"""
    for candidate in TOOLS:
        if candidate.name == name:
            return candidate
    raise KeyError(f"未注册的工具：{name!r}")
```

**`response_format="content_and_artifact"` 是核心**（L16）：`search_docs` 返回
`(str, list[RetrievedChunk])`，LangChain 把它变成
`ToolMessage(content=str, artifact=list[...])`。
`content` 给模型（`render_chunks` 出的带 `[n]` 编号的片段），
`artifact` 给图（原始 chunk，追加进 `state["chunks"]` 后重算 `citations`）。
两者同一次调用产出，编号不会漂移（D4）。

`read_doc` **不带 artifact**：它返回裸 `str`，`tool(read_doc)` 走默认 `"content"`。
判据 9 断言 `SEARCH_DOCS.response_format == "content_and_artifact"` 且
`READ_DOC.response_format == "content"` —— 两条都要钉住，
"给所有工具都加 artifact"是个看起来更整齐、实则把 `read_doc` 的整页文本
灌进状态的错误。

### 5.5 `agent/graphs/qa.py`

**节点与边**（D1）。完整签名：

```python
@dataclass(frozen=True, slots=True)
class QAGraph:
    """编译好的图 + 与它同源的检索上下文。**两者必须一起走**，见下。"""

    graph: CompiledStateGraph
    preflight: Callable[[], Awaitable[None]]


def build_qa_graph(*, checkpointer: BaseCheckpointSaver[str] | None,
                   model: BaseChatModel | None = None,
                   search: Searcher | None = None,
                   fetch: Fetcher | None = None,
                   preflight: Callable[[], Awaitable[None]] | None = None) -> QAGraph: ...


def _resolve_deps(
    search: Searcher | None, fetch: Fetcher | None,
    preflight: Callable[[], Awaitable[None]] | None,
) -> tuple[Searcher, Fetcher, Callable[[], Awaitable[None]]]:
    """三个要么全给、要么全不给。混着给在**构图时**就炸，不等开流才炸（见下）。"""
    injected = (search is not None, fetch is not None, preflight is not None)
    if len(set(injected)) > 1:
        raise ConfigError("search / fetch / preflight 必须同时注入或同时省略")
    if search is not None and fetch is not None and preflight is not None:
        return search, fetch, preflight
    return _default_search, _default_fetch, assert_index_ready   # 生产：全局依赖
```

- `model=None` → 用 `llm.client.get_chat_model()`（生产路径）。
- **三个可调用参数是一组，不许只给一半。** 生产三个都 `None` → 全部落到 `rag/` 的全局依赖；
  测试三个都给 → 全部绑到夹具集合与 `HashingEmbedder`。混着给（例如只给 `search`
  不给 `preflight`）在**构图时**就 `ConfigError`，而不是等到某次开流才出现
  "预检说索引兼容、检索却漂移" —— 那种错只在测试里发生，生产测试不出来。
- 判据：`search is not None` 与 `fetch is not None` 与 `preflight is not None`
  **三者同真同假**，由 `test_graph_qa.py` 的一条参数化用例钉住（三种半套组合都 `ConfigError`）。
- **`preflight` 与 `graph` 打包在同一个对象里，不是两件事，理由与上一段相同**：
  节点里的检索用的是注入的那一组，开流前预检（D6）必须用**同一组**
  （否则拿 `settings.embedding_model_id` 去校验一个 `test-hashing-64` 建的集合就会
  `ConfigError`）。所以不是"图里一套、路由里另一套"，而是图把两者一起交出来。

**`retrieve` 节点也复用同一个 `Searcher`**：它调的不是全局 `search_docs(question)`，
而是构图时定下的那个可调用对象：

```python
searcher = search or _default_search                       # 构图时定死
chunks = await searcher(question, DEFAULT_TOP_K)
```

`agent/tools/docs.py` 的 `search_docs` 用的是**同一个** `_default_search`
（它在 `searcher=None` 时自己兜底，§5.3）。所以首轮检索与工具轮检索是同一条代码路径，
不是两套实现 —— 差别只有"谁传 `start_index`"。

模块头部因此有这四行 import（**`agent/` 不 import `chromadb`**，§5.16 #32）：

```python
from pyassist.agent.registry import get_tool, tool_specs
from pyassist.agent.tools.docs import Fetcher, Searcher, _default_fetch, _default_search
from pyassist.rag.retriever import assert_index_ready
from pyassist.config import DEFAULT_TOP_K
```

`_default_search` / `_default_fetch` 带下划线却跨模块 import —— 刻意如此：
它们是"生产默认"这一**唯一实现**，复制一份到 `qa.py` 就等于开第二条检索路径
（§2 单点所有权）。改名成 `default_search` 也可以，但**不许两个名字并存**。

节点一览：

| 节点 | 输入 | 做的事 | 产出 |
|---|---|---|---|
| `retrieve` | `question` | `await searcher(question, DEFAULT_TOP_K)`（构图时定下的那一个）；空 → `raise RetrievalEmpty(query=question)`；否则 `build_citations` | 覆盖 `chunks/citations/tool_rounds/usage/answer`（D7）；追加 `HumanMessage(question)`；写 `citations` 事件 |
| `agent` | `messages` + `chunks` | `_render_model_messages()`：`_trim_history` → `_context_template()` 用 `citations` 渲染片段 → 追加为**最后一条** `HumanMessage`（不进状态）→ `model.bind_tools(tool_specs())` → `astream` 聚合 | 写 `token` 事件；追加 `AIMessage`；`usage` 覆盖为当轮聚合值 |
| `tools` | 最后一条 `AIMessage.tool_calls` | 逐个 `await get_tool(tc["name"]).ainvoke(_tool_call_with_injection(tc, ...))` —— **保留 `ToolCall` 形状**（L18） | 追加 `ToolMessage`；`chunks += artifact`；`citations = build_citations(全部)`；`tool_rounds += 1`；写 `tool` 与 `citations` 事件 |
| `finalize` | `messages` + `citations` | `answer = 最后一条 AIMessage.content`；剥越界标记（`MAX_GROUNDING_RETRIES=0`，D4）；`render_reference_block` 追加 | `answer`（**不写 `done`**） |

**`done` 事件不由节点产生**，由 `services/qa.py::stream_answer` 在 `finally` 里发（§5.7）：
它要覆盖"图正常跑完"与"图抛异常"两条路径，而节点只在正常路径上存在。
让 `finalize` 发 `done` 就等于异常路径上没有 `done`，而 §2.4 第 2 条要求**每条流**都有。
所以 `QaEventType` 里有 `done`（形状统一），但 writer 侧只有服务层用得到它。

`tools` 节点里 `start_index` 的取值是**唯一需要小心的一行**：它从
`len(state["chunks"]) + 1`（即下一批片段的全局首号）起算，而不是 1。
这样 `search_docs` 渲染出的 `[n]` 与随后 `build_citations(全部 chunks)` 的
`index` 逐一对齐 —— 两者都从"全局位置 + 1"来（§5.3 的 `render_chunks`）。

`read_doc` **没有** `start_index`（它不产引用）。多传这个键给 `read_doc` 会怎样？
实测**不会报错，会被静默丢掉**（`args_schema` 由签名生成，未知键在
`model_validate` 时被忽略）。静默丢掉正是本项目最不想要的失败形态，所以节点
**按工具名分别组装**注入字典，而不是一套塞给所有工具：

```python
def _tool_call_with_injection(
    tc: dict[str, Any], *, next_index: int, searcher: Searcher, fetcher: Fetcher
) -> dict[str, Any]:
    """把注入参数并进 `tc["args"]`，**但保持 `ToolCall` 形状**（L18）。

    ⚠️ **不能**只传 `{**tc["args"], ...}` —— 那会让 `ainvoke` 返回裸 `str`，
    `ToolMessage.artifact` 与 `tool_call_id` 全部消失（实测）。必须原样带上
    `name` / `id` / `type` 三个键。
    """
    args = dict(tc["args"])
    if tc["name"] == "search_docs":
        args.update(start_index=next_index, searcher=searcher)
    else:                                              # read_doc
        args["fetcher"] = fetcher
    return {**tc, "args": args}
```

`next_index` 由 `tools` 节点在循环里**递增**，不是每条都取 `len(chunks) + 1`：

```python
next_index = len(state["chunks"]) + 1
for tc in tool_calls:
    message = await get_tool(tc["name"]).ainvoke(
        _tool_call_with_injection(tc, next_index=next_index, searcher=searcher, fetcher=fetcher)
    )
    if not isinstance(message, ToolMessage):           # L18：形状错了这里就炸，别等下游
        # 不用 assert：ruff 的 S101 在 src/ 里是开的（pyproject 只给 tests/ 豁免）
        raise LLMBadResponse(
            f"工具 {tc['name']!r} 返回了非 ToolMessage 对象：{type(message).__name__}",
            details={"tool_name": tc["name"]},
        )
    next_index += len(message.artifact or [])          # 只有 search_docs 有 artifact
```

**为什么不写 `len(chunks) + 1`**：并行 `tool_calls` 是 DeepSeek 支持的（§1），
两个 `search_docs` 同时返回时，若各自都取"当前长度 + 1"，它们拿到**同一个起点**，
编号当场重叠 —— 而 `build_citations` 会把它们当成两个不同片段，序号重复但内容不同，
引用块里会出现两个 `[6]` 指向两页文档。递增游标是唯一不会撞号的写法。

模型若返回一个**没注册的工具名**（绑定 tools 后正常不会发生，但 `strict` 不是万能的），
`get_tool` 抛 `KeyError`；`services/qa.py` 的 `except (AppError, KeyError)` 把它转成
`LLMBadResponse`（502）—— 这与"tool_calls JSON 解析失败"属同一类上游畸形响应，
不是我们这边的错。**节点里不 try/except**：错误映射只在服务层一处（§5.7）。

`finalize_empty` 不是节点（与 D1 的图一致）—— 空检索直接在 `retrieve` 里 `raise`，
由服务层接住发 `error`。少一个节点就少一处"两条路径行为不一致"的可能。

`agent` 的聚合与流式（L6/L7）：

```python
aggregate: AIMessageChunk | None = None
async for piece in bound.astream(messages):        # 绝不用同步 .stream()
    aggregate = piece if aggregate is None else aggregate + piece
    if piece.content:
        writer({"type": "token", "text": str(piece.content)})
reply = _to_ai_message(aggregate)                   # AIMessageChunk → AIMessage（保留 reasoning_content）
```

`_to_ai_message` 必须把 `additional_kwargs["reasoning_content"]` 带过去 ——
**这就是 `ChatDeepSeekPatched` 的出站补丁在流式路径上的落点**（L6 实测首块带该字段）。
判据 5 专门测"连续两轮工具调用后第三轮 payload 仍带 `reasoning_content`"。

**送给模型的 `messages` 怎么拼**（这一段必须写死，否则前缀缓存与历史裁剪都会走形）：

```python
def _system_message() -> SystemMessage:
    """`qa.jinja2` 渲染成的系统提示。**逐字节稳定**，每轮都一样（前缀缓存，§1）。"""
    rendered = load_prompt(QA_PROMPT_NAME).format_messages(
        history=[], output_language=OUTPUT_LANGUAGE
    )
    return cast("SystemMessage", rendered[0])


@cache
def _context_template() -> ChatPromptTemplate:
    """`qa_context.jinja2` 的**独立加载器**。**不走 `load_prompt()`**，理由见下。"""
    path = PROMPT_DIR / f"{QA_CONTEXT_PROMPT_NAME}.jinja2"
    return ChatPromptTemplate.from_messages(
        [("human", path.read_text(encoding="utf-8"))], template_format="jinja2"
    )


def _render_model_messages(messages, citations, question) -> list[BaseMessage]:
    """拼出**这一轮**要发给模型的消息序列（不写回状态）。

    `[SystemMessage] + trimmed_history[:-1] + [HumanMessage(渲染后的片段块)]`
    —— 用渲染块**替换**最后那条裸提问，不是追加。理由三条：
    1. 追加会让模型连看两条 human，且问题在状态里出现两次；
    2. 渲染块只在这一轮存在，**不进状态** —— 否则检查点里每轮多存一份全文（R5）；
    3. 变长的片段块永远在**最后**，system + 老历史逐字节稳定 → 前缀缓存照常命中（§1）。
    """
    trimmed = _trim_history(messages, limit=MAX_HISTORY_MESSAGES)
    if not isinstance(trimmed[-1], HumanMessage):
        return [_system_message(), *trimmed]    # 工具轮：最后一条是 ToolMessage，原样发
    context = _context_template().format_messages(citations=citations, question=question)
    return [_system_message(), *trimmed[:-1], *context]
```

- `qa.jinja2` 经通用 `load_prompt()` 加载（它带 `MessagesPlaceholder("history")`），
  传 `history=[]` 拿到恰好一条 `SystemMessage`。
- **`qa_context.jinja2` 不能走 `load_prompt()`（L17，实测）**：那个加载器把模板
  当 **`system` 角色**并**追加一个必填的 `MessagesPlaceholder("history")`**。
  于是 `load_prompt("qa_context").format_messages(citations=..., question=...)`
  会直接 `KeyError: 'history'`；补上 `history=[]` 之后拿到的是**一条 `SystemMessage`** ——
  第二个 system 会插到历史中间，前缀缓存的前提（system 恒在最前）当场破裂。
  所以片段块用上面那个 8 行的独立加载器，**唯一区别是角色 `human`、不带 placeholder**。
  实测 `("human", text)` + `format_messages(citations=..., question=...)`
  → 恰好一条 `HumanMessage`。
- `output_language` 只喂给 `qa.jinja2`（`config.OUTPUT_LANGUAGE`，唯一来源）；
  片段块**不传**它 —— 语言只能有一个来源。
- **只有最后一条是 `HumanMessage` 时才渲染**：工具轮的第二次 `agent` 调用要原样
  发出 `AIMessage(tool_calls) → ToolMessage`，再塞一遍片段块会让模型重复看到同批证据。
- `SystemMessage` **不写回状态**：它由 `_system_message()` 每次现渲染，字节恒定。
  这样"system 永远在最前"是结构事实，不依赖裁剪函数记得保留它（D8 末条）。

### 5.6 `rag/retriever.py` 增量：`assert_index_ready()`

```python
async def assert_index_ready(
    *,
    collection: Collection | None = None,
    embedder: Embedder | None = None,
) -> None:
    """**开流前的廉价预检**（D6）：开集合 → 非空 → 索引元数据与当前配置兼容。

    三件事都不碰模型：不嵌入、不查询。于是它能在 SSE 还没开始吐字节之前跑完，
    把 `CorpusMissing`（→503）与 `ConfigError`（→500）变成**真信封**而不是流内事件。

    **`async def` 且内部 `to_thread`**，与 `retrieve()` 完全同构 —— 因为
    `open_collection()` 会构造 Chroma 客户端（同步阻塞）。不给调用方留
    "顺手同步调一下"的机会：一个 sync 函数在 async 路由里跑，正是 §2 流式铁律
    要防的那种阻塞。

    期望的模型 ID 与 `_search` 同一规则：注入 `embedder` 就用它的 `model_id`，
    否则用 `settings.embedding_model_id`。**少了这条，注入 fake embedder 的测试
    会全部撞 `ConfigError`** —— 夹具集合是用 64 维哈希建的，与 bge-m3 不同源。
    """
    return await asyncio.to_thread(_assert_index_ready, collection=collection, embedder=embedder)


def _assert_index_ready(*, collection: Collection | None, embedder: Embedder | None) -> None:
    target = collection or store_module.open_collection()
    if collection is not None:
        store_module.assert_not_empty(target)
    expected_model = embedder.model_id if embedder is not None else settings.embedding_model_id
    store_module.assert_index_compatible(target, model_id=expected_model)
```

这是**本阶段对阶段五唯一的改动**（§9.1）：它是一个薄组合，
放在 `retriever.py` 是因为 §2 把"读向量库"整体划给了它 ——
放在 `api/routers/chat.py` 就等于在路由里直接碰 Chroma（守护 #24 会红）。

### 5.7 `services/qa.py`

```python
@dataclass(frozen=True, slots=True)
class QAResult:
    answer: str
    citations: list[Citation]
    usage: UsageSnapshot
    status: Literal["ok", "empty"]


async def stream_answer(
    question: str, *, graph: CompiledStateGraph, thread_id: str, request_id: str
) -> AsyncIterator[QaEvent]:
    """跑图，产出**已筛好**的事件（D5）。异常在这里被翻译成 `error` + `done`。

    - `graph.astream(..., stream_mode=["custom","updates"])`（L2）；
    - 只转发 `custom`；`updates` 仅用于兜底取 `answer` / `usage`；
    - `AppError` / `KeyError` → `error` 事件（`code=exc.code`，`details=exc.details`）；
    - **任何**其它异常 → `INTERNAL_ERROR` 的 `error` 事件（L20，不泄露原文）；
    - 任何路径都以 `done` 收尾（§2.4 第 2 条）。
    """

async def answer(
    question: str, *, graph: CompiledStateGraph, thread_id: str, request_id: str
) -> QAResult:
    """把 `stream_answer()` 消费完，拼回结果（D10）。**不是**另一套 `ainvoke`。"""
```

`stream_answer()` 的骨架（异常 → `error` + `done` 的**唯一**落点）：

```python
_EMPTY_USAGE: Final = UsageSnapshot(
    prompt_tokens=0, completion_tokens=0, cached_tokens=0,
    cost_usd=Decimal("0"), is_off_peak=True,
)

async def stream_answer(question, *, graph, thread_id, request_id) -> AsyncIterator[QaEvent]:
    config = {"configurable": {"thread_id": thread_id}}
    usage = _EMPTY_USAGE
    try:
        async for mode, payload in graph.astream(
            {"question": question}, config, stream_mode=["custom", "updates"]
        ):
            if mode == "custom":
                yield cast("QaEvent", payload)          # 节点已经筛过（D5）
            elif mode == "updates":
                usage = _usage_from_updates(payload) or usage   # 只兜底，不转发
    except (AppError, KeyError) as exc:                 # KeyError：未注册的工具名（§5.5 末段）
        yield _error_event(exc)
    except Exception:                                   # L20：**必须**有这一支，见下
        logger.exception("qa.stream_crashed", request_id=request_id)
        yield {"type": "error", "code": ErrorCode.INTERNAL_ERROR.value,
               "message": "服务内部错误，请重试", "details": None}
    yield {"type": "done", "request_id": request_id, "usage": _usage_dict(usage)}


def _error_event(exc: AppError | KeyError) -> QaEvent:
    """`AppError` → 原样翻译；`KeyError` → `LLMBadResponse`。两条路都产出 `error` 事件。"""
    if isinstance(exc, KeyError):
        tool_name = exc.args[0] if exc.args else ""      # str(KeyError("x")) 会带引号
        exc = LLMBadResponse(
            f"模型调用了未注册的工具：{tool_name}", details={"tool_name": tool_name}
        )
    return {"type": "error", "code": exc.code.value,
            "message": exc.message, "details": exc.details}
```

- **`done` 在 `try/except` 之后发，不在 `finally` 里**（L19，实测）：在 `finally` 里
  `yield` 会让异步生成器在**客户端断开**时抛
  `RuntimeError: async generator ignored GeneratorExit` —— 断开是常态（前端在 `done`
  之后主动关连接，`_sse_stream` 的 `finally` 也会 `task.cancel()`），所以这是必然踩的坑。
  写成"正常路径 + 每个 `except` 之后"都走到同一句 `yield done`，就同时满足
  §2.4 第 2 条（每条流都有 `done`）与"断开时干净退出"。
- **`except Exception` 这一支不是"兜底吞异常"，是 §2.4 第 2 条的硬要求**（L20，实测）：
  缺了它，任何非 `AppError` 的异常（`TypeError`、`ValueError`、检查点反序列化失败、
  第三方库的 `RuntimeError`）都会让生成器**直接结束**，`_pump` 的 `async for` 收到
  `StopAsyncIteration` 就正常返回 —— 路由侧看到的是"图跑完了"，于是客户端拿到一个
  **既没有 `error` 也没有 `done` 的半截流**。前端会把它误判成断线并重连，而重连会
  重跑整张图（§2.4 第 1 条警告的正是这件事，等于重复计费）。
  所以这一支是**唯一**把"未知故障"变成合法线上事件的地方：`INTERNAL_ERROR` 是 §3.2
  已登记的传输层码，不是自造字符串；`message` 是固定文案（不把异常文本发给用户，
  避免泄露内部路径/密钥片段，§3.2「不外泄 message 与 traceback」），细节只进日志。
- `usage` 的取值：正常路径取最后一次 `updates` 里的终态，异常路径保持 `_EMPTY_USAGE` ——
  于是 §3.1 的"失败时 `usage` 填零而非省略"在流里也成立。
- **`astream` 的 state 入参只有 `{"question": question}`**：检查点是 state authority，
  续跑与历史都由它提供（`AGENTS.md` §2「恢复时不要传状态」）。
- `graph.astream(..., stream_mode=[...])` 产出的是 `(mode, payload)` 二元组（L2）——
  所以 `async for` 必须解包两个值，不能只取一个。

- `thread_id` 由路由给：本阶段**先**接受请求里可选的 `session_id`，缺省用
  `request_id`（即"一次请求一条 thread"）。
  用 `request_id` 做默认值让"不带 session 的调用"天然不串台，且不需要额外建表（§10.4）。
- **`request_id` 进 `stream_answer` 是为了 `done` 事件**（§2.4 末行），不参与业务。
- 两个 router 各自有一行 `_thread_id(request, body)`：`body.session_id or ctx.request_id`，
  `ctx = get_request_context(request)`。把它写成**每个 router 一行**而不是
  `services/qa.py` 里的函数，是因为 `services/` 不该 import `fastapi`（分层）。
- `answer()` 在遇到 `error` 事件且 `code == RETRIEVAL_EMPTY` 时返回
  `QAResult(answer="", citations=[], usage=零值, status="empty")`，
  由 `/knowledge` 决定是抛 `RetrievalEmpty`（让 `api/errors.py` 出 200 信封，D6）。
  其它 `error` 一律**重新抛出**对应异常 —— 它们是真故障，不该被 200 吞掉。
- **`KeyError` 的翻译在 `stream_answer` 的 `except` 里，不在节点里**（§5.5 末段）：
  `tools` 节点调 `get_tool(tc["name"])` 时若模型给了没注册的名字，抛的是 `KeyError`，
  不是 `AppError`；`stream_answer` 用 `except (AppError, KeyError) as exc` 接住，
  交给 `_error_event()`：它把 `KeyError` 包成
  `LLMBadResponse(..., details={"tool_name": ...})` 再翻译成 `error` 事件。
  **节点里不 try**：图节点只做业务，错误映射只有一处（§2.4 第 2 条）。
- **`except Exception` 那一支同时覆盖"上游畸形"和"我们自己写错"**（L20）：前者该给用户
  `INTERNAL_ERROR`，后者该炸在测试里 —— 两者在线上都只能落到同一个固定文案，
  区别体现在日志（`logger.exception` 带完整 traceback）和 `request_id` 上。
  不要因为它"看起来像在吞异常"就删掉：删掉之后客户端拿到的是无 `error`、无 `done`
  的半截流（实测），比一条明确的 `INTERNAL_ERROR` 难排查得多。

### 5.8 `api/routers/knowledge.py`

```python
router = APIRouter()

@router.post("/knowledge", response_model=Envelope[QAData])
async def knowledge(request: Request, body: KnowledgeRequest) -> Envelope[QAData]:
    qa = get_qa_graph(request)
    await qa.preflight()                     # 非流式也要预检：否则 CorpusMissing 会在图里冒
    request_id = get_request_context(request).request_id
    result = await answer(
        body.question, graph=qa.graph, thread_id=_thread_id(request, body), request_id=request_id
    )
    if result.status == "empty":
        raise RetrievalEmpty(query=body.question)        # → 200 + ok=true（api/errors.py）
    return envelope_success(_to_data(result), meta=build_meta(request, usage=_to_usage(result.usage)))
```

- **`raise RetrievalEmpty` 是本文件的唯一业务分支**，且它不碰 HTTP ——
  状态码由 `api/errors.py` 决定（§4「唯一异常映射点」）。
- `meta.usage` 必须来自**这次**调用的 `UsageSnapshot`（§3.1 的四个字段一一对应）：
  `prompt_tokens / completion_tokens / cached_tokens / cost_usd / is_off_peak`。

### 5.9 `api/routers/chat.py`（★ 唯一事件 → SSE 映射点）

```python
router = APIRouter()

@router.post("/chat")
async def chat(request: Request, body: ChatRequest) -> EventSourceResponse:
    request_id = get_request_context(request).request_id
    qa = get_qa_graph(request)
    await qa.preflight()                             # D6：开流前预检（廉价、不加载模型）
    events = stream_answer(
        body.question, graph=qa.graph,
        thread_id=_thread_id(request, body), request_id=request_id,
    )
    return EventSourceResponse(
        _sse_stream(events, queue=asyncio.Queue(maxsize=64),
                    heartbeat_seconds=SSE_HEARTBEAT_SECONDS),
        ping=0, sep="\n",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

`queue` 有 `maxsize`：图跑得比客户端读得快时**不能无限攒内存**（客户端断了却还在跑图
是"用户了事、账单照付"的典型）。`_pump` 里用 `await queue.put(event)` 自然施加背压。

- **是 `async def` + `await qa.preflight()`，不是 `await asyncio.to_thread(assert_index_ready)`**：
  预检必须用**图节点的同一组**依赖（§5.5 的 `QAGraph.preflight`，与 `search`/`fetch` 同源），
  否则夹具集合（`test-hashing-64` 建的）会被 `settings.embedding_model_id` 判成漂移。
  路由自己调 `assert_index_ready()` 就等于"预检一套依赖、检索另一套" —— 正是 D11 要挡的。

`_sse_stream()` 的结构（D5）：

```python
async def _sse_stream(events, queue, heartbeat_seconds) -> AsyncIterator[ServerSentEvent]:
    task = asyncio.create_task(_pump(events, queue))          # 图在独立 task 里跑
    getter: asyncio.Task[QaEvent] | None = None
    try:
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(                     # L20：与"结束"竞速，不用哨兵
                {getter, task}, timeout=heartbeat_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done:
                yield _to_sse(getter.result())
                getter = None
            elif task in done:                                # 生产者已结束
                _log_pump_crash(task)                         # 崩了也不静默，见下
                while not queue.empty():                      # 排干尾部：最后的事件不能丢
                    yield _to_sse(queue.get_nowait())
                break
            else:
                getter.cancel()
                getter = None
                yield ServerSentEvent(comment="heartbeat", sep="\n")   # L11 逐字节
    finally:
        if getter is not None:
            getter.cancel()                                   # 悬空的 queue.get() 必须收掉
        task.cancel()                                          # 客户端断开也要停图
        await asyncio.gather(task, return_exceptions=True)


async def _pump(events: AsyncIterator[QaEvent], queue: asyncio.Queue[QaEvent]) -> None:
    """把服务层的事件搬进队列。**不发结束哨兵** —— 生产者的结束由 task 状态表达（L20）。"""
    async for event in events:
        await queue.put(event)          # 队列满 → 背压；取消 → CancelledError 自然上抛
```

为什么非要这一层中转：图必须跑在**独立 task** 里，`_sse_stream` 才能在
"等事件"的同时发心跳；而 `queue.get()` 被取消只会取消**这一次等待**，
不会波及图（这正是 §D5 里 `wait_for(anext(gen))` 干不成的事）。

**为什么不用 `None` 当结束哨兵（L20，实测）**：哨兵要由 `_pump` 的 `finally` 送出，
而那一刻队列**往往正好是满的**（消费方刚取消，64 个坑位一个没腾）。
`await queue.put(None)` 于是永久阻塞；`_sse_stream` 的 `finally` 在
`await gather(task, ...)` 上等它，`task.cancel()` 也救不了它 —— 因为取消的是
`gather` 的等待者而非这个已经卡在 `put` 里的协程。
实测三种 `maxsize`（1 / 2 / 8）全部 `gather HUNG`，客户端断开一次就漏一个 task。
改成"生产者的**结束**由 `task.done()` 表达"之后，这个死锁在结构上不存在：
没有任何一句 `await` 需要在取消之后才完成。

- **`asyncio.wait({getter, task})` 而不是 `wait_for(queue.get())`**：`wait_for` 只看得到
  "队列里有没有东西"，看不到"生产者已经不在了"。于是空流要等满一个心跳才退出
  （实测 `SSE_HEARTBEAT_SECONDS=15` 时白挂 15 秒），而正常收尾也要多等一个心跳周期。
  与 `task` 竞速之后：空流 0 秒退出、快流 0 秒收尾、慢流照常发心跳（三种都实测过）。
- **`task in done` 分支要先把队列排干再 `break`**：`_pump` 可能在写入最后几个事件后
  立刻结束，`wait` 会同时报告两者 —— 只 `break` 不排干就会**丢掉尾部事件**（典型是
  `done`）。排干用 `get_nowait()`（此刻没有别的消费者，`queue.empty()` 是可靠的）。
- **`_log_pump_crash(task)` 是必要的，不是防御性代码**：生产者抛异常时 `async for`
  提前结束，`_sse_stream` 只看到"task 结束了"，**看不到异常** —— 不查 `task.exception()`
  就等于把故障吞进黑洞。§5.7 的 `except Exception` 已经保证 `stream_answer` 自己
  不会漏 `error` + `done`，所以这里的检查针对的是它**之外**的崩溃（序列化失败、
  `_to_sse` 的 `KeyError` 等）：只打 `error` 级日志带 `request_id`，不补发事件
  （流已经开始，补发会让事件顺序与状态机不一致）。
- **`finally` 里必须先 `getter.cancel()` 再 `task.cancel()`**：漏掉前者时，
  被丢弃的 `queue.get()` 仍挂在队列的 `getter` 等待表里，`aclose()` 会一直等它
  （实测：漏这一句 → `aclose` 超时；补上 → 立刻返回）。这是本函数**唯一**的
  资源泄漏点，`test_sse` ⑦ 就是钉它的。
- `_to_sse(event)` 是**唯一**把事件翻成 `ServerSentEvent` 的地方 ——
  它按 `type` 取 payload 键，**不是**把整个事件 dict 塞进 data：

  ```python
  _PAYLOAD_KEYS: Final = {
      "token": ("text",),
      "citations": ("citations",),
      "tool": ("phase", "name"),
      "error": ("code", "message", "details"),
      "done": ("request_id", "usage"),
  }

  def _to_sse(event: QaEvent) -> ServerSentEvent:
      kind = event["type"]
      payload = {key: event[key] for key in _PAYLOAD_KEYS[kind] if key in event}
      return ServerSentEvent(
          event=kind, data=json.dumps(payload, ensure_ascii=False), sep="\n"
      )

  def _log_pump_crash(task: asyncio.Task[None]) -> None:
      """生产者异常只进日志：流已开，补发事件会打乱状态机（§5.9 上）。"""
      if not task.cancelled() and (exc := task.exception()) is not None:
          logger.error("qa.pump_crashed", exc_info=exc)
  ```

  这一层"挑键"是必要的：`QaEvent` 是 `total=False` 的扁平 dict，
  直接把整个 dict 发出去会把 `type` 冗余进 data，且将来加字段会**静默**改线上格式。
  用 `_PAYLOAD_KEYS` 之后，线上字段是白名单，加键不会外泄。
- 路由**不做任何**过滤：图里已经筛过（D5）。这里只做"dict → 字节"。
- 若 `graph` 为 `None`（无 API key）：`ConfigError` 会在 `get_qa_graph` 里抛，
  发生在**开流前**，于是仍然是 500 真信封 —— 与 D6 的预检同一条原则。

### 5.10 `api/schemas.py`：**只追加**（契约冻结，§4）

```python
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)
    session_id: str | None = Field(default=None, max_length=64)

class KnowledgeRequest(ChatRequest): ...

class CitationData(BaseModel):
    index: int
    title: str
    section: str | None
    url: str
    snippet: str

class QAData(BaseModel):
    answer: str
    citations: list[CitationData]
    status: Literal["ok", "empty"]
```

- **不新增 `API_VERSION`**（`v1` 不变）：只加**新的**请求/响应模型与**新的**路由，
  既有字段一个没改 —— 按 `AGENTS.md` §6 第 3 条，这**不是**破坏性变更。
- `Usage` 不新建：`llm/tracing.UsageSnapshot` → `schemas.Usage` 的映射
  `_to_usage()` 写在 `services/qa.py`（字段名逐字一致，是阶段三留下的缝）。
- `ChatRequest` 的 `max_length=2_000`：超长提问会直接变成 422 真信封，
  而不是流到一半才炸 —— 流一旦开始就改不了状态码（D6 的同一逻辑）。

### 5.11 `api/main.py` 与 `api/deps.py` 增量

`main.py`：

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level, json_output=settings.log_json)
    async with open_engine(app.state.database_url) as engine:
        app.state.engine = engine
        async with open_checkpointer(database_url=app.state.database_url) as saver:
            app.state.checkpointer = saver
            app.state.qa_graph = _build_graph_if_possible(app, saver)   # 无 key → None
            logger.info("api.startup", api_version=API_VERSION, qa_graph=app.state.qa_graph is not None)
            yield
    logger.info("api.shutdown")
```

`_build_graph_if_possible(app, saver)` 内部：

```python
def _build_graph_if_possible(app: FastAPI, saver: BaseCheckpointSaver[str]) -> QAGraph | None:
    try:
        return build_qa_graph(
            checkpointer=saver,
            model=app.state.chat_model,      # None → get_chat_model()
            search=app.state.search,         # None → _default_search（生产向量库）
            fetch=app.state.fetch,           # None → _default_fetch
            preflight=app.state.preflight,   # None → assert_index_ready
        )
    except ConfigError:
        logger.warning("api.qa_graph.unavailable")
        return None
```

`app.state.search` / `.fetch` / `.preflight` 由 `create_app` 从参数原样挂上，
**默认都是 `None`** —— 生产路径一行代码都不走测试分支（D11）。

加路由（**必须**用 extend，理由见 `api/main.py:105-113` 与 `docs/decisions/0002`）：

```python
app.router.routes.extend(_health_router.routes)
app.router.routes.extend(chat_router.routes)       # ← 阶段六
app.router.routes.extend(knowledge_router.routes)  # ← 阶段六
```

`deps.py`：

```python
def get_qa_graph(request: Request) -> QAGraph:
    """取 lifespan 编译好的图。**不在这里现编一个**（§2 单点所有权）。

    返回的是 `QAGraph(graph, preflight)` 这一整个对象，不是裸的 `CompiledStateGraph` ——
    预检与图必须共用同一组依赖（§5.5 的 `search`/`fetch`/`preflight` 三件套），
    路由拿不到那个依赖就只能猜，而猜就是"生产对、测试错"的来源。
    """
    qa = getattr(request.app.state, "qa_graph", None)
    if qa is None:
        raise ConfigError("问答图不可用：DEEPSEEK_API_KEY 未配置或图未编译")
    return cast("QAGraph", qa)
```

`_build_graph_if_possible`：捕 `ConfigError` → 打 `api.qa_graph.unavailable` 警告 → 返回 `None`。
**这是刻意让服务能起来**：没有 key 时 `/health` 仍然可用（ADR 0001 的同一取舍）。

### 5.12 `evals/metrics.py` 与 `evals/run_rag_eval.py`

```python
def recall_at_k(retrieved_urls: Sequence[str], relevant_urls: Sequence[str], *, k: int = 5) -> float:
    """命中的相关页数 / 相关页总数。`retrieved_urls` 取前 k 条，**按 doc_url 去重**后比。"""

def citation_accuracy(answer: str, citations: Sequence[Citation],
                      relevant_urls: Sequence[str]) -> float:
    """答案里出现的每个 `[n]`，其 `Citation.doc_url` 是否在 `relevant_urls` 里。

    **这是对 `AGENTS.md` §5.2「引用准确率」的可判定代理**：真正的定义是
    "链接指向的文档确实包含支撑该结论的片段"，那需要语义判断；本阶段用
    "链接指向的文档是否属于该题的相关文档集"来近似（§10.3 记录了这个偏差）。
    答案里一个标记都没有 → 返回 0.0（并计入"无引用"计数），不是 1.0。
    """
```

`run_rag_eval.py` 的流程：逐条读 `qa.jsonl` → `retrieve(question, top_k=5)` → 算 Recall@5；
再跑一遍图拿答案 → 算引用准确率。**它需要真实索引 + 真实 API key**，
因此是**手动命令**，不进 CI（`AGENTS.md` §5.1 的离线要求）。

输出格式（写进 `docs/evals/阶段六-跑分.md` 的就是这段）：

```
records=44  recall@5=0.8xx  citation_accuracy=0.9xx  answers_without_citation=0
```

**纯函数的可测性**：`metrics.py` 的两个函数没有任何 I/O，
`test_rag_eval.py` 用固定假数据断言具体数值、边界（`k` 超长、重复 URL、零标记）——
于是"跑分器的算术"是 CI 的一部分，只有"跑分器去不去得了网"是手动步骤。

### 5.13 `tests/fakes/llm.py`（L14 逼出来的）

```python
class ScriptedChatModel(BaseChatModel):
    """按脚本逐轮返回 `AIMessage` 的聊天模型替身。

    **为什么不用 `GenericFakeChatModel`**：它的 `bind_tools()` 直接
    `NotImplementedError`（L14 实测），而图里 `agent` 节点必须 `bind_tools`。
    自写 30 行，比给现成 fake 打补丁便宜。
    """
    script: list[AIMessage] = Field(default_factory=list)
```

- `_generate` 弹出下一条脚本；`_astream` 把 `content` 逐字符切成 `AIMessageChunk`
  （于是"逐字上屏"在测试里是真的逐字，不是一次性）。
- `bind_tools()` 记录收到的 specs 并返回 `self` —— 判据 9 用它断言
  "模型看到的工具名 == 函数名"。
- **它不发任何网络请求**：`respx` 的 `assert_all_called=False` 都能省掉，
  全量 `pytest` 全程不触网（判据 14）。

### 5.14 `tests/conftest.py` 增量

| 夹具 | 作用 |
|---|---|
| `scripted_model` | 空的 `ScriptedChatModel`，用例自己塞 `script` |
| `qa_app(tmp_database_url, collection, hashing_embedder, scripted_model)` | `create_app(..., chat_model=scripted_model, search=..., fetch=..., preflight=...)` —— 三个可调用对象在**夹具里**由 `collection` + `hashing_embedder` 绑好，一起交出去 |
| `qa_client(qa_app)` | `TestClient(qa_app)` **上下文管理器**（lifespan 必须跑，否则没有 checkpointer / 图） |

`collection` 与 `hashing_embedder` 两个夹具**直接复用阶段五的**（`conftest.py:67`），
不新建一套 —— 图的测试与检索的测试走**同一份夹具语料**，
于是"检索对但图错"与"图对但检索错"能区分开。

`qa_app` 里三个可调用对象的写法（**配对发生在这里，不在 `create_app` 里**，D11）：

```python
@pytest.fixture
def qa_app(tmp_database_url, collection, hashing_embedder, scripted_model):
    async def search(query: str, top_k: int) -> list[RetrievedChunk]:
        return await retriever.retrieve(query, top_k=top_k,
                                        collection=collection, embedder=hashing_embedder)

    async def fetch(doc_url: str) -> list[RetrievedChunk]:
        return await retriever.fetch_document(doc_url, collection=collection)

    async def preflight() -> None:
        await retriever.assert_index_ready(collection=collection, embedder=hashing_embedder)

    return create_app(database_url=tmp_database_url, rate_limit_enabled=False,
                      chat_model=scripted_model,
                      search=search, fetch=fetch, preflight=preflight)
```

`retriever.retrieve` / `fetch_document` / `assert_index_ready` 的 `collection` / `embedder`
参数**本来就存在**（阶段五留的缝），所以这里不需要为测试新开任何口子 ——
`create_app` 只是把三个**已经绑好的行为**原样往下传。

### 5.15 `llm/prompts/qa_context.jinja2`

```jinja
以下是本轮检索到的 Python 官方文档片段。回答只能依据这些片段。

{% for citation in citations %}
[{{ citation.index }}] {{ citation.title }}{% if citation.section %} — {{ citation.section }}{% endif %}
{{ citation.snippet }}
{% endfor %}

规则：引用片段时在句末标注对应编号，例如"（见 [1]）"。
只标注上面真实存在的编号；没有片段支撑的结论必须删除，不要补编号。

问题：{{ question }}
```

- **`{{ citation.index }}` 而不是 `{{ loop.index }}`**（D4）：编号的唯一来源是
  `build_citations`，模板只排版。
- **角色是 `human`，由 `_context_template()` 加载**（L17）：走 `load_prompt()` 会得到
  一条 `SystemMessage` 并要求一个 `history` 占位符，两个后果都不可接受（§5.5）。
  这也意味着**这个模板不会出现在 `test_prompt_stability.py` 的
  `format_messages(history=..., output_language=...)` 调用里** —— 那个测试只按
  `qa.jinja2` 的形状渲染。片段模板的"无易变 token"仍由同文件的 `glob("*.jinja2")`
  静态扫描覆盖（它不渲染，只读文本）。
- 片段按 `citation.index` 升序传入（`build_citations` 的输出本来就是升序）。
- 变量名是 `citation`（单数，循环变量），**不是** `citations`；调用方传的 kwarg 名是
  `citations`。两者不同名是刻意的：`citations=` 是整个列表，`citation` 是当前元素。

### 5.16 契约守护 #27–#33

追加到 `tests/test_contract_guardrails.py`，沿用 `_python_sources()` 与 `_imported_modules()`。

| # | 守护 | 守的是 | 匹配面（**必须收窄**） |
|---|---|---|---|
| 27 | `src/` 内不出现 `create_react_agent` / `create_agent`，也不 `import langgraph.prebuilt` | `AGENTS.md` §1「不用 LangChain 的成品 Agent」 | AST：`_call_name()` ∈ 两个名字 或 import 模块名以 `langgraph.prebuilt` 开头（L13：能 import 成功，故必须查） |
| 28 | `tool(` / `@tool` 只出现在 `agent/registry.py` | §2「定义工具」 | AST：`_call_name() == "tool"` 且调用方文件不是 registry；装饰器 `@tool` 同理 |
| 29 | `SqliteSaver` / `AsyncSqliteSaver` / `PostgresSaver` / `AsyncPostgresSaver` 只出现在 `agent/checkpointer.py` | §2「检查点构造」 | 沿用 #14 的写法扩到阶段六的图与路由 |
| 30 | `get_stream_writer(` 只出现在 `agent/graphs/**` | §2「流式事件 → SSE 映射」+ D5（L5：节点外调用必抛） | AST：调用点文件路径必须以 `agent/graphs/` 开头 |
| 31 | `graph.stream(`（**同步**）在 `src/` 内零出现 | §2「必须用 `await graph.astream(...)`，这条没有例外」 | AST：`_call_name() == "stream"` 且 receiver 名含 `graph`；`astream` 不匹配（名字不同） |
| 32 | `chromadb` 的 import 白名单**不变**（仍只有 `rag/store.py` / `rag/ingest.py` / `rag/retriever.py`） | §2「读向量库」归属 + #24 的延伸 | **不加白名单**。早期草案让 `agent/tools/docs.py` 注入 `Collection`，那会把 `chromadb` 拖进 `agent/` 层 —— 后来改成注入 `Searcher`/`Fetcher` 可调用对象（§5.3），`agent/` 对 Chroma 零知识。这条守护就是防止有人"图省事"把句柄注回去 |
| 33 | `load_prompt(` **不得**出现在 `agent/graphs/qa.py` 里渲染 `qa_context` 的那条路径上 —— 即 `qa_context.jinja2` 只能经 `_context_template()` 读 | L17：`load_prompt` 强制 `system` 角色 + 必填 `history` 占位符，用错会**静默**把第二个 system 塞进历史中间（前缀缓存破裂，不报错） | AST：`agent/graphs/qa.py` 内 `load_prompt` 的实参不得等于 `QA_CONTEXT_PROMPT_NAME`。**这条只是保险**：真正拦住它的是 `test_graph_qa` 里"发给模型的消息序列首条是 system、其余 human/tool"的断言 |

**为什么 #31 要单独一条**：`graph.stream()` 在 async 路由里**能跑通**，
只是阻塞事件循环；它不会报错、不会有警告、测试也绿 —— 一个慢请求拖垮所有连接，
是"本地跑得通、上线出事"的典型。AST 扫描是唯一能在 CI 里拦住它的办法。

### 5.17 测试文件（覆盖矩阵）

| 文件 | 覆盖 |
|---|---|
| `test_registry.py` | `SEARCH_DOCS.name == "search_docs"` 且 `.coroutine.__name__ == "search_docs"`（L8）；`READ_DOC` 同理；`tool_specs()` 的 `function.name` 与 `TOOLS` 一一对应；**逐工具**断言 `SEARCH_DOCS.response_format == "content_and_artifact"` 且 `READ_DOC.response_format == "content"`（L16，"给所有工具都加 artifact"是错的）；`SEARCH_DOCS._injected_args_keys` 恰为 `{top_k, start_index, searcher}`、`READ_DOC` 恰为 `{fetcher}`；`get_tool("不存在")` 抛 `KeyError`；`tool_specs()` 返回拷贝（改一个不影响下次） |
| `test_graph_qa.py` | ① 实质路径：`retrieve` 命中夹具语料 → `agent` 出答案 → `finalize` 带来源块；② `citations` 的 `index` 从 1 连续；③ 答案里每个 `[n]` 都在 `citations` 里；④ **检索为空 → `RetrievalEmpty`**（服务层捕获 → `status="empty"`，零 token）；⑤ **连续两轮工具调用**：脚本给 `AIMessage(tool_calls=[...])` ×2 → 断言**第三轮**请求 payload 里 `assistant["reasoning_content"]` 存在（respx 断言出站 JSON）；⑥ **检查点往返**：`chunks[0]` 仍是 `RetrievedChunk`、`usage.cost_usd` 仍是 `Decimal`（L3）；⑦ **同 thread 两轮**：第二轮的 `chunks` 不含第一轮的 `chunk_id`（D7）；⑧ `_trim_history` 不拆 `tool_calls → ToolMessage` 组；⑨ **依赖注入要么全给要么全不给**：`build_qa_graph` 只传 `search` / 只传 `fetch` / 只传 `preflight` 等三种半套组合都 `ConfigError`（D11）；⑩ 并行 `tool_calls`（两个 `search_docs`）时编号不重叠（§5.5 的递增游标）；⑪ **送给模型的消息序列首条是 `SystemMessage`、且序列里只有一条 system**，且 `system.content` 两次调用逐字节相同（L17 + 前缀缓存）；⑫ `tools` 节点调 `search_docs` 后拿到的确实是 `ToolMessage`（不是裸 `str`），且 `.artifact` 非空（L18） |
| `test_sse.py` | ① `X-Accel-Buffering: no` 与 `Cache-Control: no-cache` 都在（L9/L12）；② 线上字节：`b"event: token\ndata: "` 前缀、事件间是 `\n\n` 不是 `\r\n\r\n`（L11）；③ 心跳：把 `SSE_HEARTBEAT_SECONDS` patch 到 0.05，脚本模型 `await asyncio.sleep(0.3)`，断言响应体里出现 `b": heartbeat\n\n"`；④ **不出现 LangGraph 原始事件名**（`on_chat_model_stream` / `on_tool_start` / `updates`）—— 判据 8；⑤ 空检索：有 `event: error` + `code=RETRIEVAL_EMPTY`、有 `done`、**没有任何 `event: token`**（判据 1）；⑥ 索引未建：503 真信封（非流），`CACHE`/`X-Accel` 头不在它身上；⑦ **读到一半 `aclose()` 不抛** `async generator ignored GeneratorExit`（L19），且断开后图任务被取消；⑧ 把队列 `maxsize` patch 到 1、事件源无限产出，读到第一个事件后 `aclose()`，`asyncio.wait_for(..., timeout=1)` 内返回（L20 哨兵死锁回归）；⑨ 事件源**抛非 `AppError` 异常**（如 `ValueError`）：`stream_answer` 仍产出 `event: error` + `code=INTERNAL_ERROR` + `done`，且 `message` 不含异常原文；⑩ 事件源**立即结束**（空流）：`wait_for` 一个心跳周期内结束，且响应体里**没有** `: heartbeat` |
| `test_rag_eval.py` | `recall_at_k` 的具体数值；`k` 大于列表长度不越界；重复 URL 去重；`citation_accuracy` 在零标记时返回 0.0、在越界标记时不算分；端到端用**假检索结果**跑 `run_rag_eval` 的纯函数部分（不触网） |

---

## 6. 任务分解

| 任务 | 内容 | 产物 | 预估 |
|---|---|---|---|
| **T0** | 归档阶段五规格（`cmp` 校验逐行一致） | `docs/specs/阶段五-RAG子系统.md` | ✓ 已完成 |
| **T1** | 写阶段六 SPEC（本文件） | `SPEC.md` | ✓ 本文件 |
| **T2** | `config.py` 八个常量 + `agent/state.py` | 2 文件 | 小 |
| **T3** | `agent/tools/docs.py` + `agent/registry.py` + `test_registry.py` | 3 文件 | 小 |
| **T4** | `rag/retriever.py::assert_index_ready()`（唯一改阶段五的地方） | 1 文件增量 | 小 |
| **T5** | `llm/prompts/qa_context.jinja2` + `test_prompt_stability.py` 复跑 | 1 文件 | 小 |
| **T6** | `agent/graphs/qa.py`：四个节点 + 边 + `_trim_history` | 1 文件 | **大**（本阶段核心） |
| **T7** | `tests/fakes/llm.py` + `conftest.py` 夹具 + `test_graph_qa.py` | 4 文件 | **大** |
| **T8** | `services/qa.py`：`stream_answer` / `answer` / `_to_usage` | 1 文件 | 中 |
| **T9** | `api/schemas.py` 追加 + `api/routers/knowledge.py` | 2 文件 | 小 |
| **T10** | `api/routers/chat.py`（SSE + 心跳）+ `api/main.py` lifespan/挂载 + `deps.py` + `test_sse.py` | 4 文件 | **大** |
| **T11** | `evals/metrics.py` + `run_rag_eval.py` + `test_rag_eval.py` | 3 文件 | 中 |
| **T12** | 守护 #27–#33 + 全量回归 + 跑分 + `docs/evals/阶段六-跑分.md` | 2 文件 + 1 文档 | 中 |

**硬顺序**：T2 → T3 → T6（状态与工具是图的输入）。T4 / T5 可与 T2/T3 **并行**。
T7 依赖 T6。T8 依赖 T6。T9 依赖 T8。T10 依赖 T8 与 T9 的 schema。
T11 只依赖 `retriever` 与 `citations`，可与 T6–T10 **全程并行**。
T12 必须最后。

**T6 / T7 / T10 是本阶段的主要工作量**，其余都是小块。
T12 的"跑分"需要真实索引与 API key，安排在**非峰值时段**（`AGENTS.md` §1「价格与错峰」）。

---

## 7. 验收

### 7.1 命令

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src/pyassist
uv run pytest                                   # 退出码 0，全程不触网

# 阶段六专属
uv run pytest tests/test_registry.py tests/test_graph_qa.py tests/test_sse.py \
              tests/test_rag_eval.py -v

# 既有回归（必须仍全绿）
uv run pytest tests/test_deepseek_reasoning.py tests/test_contract_guardrails.py \
              tests/test_state_ownership.py tests/test_prompt_stability.py -v

# 手动：真实链路（需真实索引 + API key，安排在非峰值时段）
uv run python -m pyassist.evals.run_rag_eval
curl -N -X POST http://127.0.0.1:8000/chat \
     -H 'Content-Type: application/json' \
     -d '{"question": "asyncio.gather 里某个任务抛异常会怎样？"}' | head -40

# 手动：§2.3 的实测事实复验（升级依赖后必跑，输出应逐条对上）
uv run python -c "from sse_starlette.sse import ServerSentEvent as S; \
print(S(comment='heartbeat', sep='\n').encode()); \
print(S(data='hi', event='token', sep='\n').encode())"
```

### 7.2 判据

| # | 判据 | 由什么保证 |
|---|---|---|
| 1 | 空检索：`/chat` 有 `error`（`code=RETRIEVAL_EMPTY`）+ `done`，**零 `token` 事件**；`/knowledge` 返回 **200 + `ok=true` + `data.status="empty"`** | `test_sse` ⑤ + `test_graph_qa` ④ |
| 2 | 实质性回答 ≥ 1 个可点击来源，且每个 `[n]` 都能在 `citations` 里找到 | `test_graph_qa` ②③ |
| 3 | 引用编号从 1 连续、由 `build_citations` 产生（模板里没有 `loop.index`） | `test_graph_qa` ② + 代码审查 |
| 4 | 同一 `thread_id` 连问两轮，第二轮 `chunks` 不含第一轮的 `chunk_id` | `test_graph_qa` ⑦（D7） |
| 5 | **连续两轮工具调用后，第三轮请求 payload 里 `reasoning_content` 仍存在** | `test_graph_qa` ⑤（respx 断言出站 JSON） |
| 6 | 检查点往返后 `chunks[0]` 仍是 `RetrievedChunk`、`usage.cost_usd` 仍是 `Decimal`；`AppState` 的键集合是 `MessagesState` 的**超集**且 `messages` 的 reducer 是 `add_messages` | `test_graph_qa` ⑥（L1 / L3） |
| 7 | 两个响应头在位；事件分隔是 `\n\n`；心跳字面量 `: heartbeat\n\n` 在慢图上到达 | `test_sse` ①②③（L11） |
| 8 | SSE 响应体里**不出现** LangGraph 原始事件名（`on_chat_model_stream` / `on_tool_start` / `updates`） | `test_sse` ④ |
| 9 | 工具名 == 函数名（`.name` 与 `.coroutine.__name__` 都等于 `search_docs`）；`tool_specs()` 与 `TOOLS` 一一对应；`SEARCH_DOCS` 带 artifact、`READ_DOC` 不带 | `test_registry`（L8 / L16） |
| 10 | 守护 #27–#33 全绿，且既有 #1–#26 **仍然全绿** | `tests/test_contract_guardrails.py` 全跑 |
| 11 | Recall@5 与引用准确率的**计算**在固定假数据上可复现 | `test_rag_eval` |
| 12 | 送给模型的序列里**只有一条 system 且在最前**，两次渲染逐字节相同；片段块是 `HumanMessage` | `test_graph_qa` ⑪（L17） |
| 13 | `tools` 节点拿到的是 `ToolMessage`（不是裸 `str`），`.artifact` 非空且能追加进 `chunks` | `test_graph_qa` ⑫（L18） |
| 14 | 客户端中途断开（`aclose()`）不抛 `GeneratorExit` 相关 `RuntimeError`，图任务被取消 | `test_sse` ⑦（L19） |
| 14b | 队列满 + 客户端断开时 `aclose()` 不死锁；空流不等心跳、不发心跳；非 `AppError` 崩溃仍有 `error`+`done` | `test_sse` ⑧⑨⑩（L20） |
| 15 | `mypy --strict` 通过，**没有 `type: ignore` 兜底**；`pytest` 全程不触网 | `uv run mypy` + 全量 `pytest` |
| 16 | 未写 `data/chroma` / `data/app.db`（测试全在 `tmp_path`） | 全量 `pytest` + 人工核对 `git status` |
| 17 | 未创建 §1.2 清单里的任何东西（前端 / 沙箱 / v2 图 / 迁移） | 人工核对 + `git status` |
| 18 | 真实跑分：Recall@5 ≥ 0.80、引用准确率 ≥ 0.90，分数写进 `docs/evals/阶段六-跑分.md` | 手动命令（§7.1 第四条），非峰值时段 |

---

## 8. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | **`reasoning_content` 在流式 + 工具循环下丢失**（本阶段最高风险） | `agent` 节点聚合 chunk 时显式搬运 `additional_kwargs`（§5.5 的 `_to_ai_message`）；判据 5 用 respx 断言**第三轮**出站 JSON；`test_deepseek_reasoning.py` 复跑。若上游修好了，测试仍留着 |
| R2 | 流式 + 思考模式：DeepSeek 的流式 `reasoning_content` 出现在**首块**（L6 实测），若节点只看 `content` 就会把它丢掉 | `_to_ai_message` 从**聚合体**取 `additional_kwargs`，不从某一块取；单测断言聚合体带该键 |
| R3 | 心跳实现用错（`wait_for(anext(gen))`）会把图取消掉 | D5 明确用 `Queue` + `wait_for(q.get())`（已实测两种写法行为不同）；`test_sse` ③ 同时断言"心跳到了"与"答案完整" |
| R4 | 新增 router 用 `include_router` → slowapi 默认限流**静默失效** | §5.11 明确用 `app.router.routes.extend(...)`；`tests/test_envelope.py::test_rate_limited_end_to_end`（阶段四既有）继续跑 |
| R5 | 检查点膨胀：每轮的 `chunks`（含全文）都进状态 | `chunks` 每轮**覆盖**（D7）；`read_doc` 截断到 `READ_DOC_MAX_CHARS`；`_trim_history` 裁老消息（D8）。本阶段**不做** thread 清理（§10.7） |
| R6 | 引用准确率用 `relevant_urls` 代理，与"链接指向的文档确实包含支撑片段"有偏差 | §10.3 明确记录偏差；跑分文档里同时给出"无引用答案数"与"越界标记数"，让偏差可见而不是被一个数字盖住 |
| R7 | `max_length=2000` 之外的超长提问仍可能顶爆上下文（历史 + 片段） | `_trim_history` + `citations` 只取 `top_k=5`；流式路径没有"超时"问题（`astream` 持续吐字节），非流式 `/knowledge` 走同一条 `stream_answer`，天然不会挂 HTTP 超时 |
| R8 | 图的集成测试若用真模型就会触网 | `create_app(chat_model=...)` 注入缝（D11）；`ScriptedChatModel` 完全离线（§5.13） |
| R9 | 片段块误用通用 `load_prompt()` → 得到第二个 `SystemMessage` 且缺 `history` 报 `KeyError` | L17 实测记录 + `_context_template()` 独立加载（§5.5）+ 守护 #33 + `test_graph_qa` ⑪ 断言"序列里只有一条 system 且在最前" |

---

## 9. 与 ROADMAP 的差异（均需知悉）

1. **`rag/retriever.py` 追加 `assert_index_ready()`**（ROADMAP 未列）。
   理由：D6 要求"开流前做廉价预检"，而 `AGENTS.md` §2 把"读向量库"划给了 `retriever.py`；
   放在 router 里直接碰 Chroma 会被守护 #24 打回。这是本阶段对阶段五**唯一**的改动。
2. **`services/qa.py` 的 `answer()` 是 `stream_answer()` 的消费者，不是另一套 `ainvoke`**
   （D10）。ROADMAP 只说"两个 router"，没说它们怎么共用路径；本规格把这条钉死，
   因为两条独立路径会让 `reasoning_content` / 引用编号 / 空检索处置悄悄分叉。
3. **新增 `evals/metrics.py`**（ROADMAP 只列 `run_rag_eval.py`）。
   理由：把"算术"与"I/O"分开，算术才能在 CI 里离线测（§5.12）。
4. **新增 `llm/prompts/qa_context.jinja2`**（ROADMAP 未列）。
   `qa.jinja2` 必须逐字节稳定才能吃前缀缓存（`AGENTS.md` §1），
   检索片段是**每轮都变**的内容，不能塞进它。
5. **新增 `tests/fakes/llm.py`**（ROADMAP 未列）。
   L14 实测现成的 `GenericFakeChatModel` 不支持 `bind_tools`，不自写就没法离线测图。
6. **`agent/store.py` 不在 lifespan 里打开**。阶段三已备好构造点，
   但本阶段没有任何代码读长期记忆（§1.2），过早打开会让"能否起服务"多一个依赖。
7. **`POST /chat` 的 `session_id` 缺省值用 `request_id`**（§5.7）。
   这样"不带 session 的调用"天然不串台，且本阶段不需要建 `chat_sessions` 表（§10.4）。
8. **`MAX_GROUNDING_RETRIES = 0`**（D4）。`find_ungrounded_markers` 在本阶段只用来剥标记，
   不触发重生成 —— 重生成要再付一次钱，而剥标记已经消掉了"指向不存在的来源"这个错误。

---

## 10. 待定项（每条给推荐值，均不阻塞动工）

1. **真实跑分需要什么**：真实索引（`data/chroma`，阶段五已建好 17366 chunk）
   + 真实 `DEEPSEEK_API_KEY` + 网络。推荐**在非峰值时段**跑（`AGENTS.md` §1：
   UTC 周一至周五 01:00–04:00 与 06:00–10:00 为峰值，其余半价）。
   44 条约几十次模型调用，Flash 档成本在**美分级**，不是问题；问题只是"别在峰值跑"。
2. **`RETRIEVAL_MIN_SCORE` 回填值**：现在是 `0.0`（不过滤）。
   推荐**先跑一次不改阈值**，看 `retrieval.done` 里 top-5 的 `scores` 分布，
   再决定要不要设一个下限（例如 0.35）。**不要**为了"提高引用准确率"而把阈值调高 ——
   阈值调高会先压低 Recall@5，两个指标是互相拉扯的，必须一起看。
   回填后同步改 `config.py` 那段 docstring。
3. **引用准确率是否加 LLM 裁判**：推荐**不加**。
   用 `relevant_urls` 做可判定代理，好处是**完全可复现、零成本、能进 CI**；
   坏处是它测的是"链接指向的文档对不对"，不是"该文档是否真的支撑这句话"。
   若将来要加裁判，**先把它做成独立脚本**（`experiments/`），不要进 `evals/metrics.py` ——
   那会让跑分结果依赖一次模型调用的随机性。
4. **会话建档与 Token 账本是否本阶段落库**：推荐**不落**。
   `db/repositories.py` 的 `create_chat_session` / `append_token_usage` 已就位，
   但 `AGENTS.md` §2 明确规定"对话历史只读 checkpoint"，本阶段落一份会话表
   会立刻制造第二个真相。阶段八接 `/stats` 与 `daily_token_limit` 时统一做，
   那时才有消费方。**本阶段 `meta.usage` 已经把这轮的用量完整暴露出来了**，
   数据没丢，只是还没归档。
5. **README 尚不存在**（`pyproject.toml` 的 `readme` 也刻意没声明）。推荐先写
   `docs/evals/阶段六-跑分.md` 记录分数与方法，阶段八写 README 时引用它。
   ROADMAP 说"把分数记进 README"，但 README 是阶段八产物 —— 记在这里不违背意图。
6. **是否引入 rerank / query 改写**：推荐**不引入**。
   先把稠密检索的门跑过；若 Recall@5 明显低于 0.80，再在图里加一步
   （那正好是"工具循环"的用武之地，不用改引擎）。
7. **检查点清理（thread 过期）**：本阶段**不做**。`AGENTS.md` §2 提到"定期清理过期 thread"，
   但清理策略（按时间？按条数？）需要真实使用数据才能定。
   本阶段的缓解是"`chunks` 每轮覆盖 + 历史裁剪"，膨胀速度已被压住。
8. **`X-Request-ID` 与 SSE**：L12 实测中间件会给 SSE 响应也盖上 `X-Request-ID`，
   所以路由里**不重复发**；但 `done` 事件里仍带一次 —— 前端在流里需要一个 id
   来关联日志，而响应头在 `EventSource` 里读不到（浏览器 API 限制）。
9. **是否新增 pytest marker**：本阶段**不新增**。真实跑分是手动命令（§7.1），
   不进 CI；若将来要加联网测试，**必须同时在 `pyproject.toml` 注册**，
   否则 `--strict-markers` 直接报错（阶段五待定项 9 的同一提醒）。
10. **`QaEvent` 用 `TypedDict(total=False)` 而非 Pydantic 模型**（§5.2）。
    它只在进程内从节点流向 `services/qa.py`，从不跨进程、不落盘，
    用 `TypedDict` 省掉一次 `model_dump` 且能直接 `json.dumps`。
    若施工时发现类型收窄太麻烦（`total=False` 下每个键都是 `NotRequired`），
    换成 `dataclass` 也可以 —— 但**事件类型集合必须仍是那五个**，`test_sse` 会断言。

---

## 11. 本阶段为阶段七预留的接口（不写空壳，只列形状）

阶段七（沙箱 + v2 诊断）**不需要**回头改本阶段的任何文件。它要做的只是"用"与"加工具"：

| 阶段七要用的 | 本阶段给它的 |
|---|---|
| 新增工具（`run_code` / `run_tests`） | `agent/registry.py` 里 `TOOLS` 追加一项即可；`tool_specs()` 自动带上，`agent` 节点无需改 |
| 诊断图 | `agent/state.py` 的 `AppState` 直接继承（`chunks/citations/usage/tool_rounds/answer` 全都用得上） |
| 失败回环（跑测试不过 → 重新修复） | `MAX_TOOL_ROUNDS` 之外再加一个"沙箱验证"条件边；`agent ⇄ tools` 的循环模式可照抄 |
| 流式诊断过程 | `services/qa.py::stream_answer` 的结构（`astream` + `custom` + `error`/`done` 收尾） |
| 新增 SSE 事件类型（如 `sandbox`） | `QaEvent` 的 `type` 加一个 `Literal` 值，**并在 `_PAYLOAD_KEYS` 里加一行白名单**（§5.9）。`_to_sse` 是 `_PAYLOAD_KEYS[kind]` 查表，不登记会直接 `KeyError` —— 这是刻意的：白名单保证"给事件加字段"不会悄悄改线上格式 |
| 引用越界重生成 | `MAX_GROUNDING_RETRIES` 常量已在；`find_ungrounded_markers` 已在 |
| 检查点恢复后重跑节点 | 节点已按"幂等"写（`retrieve` 覆盖、`tools` 只追加 artifact） |

**明确不在本阶段做的**：`sandbox/**`、`agent/tools/sandbox_tools.py`、
`agent/graphs/diagnose.py`、`services/diagnose.py`、`api/routers/diagnose.py`、
`evals/datasets/errors.jsonl`（§1.2）。

