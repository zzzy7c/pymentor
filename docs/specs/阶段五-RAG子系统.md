# SPEC — 阶段五 · RAG 子系统 · 实施规格

> **归档声明**：阶段一（工程底座）、阶段二（LLM 基座）、阶段三（持久化与状态双源）、
> 阶段四（API 契约与错误体系）的规格均已归档到 `docs/specs/`：
> `阶段一-工程底座.md`、`阶段二-LLM基座.md`、`阶段三-持久化与状态双源.md`、`阶段四-API契约与错误体系.md`。
> 本文只描述**阶段五**。阶段四的归档件与原 `SPEC.md` **逐行一致**（`cmp` 校验通过）。
>
> 本文件是施工规格，不是契约。契约是 `AGENTS.md`；两者冲突时以 `AGENTS.md` 为准。
> 本文的每一条设计决定都能追到 `AGENTS.md` 的某个条款号或本文件的某条实测事实（§2）。

**阶段五 = RAG 子系统**（ROADMAP「阶段五 · RAG 子系统」，对应学习阶段四）。它是 v1 的核心引擎：
`AGENTS.md` §5.2 的三个硬指标（Recall@5 ≥ 0.80、引用准确率 ≥ 0.90、100% 实质性回答带可点击来源）
全部由本阶段的产物决定。**本阶段只交付引擎，不交付壳** —— 图、服务、路由、跑分器都在阶段六。

---

## 1. 范围边界

### 1.1 做

| 交付 | 对应 `AGENTS.md` |
|---|---|
| `rag/embedder.py`：唯一向量化出口 | §2 单点所有权 |
| `rag/store.py`：唯一向量库连接点 + 索引元数据一致性校验 | §2 |
| `rag/corpus.py`：语料抓取与解析（**新增所有者**，见 §9.1） | §1「要更新只能跑 ingest 的抓取流程」 |
| `rag/ingest.py`：唯一写向量库的地方（分块 + 向量化 + 收敛写入） | §2 / §5.2「ingest 可重跑且幂等」 |
| `rag/retriever.py`：唯一检索入口 | §2 |
| `rag/citations.py`：唯一引用构造与校验 | §5.2「引用由 `rag/citations.py` 生成」 |
| `evals/datasets/qa.jsonl`：评估集（≥40 条） | §5.2「在 `evals/datasets/qa.jsonl` 上」/ §4 冻结 |
| 6 个测试文件 + `tests/fakes/` + `tests/fixtures/` | §5.1「测试不依赖真实 LLM 或网络」 |
| 契约守护 #20–#26 | §5.1 |
| `config.py` / `pyproject.toml` / `.env.example` 增量；`AGENTS.md` §2 登记新所有者 | §1 / §6 |

### 1.2 不做（明确排除，验收时按此清单反查）

| 不建 | 归哪个阶段 | 理由 |
|---|---|---|
| 任何 `api/routers/*.py`、`services/*.py`、`agent/*`、任何图 | 阶段六 | 阶段五只交付引擎，不交付壳（ROADMAP 阶段六产出） |
| `evals/run_rag_eval.py`、Recall@5 / 引用准确率的跑分器 | 阶段六 | ROADMAP 把它列在阶段六产出里 |
| SSE / 流式 / 前端 | 阶段六 / 阶段八 | —— |
| BM25 / 混合检索 / rerank / query 改写 | 阶段六之后 | 先把稠密检索的门跑过再说（§10.6） |
| Qdrant 切换 | 产品决定 | `rag/store.py` 是唯一切换点，届时只改它 |
| 为后续阶段预留的空函数 / TODO 占位 | —— | 缝只以「唯一入口 + 已归一化的数据 + 守护测试」形式存在 |
| `MemorySaver` | 永不允许 | §2 / §4 硬禁（本阶段不碰检查点，但不要在测试里顺手引入） |

---

## 2. 前置核查结果（2026-10-13 本机实测 / 读 `.venv` 源码）

设计的地基。全部已核实，不是推测。

### 2.1 已就位的可复用缝（不要重造）

| 缝 | 位置 | 阶段五怎么用 |
|---|---|---|
| `RetrievalEmpty` / `CorpusMissing` / `ConfigError` | `src/pyassist/errors.py` | **阶段五不新增任何错误码**，只 `raise`（§3.2 现有枚举已够） |
| `RetrievalEmpty → 200 ok=True`、`CorpusMissing → 503` | `api/errors.py` 的 `_MAPPING` | 映射已就位，领域层无需感知 HTTP |
| `Envelope[T]` / `Page[T]` / `envelope_success` / `build_meta` | `api/schemas.py` / `api/deps.py` | 阶段六的 router 直接用（阶段五不写 router） |
| `get_logger` / `bind_request_context` | `logging.py` | `retrieval.done` / `ingest.done` 打点 |
| 19 条契约守护 + `_python_sources()` / `_call_name()` / `REPO_ROOT` | `tests/test_contract_guardrails.py` | 追加 #20–#26，沿用同一套 AST 辅助 |
| `Settings.embedding_model_id` 占位 + `.env.example` 的 `EMBEDDING_MODEL_ID` | `config.py:155` / `.env.example` | 阶段五把它接上：默认值改为引用 `DEFAULT_EMBEDDING_MODEL_ID` |

**结论：阶段五不需要任何新错误码，也不需要动 `api/` 下任何文件。**
`src/pyassist/rag/` 与 `src/pyassist/evals/` 目前只有 `__init__.py`；
`data/corpus/` 与 `data/chroma/` 只有 `.gitkeep`。全部从零建。

### 2.2 已装 / 未装

- **已装**：`chromadb 1.5.9`（默认 **Rust 后端**）、`sqlite-vec 0.1.9`、`numpy 2.5.3`、
  `onnxruntime 1.30.0`、`tokenizers`、`huggingface_hub 1.33.0`、`httpx 0.28.1`。
- **未装**：`sentence-transformers` / `torch` / `beautifulsoup4` / `lxml` /
  `langchain-text-splitters` / `rank-bm25`。
  → 阶段五**必须新增两个依赖**（`sentence-transformers`、`beautifulsoup4`），见 §5.12。
  `numpy` / `onnxruntime` / `tokenizers` 随 chromadb 装好，**不重复声明**。

### 2.3 ChromaDB 1.5.9 实测事实（写错就不过验收）

| # | 事实 | 对实现的强制后果 |
|---|---|---|
| C1 | `PersistentClient(path=...)` **在构造时即建目录**，并在其中创建自己的 `chroma.sqlite3` | 测试必须能把 path 指到 `tmp_path`，否则单测一跑就写 `data/chroma`（撞 §4 禁区） |
| C2 | `chromadb.config.Settings.anonymized_telemetry: bool = True`（`config.py:227`） | 构造客户端**必须**传 `settings=Settings(anonymized_telemetry=False)`，否则测试不保证离线（§5.1） |
| C3 | `get_or_create_collection` 默认 `embedding_function=DefaultEmbeddingFunction()`。**省略它**，Chroma 会自行下载 ONNX 模型（all-MiniLM-L6-v2 / 384 维） | 必须**显式传 `embedding_function=None`**，不给向量库任何自行嵌入的机会（§2 单点所有权，否则出现第二套向量） |
| C4 | 传 `embedding_function=None` 后，若用 `documents=` 而没给 `embeddings=`，运行时抛 `ValueError` | 免费的兜底：忘了自带向量会立刻炸，而不是静默乱算 |
| C5 | 默认距离度量是 **l2**，不是 cosine。cosine 用 `configuration={"hnsw": {"space": "cosine"}}` | 建集合必须显式指定 cosine，且**读取时校验**（D7） |
| C6 | `query` 返回的 `ids`/`documents`/`metadatas`/`distances` 是**两层嵌套列表**；`get` 返回**一层**。`get` 的 `include` **不允许** `distances` | 取结果要 `[0]`；`collection.get` 只能 `include=["documents","metadatas"]` |
| C7 | cosine 距离 = `1 - 余弦相似度`，范围 [0,2]，**越小越相似** | `score = 1.0 - distance`。**仅当度量确实是 cosine 时成立** → 必须校验 C5，否则 score 静默变垃圾 |
| C8 | `upsert` 是**替换语义且幂等**，同 id 重复 upsert 后 `count()` 不翻倍 | 幂等测试可用 `count()` 与 id 集合双重断言 |
| C9 | `get_or_create_collection` 对**已存在**的集合会**忽略**传入的 `metadata` / `configuration`（`api/client.py:332-334` 原文：*"If the collection already exists, the schema, configuration, and metadata arguments will be ignored."*） | 模型漂移校验只能**读** `collection.metadata`，绝不能指望写入生效 |
| C10 | 维度由**第一批插入的向量**固定；不匹配抛 `chromadb.errors.InvalidDimensionException`（Rust 后端可能包成基类 `ChromaError`，`chromadb/errors.py:6`） | 换模型后忘重建索引 → 兜底转 `ConfigError`，别裸奔成 500 |
| C11 | `collection.metadata` 只接受 `str / int / float / bool` | 索引元数据（model_id / dimension / space / chunker_version）都能落进去 |
| C12 | `collection.configuration_json` 是 `CollectionCommon` 上的**属性**（`CollectionCommon.py:158`），返回 `Dict[str, Any]`；其内部形状跨版本不保证 | 读 space 时走**递归找 key**的防御式取值，且以我们自己写入的 metadata 为主证（D7） |
| C13 | `validate_embedding_function_conflict_on_create` 只在**两边都非 None** 时才比较（`collection_configuration.py:749-767`） | 只传 `embedding_function=None` + 不含 EF 的 `configuration` 是安全的，不会误报冲突 |

### 2.4 语料来源

- **官方文档**：`docs.python.org/3.12` 的 HTML 归档 zip。解压后形如 `library/asyncio-task.html`，
  可映射回 `https://docs.python.org/3.12/library/asyncio-task.html#<anchor>`。
- **PEP**：索引走 `https://peps.python.org/api/peps.json`（**已联网核实存在**：keyed by PEP number，
  含 `title` / `status` / `type` / `topic` / `created`）；正文抓 `https://peps.python.org/pep-XXXX/` 的 HTML，
  **只抓 `config.PEP_NUMBERS` 的固定清单**以保证可复现。
- ⚠️ **归档 zip 的确切 URL 未能代验**：本机策略拦截了对 `docs.python.org` 的抓取（WebFetch 被安全策略拒绝）。
  → 因此实现采用**两步解析**：先抓 `https://docs.python.org/3.12/download.html`，从中抽出
  `*-docs-html.zip` 的链接；抽不到才回退到写死的 `config.DOCS_ARCHIVE_URL_FALLBACK`。
  这样对「URL 模式记错」和「上游改版」都免疫。**施工第一步就是 `curl -I` 核实一次**（见 §10.11）。
- 版本锁定 **3.12**（ROADMAP 待定项 3 的推荐）：版本固定、评估可复现、与 `AGENTS.md` §1 的运行时一致。

### 2.5 语料落盘布局（本规格新增的约定）

`data/corpus/` 只认**两个前缀**，其余一律不索引：

```
data/corpus/
├── docs/library/asyncio-task.html        → https://docs.python.org/3.12/library/asyncio-task.html
├── docs/reference/...html                → https://docs.python.org/3.12/reference/...html
├── docs/tutorial/...html                 → https://docs.python.org/3.12/tutorial/...html
└── peps/pep-0008.html                    → https://peps.python.org/pep-0008/
```

- 前缀外的东西（`_sources/`、`_static/`、`_modules/`、`_images/`、归档 zip 残留、`.gitkeep`）
  **直接跳过**。排除清单在 `config.CORPUS_EXCLUDE_DIRS` / `CORPUS_EXCLUDE_FILES` 各一处。
- 测试夹具用**同一个根布局**（`tests/fixtures/corpus/docs/...` + `tests/fixtures/corpus/peps/...`），
  因此夹具目录可以直接当 `--corpus` 参数用，测试路径与生产路径**走同一段代码**。

---

## 3. 目标文件清单

`✓` 新建 ｜ `△` 修改 ｜ `★` 契约关键件

```
src/pyassist/
├── config.py                  △   CORPUS_DIR / CHROMA_DIR / COLLECTION_NAME / DEFAULT_EMBEDDING_MODEL_ID
│                                   / DEFAULT_TOP_K / RETRIEVAL_MIN_SCORE / CHUNK_MAX_CHARS
│                                   / CHUNK_OVERLAP_CHARS / CHUNKER_VERSION / DISTANCE_SPACE
│                                   / CORPUS_EXCLUDE_* / DOCS_* / PEP_* / EVAL_DATASETS_DIR
│                                   / Settings.embedding_model_id 默认值引用常量
├── rag/
│   ├── embedder.py            ✓ ★ 唯一向量化出口
│   ├── store.py               ✓ ★ 唯一向量库连接点 + 索引元数据一致性校验
│   ├── corpus.py              ✓ ★ 语料抓取与解析（新所有者，§9.1）
│   ├── ingest.py              ✓ ★ 唯一写向量库的地方
│   ├── retriever.py           ✓ ★ 唯一检索入口
│   └── citations.py           ✓ ★ 唯一引用构造与校验
└── evals/
    └── datasets/qa.jsonl      ✓   评估集（只增不改，§4）

tests/
├── fakes/__init__.py          ✓
├── fakes/embedder.py          ✓   确定性哈希嵌入（离线，绝不执行真实模型）
├── fixtures/corpus/docs/...   ✓   小体量 HTML 夹具（镜像 docs 布局）
├── fixtures/corpus/peps/...   ✓   PEP 夹具
├── conftest.py                △   追加 5 个夹具（见 §5.9）
├── test_corpus_parse.py       ✓   HTML → 结构化块 + URL/锚点映射（纯函数，全离线）
├── test_ingest_idempotent.py  ✓   幂等 + 收敛（删文档后重跑）
├── test_retriever.py          ✓   检索 + 打点 + 索引漂移拦截
├── test_citations.py          ✓   引用构造、编号契约、越界标记
├── test_eval_dataset.py       ✓   qa.jsonl 的 schema 守护
└── test_contract_guardrails.py △  追加 #20–#26

pyproject.toml                 △   2 个依赖 + torch CPU 索引钉 + mypy override
.env.example                   △   EMBEDDING_MODEL_ID 注释改为指向 config 默认值
AGENTS.md                      △   §2 单点所有权表 + 目录结构块登记 `rag/corpus.py`
docs/specs/阶段四-API契约与错误体系.md  ✓   归档（T0，已完成）
SPEC.md                        △   本文件（T1）
```

**不创建**：`routers/*.py`、`services/*.py`、`agent/*`、`sandbox/*`、`Dockerfile.*`、`README.md`。

---

## 4. 关键设计决定（D1–D12，施工时不可协商）

### D1 · 向量模型：本地 `sentence-transformers` + `BAAI/bge-m3`，但**可插拔**

- `AGENTS.md` §1 明确 DeepSeek 不提供 embedding，必须另选；ROADMAP 待定项 1 推荐本地 ST。
- 选 `BAAI/bge-m3`（多语 / 1024 维 / 8192 上下文）：语料是英文、提问大概率是中文，**必须多语**。
- **`Embedder` Protocol + `get_embedder()` 单例工厂**，模型 id 只写在
  `config.DEFAULT_EMBEDDING_MODEL_ID` 一处 → 换模型 = 改一行常量 + 删 `data/chroma` 重跑 ingest
  （与 §4「重建 = 删目录 + 重跑 ingest」对齐）。
- 轻量替代（写进 §10.1，不阻塞动工）：`intfloat/multilingual-e5-small`（约 470MB / 384 维）。
  该家族的**非对称前缀**（`query: ` / `passage: `）正是 Protocol 拆出
  `embed_query` / `embed_documents` 两个方法的理由，不是装饰。
- ⚠️ `torch` 在 Windows 上从 PyPI 默认拉的是 **CUDA 轮子（约 2.5GB）**。用 `[tool.uv.sources]`
  把 `torch` 钉到 CPU 索引（`https://download.pytorch.org/whl/cpu`）。索引不可达时删掉该钉，
  接受大轮子（风险 R2）。

### D2 · `rag/embedder.py`：同步 Protocol，**不**自己开线程

```python
EmbeddingVector = list[float]

class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...
    @property
    def dimension(self) -> int: ...
    def embed_documents(self, texts: Sequence[str]) -> list[EmbeddingVector]: ...
    def embed_query(self, text: str) -> EmbeddingVector: ...
    def count_tokens(self, texts: Sequence[str]) -> list[int]: ...   # §10 第 14 条，见下

class SentenceTransformerEmbedder:   # __init__ 里惰性 import sentence_transformers
def get_embedder(model_id: str | None = None) -> Embedder   # functools.cache
def warm_up(model_id: str | None = None) -> None            # 显式预热
def reset_embedder_cache() -> None                          # 测试与换模型用
```

理由四条：
1. `SentenceTransformer.encode` 本就是同步 CPU 调用；Protocol 若做成 `async`，
   ingest 这个 **CLI 同步路径**反而要裹 `asyncio.run`。
2. 把 `to_thread` 放进 embedder，会让 ingest 白付一层线程跳。→ 线程跳只放在 retriever 的
   async 边界（D6）。**这是"谁拥有并发"的分工，不是疏漏。**
3. `model_id` / `dimension` 是索引元数据（D7）与向量库校验的输入，必须是 Protocol 的一等成员。
4. `count_tokens`（2026-10-13 加）不算"向量化"，但它必须在这儿：填充开销由批内最长的
   那条按 **token** 数决定，而只有本模块拿得到 tokenizer。放这里还顺带保证测试替身
   实现同一口径 —— 于是"分桶"在离线测试里走的是与生产**同一条判据**。
   口径必须与 `embed_documents` 一致（同一 tokenizer、同一 `EMBEDDING_MAX_SEQ_TOKENS` 截断）：
   一条 5000 token 与一条 1024 token 的文本前向开销**完全相同**，拿未截断长度排序是假精度。

### D3 · `rag/store.py`：唯一连接点，且**必须**做四件事

```python
def get_client(path: Path | None = None) -> chromadb.ClientAPI
def get_or_create_collection(*, client=None, path=None, metadata=None) -> Collection   # 写路径（ingest）
def open_collection(*, client=None, path=None) -> Collection                          # 读路径（retriever）
def build_index_metadata(*, model_id: str, dimension: int) -> dict[str, str | int | float | bool]
def assert_index_compatible(collection: Collection, *, model_id: str) -> None          # 不一致 → ConfigError
def close_clients() -> None          # 唯一的关闭点：关掉并丢弃所有缓存客户端（SPEC §10 第 15 条）
def reset_client_cache() -> None     # 只丢弃、不关闭（测试间隔离用）
```

`close_clients()` 与 `reset_client_cache()` **必须分开**：前者的语义是"收尾"（`close()`
让后台 compactor 把待落盘的 HNSW 段写完），后者是"丢缓存但别动我手上的句柄"。
合成一个会让测试之间互相把对方的句柄关掉，报出一堆"对象忽然不能用了"的怪故障。

1. `PersistentClient(path=..., settings=Settings(anonymized_telemetry=False))`。
   **`anonymized_telemetry=False` 不是可选项**（C2），少了它测试就不保证离线（§5.1）。
2. `get_or_create_collection(..., embedding_function=None, configuration={"hnsw": {"space": "cosine",
   "sync_threshold": HNSW_SYNC_THRESHOLD, "batch_size": HNSW_BATCH_SIZE}}, metadata=build_index_metadata(...))`：
   显式 `None` 掐死 Chroma 自带的 ONNX 嵌入（C3）；显式指定 cosine，否则默认 l2，
   `score = 1 - distance` 会**静默**变成垃圾（C5/C7）。
   `sync_threshold` 见 §10 第 15 条 —— **默认的 1000 是 2026-10-13 丢数据的直接原因**，
   不可退回；`batch_size` 必须 ≤ `sync_threshold`（Chroma 硬性要求）。
   ⚠️ 对已存在的集合 Chroma **忽略**这里的 `configuration`（C9）→ 改阈值必须删库重建。
3. `path` / `client` **都是可选入参**（C1：构造即建目录）—— 这是测试唯一需要的缝：夹具把 path
   指到 `tmp_path`，永不碰 `data/chroma`。默认值才走 `config.CHROMA_DIR`。
   **不加 `set_client_for_test()` 之类的后门**，用参数注入（D10）。
4. `open_collection` 用 `client.get_collection(name)`（不存在 → `chromadb.errors.NotFoundError`），
   捕获 `ChromaError` 后抛 `CorpusMissing`；**集合存在但 `count() == 0` 也抛 `CorpusMissing`**
   （索引为空 = 语料没建好，与"索引不存在"同因）。文案含
   `请先跑 python -m pyassist.rag.ingest`。
   → **这条让 §3.2 的 `CorpusMissing → 503` 从死路径变成可达路径**：若读路径也用
   `get_or_create_collection`，它会静默建一个空集合、永远返回 `[]` → 200，那个错误码就成了摆设。

**客户端缓存**：`@lru_cache` 缓在**私有函数 `_client_for(path: Path)`** 上，`get_client(path=None)`
负责把 `None` 解析成 `config.CHROMA_DIR`。`Path` 可哈希，测试的 `tmp_path` 天然是不同 key。
不缓存的话每次 `retrieve()` 都新开一个 sqlite 连接 + 遥测线程，纯浪费。

### D4 · `rag/corpus.py`：语料抓取与解析，**纯分层**

```python
class CorpusDocument:  path: Path; doc_url: str; source_url: str; html: str
class Block:           text: str; section: str | None; anchor: str | None; kind: Literal["para", "code"]
class ParsedDocument:  doc_url: str; title: str; blocks: list[Block]
class FetchReport:     documents: int; bytes_written: int; source_url: str

def parse_document(document: CorpusDocument) -> ParsedDocument          # 纯函数，bs4，全离线可测
def iter_corpus_documents(corpus_dir: Path) -> list[CorpusDocument]     # 排序，确定性
def fetch_official_docs(target_dir: Path) -> FetchReport                # 下载 + 安全解压 + 目录镜像
def fetch_peps(target_dir: Path) -> FetchReport                         # peps.json + 固定清单正文
def resolve_docs_archive_url(client: httpx.Client) -> str               # 两步解析
```

四条硬约束：
- **不得 import `chromadb` / `rag.embedder` / `rag.store` / `rag.ingest`**（否则它就成了第二个所有者）。
  由守护 #21 / #26 拦。
- 抓取用**两步解析**（先读 `download.html` 抽 zip 链接，再回退常量），见 §2.4。
- `fetch_official_docs` **手动遍历 `infolist()` 逐成员校验路径**：拒绝绝对路径、拒绝 `..` 分量、
  拒绝符号链接，最后再断言目标落在 `target_dir` 之内 —— **不用 `extractall`**（zip-slip）。
  httpx **必须显式传 `timeout=`**。别指望 ruff 一定会拦，这两条是安全要求，不是 lint 要求。
- `parse_document` 是**纯函数**（入参是已读好的 HTML 字符串，不出网）→
  这是 §5.1「测试不依赖网络」能成立的关键，也是把抓取与解析拆成两个所有者之外、再拆成两个函数的原因。

`doc_url` 与 `source_url` 的分工：`doc_url` 是**页面** URL（无锚点，用于按页归组、`read_doc`、
以及 `qa.jsonl` 的 `relevant_urls` 匹配）；`source_url` 是**带锚点**的可点击 URL（用于引用）。

### D5 · 分块：按 HTML 标题层级结构化分块，**不引 `langchain-text-splitters`**

- 纯字符数切会把 `<pre>` 代码块、`<dt>/<dd>` 定义从签名处劈开 → 直接压低 Recall@5 与引用准确率。
- 通用 HTML→Markdown 转换器（langchain 那条路）会**丢锚点**，而锚点正是
  「引用能定位到具体片段」的关键。
- 做法：
  1. 按 `h1/h2/h3` 分段 → 段内按 `<p>` / `<li>` / `<dt>` / `<dd>` 聚合到 `CHUNK_MAX_CHARS`（1000）；
  2. 相邻块重叠 `CHUNK_OVERLAP_CHARS`（150），且**落在段落边界**（不在词中间切）；
  3. `<pre>` **尽量原子不切**（超过 2×max 才按行退让）；
  4. 最近的标题链作为 `section`，该标题的 `id` 作为 `anchor`；
  5. **正文块超 `CHUNK_MAX_CHARS` 一律按词边界拆开**（2026-10-13 补，见 §10 第 13 条）。
     第 3 条的原子性是**只给代码的豁免**：`<li>` 里嵌 `<ul>` 时，`get_text(" ")` 会把整棵
     子树展平成一个块（Python 官方文档最大 47723 字符，`contents.html`）—— 它既是一条
     概括整页目录的垃圾向量，也是嵌入期 OOM 的入口。正文按空白切不损失语义，
     代码按空白切会切出半行程序，两者不该同规。
- **嵌入文本 = `f"{title}\n{section}\n{body}"`**（标题上下文前缀对小语料 Recall 提升明显），
  但 `documents` 存**纯 body** —— 这样引用片段是干净正文，不带重复标题。
- `chunk_id = sha256(f"{source_url}\0{body}".encode()).hexdigest()[:32]`。
  严格按 §5.2 的「**内容哈希 + 来源 URL**」派生：**位置无关**，不含 ordinal。
  代价是同一页里完全重复的段落会合并成一条（那是去重，不是 bug）；ordinal 仍落 metadata 供排序。
- `source_url` **必须带锚点**（`.../asyncio-task.html#asyncio.gather`），
  否则引用只能落到文档顶部，§5.2 的引用准确率与体验双输。

### D6 · `rag/retriever.py`：`to_thread` 包住「嵌入 + 查询」**整段**

```python
class RetrievedChunk:   # frozen dataclass
    chunk_id: str; text: str; doc_url: str; source_url: str
    title: str; section: str | None; score: float

async def retrieve(query, *, top_k=DEFAULT_TOP_K, min_score=RETRIEVAL_MIN_SCORE,
                   collection=None, embedder=None) -> list[RetrievedChunk]
async def fetch_document(doc_url, *, collection=None) -> list[RetrievedChunk]

class IndexProbe:      # frozen dataclass
    declared: int      # count() 说的条数（元数据段，可能说谎）
    requested: int     # 实际向索引索要的条数 = min(declared, _PROBE_MAX_RESULTS)
    readable: int      # 索引真正还回来的条数
def probe_index_depth(collection, *, max_results=_PROBE_MAX_RESULTS) -> IndexProbe   # 同步阻塞
```

`probe_index_depth` 是 ingest 的验收判据，住在**本模块**而不是 `rag/ingest.py`：
§2 把"读向量库"整体划给了 `rag/retriever.py`（守护 #24），让 ingest 自己
`collection.query()` 会开出第二个读入口。它读的不是"某个问题命中什么"，而是
"这个索引到底读得出多少条" —— `count()` 读元数据段、`query()` 才真正走 HNSW，
两者撕裂正是 2026-10-13 事故的形态（§10 第 15 条）。

- `async def retrieve(...)` 内**只做一次** `await asyncio.to_thread(_search, ...)`，
  `_search` 是同步核心（解析集合 → 校验 → 嵌入 → `collection.query` 全在里面）。
  **不能只把 `embed_query` 丢进线程**：Chroma 客户端是同步阻塞的，`collection.query` 一样会堵
  事件循环（§3.4「异步优先」）。
- 模型**加载**（下载 + torch 初始化）是重阻塞，绝不能发生在事件循环里 ——
  它在 `to_thread` 内的首次 `embed_*` 里完成，或由 `warm_up()` 预热。
- **解析顺序固定**（这个顺序本身是可测的契约，§7 判据 6/7 依赖它）：

  ```
  1. collection := 入参 or store.open_collection()          # 缺失/空 → CorpusMissing（廉价）
  2. expected   := embedder.model_id if embedder else settings.embedding_model_id
  3. store.assert_index_compatible(collection, model_id=expected)   # 漂移 → ConfigError（廉价）
  4. embedder   := 入参 or rag.embedder.get_embedder(settings.embedding_model_id)   # 此刻才加载模型
  5. 嵌入 + 查询
  ```

  第 2/3 步**故意排在加载模型之前**：`settings.embedding_model_id` 只是一个字符串，
  不必先把 2GB 的模型拉起来才发现索引是别的模型建的。测试注入 embedder 时用其 `model_id`
  做期望值 —— 语义是「这个集合必须是用**我实际要用的这个 embedder**建的」，无需打桩 `settings`。
- `retrieve()` 无命中返回 `[]`，**不抛异常**。`RetrievalEmpty` 由阶段六的图节点在 `not chunks` 时抛：
  这样"文档里没有"可以在图里走一条正常分支，而不是靠异常做控制流（§3.2 也把它定为 200）。
  两个 API 的边界因此唯一、无歧义。
- `score = 1.0 - distance`，按 score 降序（Chroma 已按 distance 升序，**仍然显式重排一次**为防御）。
  `min_score` 默认 `0.0`（不过滤）。
- 打点 `retrieval.done`：`doc_ids`（§3.5 冻结字段名）+ `scores`（**与 doc_ids 按位对齐**）+
  `latency_ms` + `top_k` + `model_id`。
- `fetch_document` 是给阶段六 `read_doc` 工具预留的"读整页"路径（防阶段六回头改本文件）：
  入参 URL **先剥掉 fragment**，按 `where={"doc_url": ...}` 取该页全部 chunk，按 `ordinal` 升序返回。

### D7 · 索引漂移校验：读 metadata，**不靠写入**

`ingest` 建集合时写入 `{"embedding_model_id", "dimension", "space", "chunker_version"}`（C11 允许的类型）；
`ingest` 与 `retriever` 取到集合后先 `assert_index_compatible`：

1. 比 `collection.metadata["embedding_model_id"]` 与期望值 → 不一致抛 `ConfigError`，
   文案直接指明「删 `data/chroma` 后重跑 ingest」（与 §4「重建 = 删目录」对齐）。
2. 校验 `space == "cosine"`，取值为**主证 metadata + 佐证 `configuration_json`**：
   metadata 是我们自己写的、形状可控；`configuration_json` 是 `Dict[str, Any]` 且**内部形状跨版本
   不保证**（C12），所以用一个防御式 `_find_string_value(obj, "space")` 递归找 key，找不到就
   `debug` 记一笔、以 metadata 为准。找到且不是 cosine → 抛（因为 `score = 1 - distance` 只在
   cosine 下成立，C7）。
3. **必须读而不是写**：`get_or_create_collection` 对已存在的集合忽略传入的 metadata（C9）。
4. 捕获 `InvalidDimensionException` / `ChromaError` → 转 `ConfigError` 并附同样的"删库重建"提示（C10）。

这是防「换模型忘了重建索引」这类**静默给垃圾排序**的经典事故 —— 它不报错，只是答案悄悄变差。

### D8 · 引用：编号一经产生**永不重排**

```python
class Citation:  # frozen dataclass
    index: int; chunk_id: str; title: str; section: str | None
    doc_url: str; url: str; snippet: str

def build_citations(chunks: Sequence[RetrievedChunk], *, snippet_chars=240) -> list[Citation]
def render_reference_block(citations: Sequence[Citation]) -> str
def extract_markers(answer: str) -> list[int]
def find_ungrounded_markers(answer: str, citations: Sequence[Citation]) -> set[int]
```

- **编号 = 检索序位（rank + 1），chunk 与 Citation 一一对应，不去重、不重排。**
  同一页的两个 chunk 拿两个编号、指向同一 `doc_url` 的**不同锚点** ——
  这比按 URL 去重**更有利于 grounding**（引用准确率核验是"引用 → 片段"级别的）。
- 冻结：阶段六的 prompt 必须复用 `build_citations` 产出的 `index`，**禁止自行编号**。
  这是 §5.2「引用准确率 ≥ 0.90」的前置 —— 编号与最终引用一旦错位，那个门直接失守。
- `Citation.chunk_id` 与 `doc_url` 必备：阶段六要做「引用 → 片段」的 grounding 核验，
  以及 `qa.jsonl` 的「按页面路径匹配」。
- `snippet`：折叠空白后取前 `snippet_chars` 字符，截断则补 `…`。阶段六可直接给前端做预览。
- `render_reference_block` 产出可点击 Markdown：
  `1. [标题](url) — 小节`，空列表返回 `""`（前端据此判断"无来源"）。
- `extract_markers` 用 `\[(\d+)\]` 抽标记，**按出现顺序去重**。
- `find_ungrounded_markers` = 答案里出现、但没有对应 Citation 的标记（越界标记），
  供阶段六决定是否重生成。**它不在本阶段抛错** —— 本阶段只提供判定，处置权在阶段六的图里。

### D9 · 幂等：**先收敛后写入**，不只是盲目 upsert

纯 `upsert` 永不删除：语料删了一篇、或改了 chunk 参数，重跑后 `count()` 只增不减。
`ingest_corpus()` 的顺序是：

```
1. 遍历语料（按 repo 相对 posix 路径排序，保证确定性）→ 解析 → 分块，得到全部 desired 记录
2. existing = set(collection.get(include=["metadatas"])["ids"])
3. stale = existing - desired_ids（--paths 模式下只在这些文档的 doc_url 范围内取 existing）
   → collection.delete(ids=sorted(stale))     # sorted：让日志与测试可复现
4. 分批 collection.upsert(ids=..., embeddings=..., documents=..., metadatas=...)   # 批大小 256
```

→ 同一语料跑两次：id 集合与 `count()` **完全一致**；删掉一篇文档再跑：它的 chunk **从库里消失**。
`delete` 仍在 `rag/ingest.py` 内（仍是"唯一写向量库的地方"，不破单点所有权）。
`IngestReport(documents, chunks, upserted, deleted, duration_ms)` + 打点 `ingest.done`。

**`--paths` 的收敛范围必须收窄**（否则单文档重建会删光其余文档）：
`prune_stale(collection, desired_ids, *, doc_urls=None)`，`doc_urls` 非空时只在这些页面的
既有 chunk 里做差集 —— 这个参数是防"开发期单页重建变清库"的**安全阀**，由测试覆盖。

### D10 · 测试必须离线：确定性**哈希**嵌入

`tests/fakes/embedder.py` 里 `HashingEmbedder`：token → **`hashlib.sha256`** → 特征哈希 → 64 维 → L2 归一化。

- ⚠️ **绝不能用 Python 内建 `hash()`** —— 它对 `str` 是按进程加盐随机的（`PYTHONHASHSEED`），
  会让测试**跨进程 flaky**。这类 bug 极难查，必须在规格里点名。
- 特征哈希让"共享词汇的 query 与 chunk 余弦相似度更高"，因此检索测试**是有意义的**，不是纯噪声。
- 但 64 维的 fake 与 1024 维的 bge-m3 **不能共用一个集合** → **每个测试用独立的临时集合**
  （`tmp_path` 派生），否则 `InvalidDimensionException`（C10）。
- 夹具注入全部走**参数**（`ingest_corpus(..., collection=..., embedder=...)` /
  `retrieve(..., collection=..., embedder=...)`），**不在 `src/` 里开测试后门**（D3 第 3 条）。
- `tests/fixtures/corpus/` 放小体量 HTML（镜像 §2.5 布局），**不依赖任何下载**。

### D11 · `qa.jsonl` 的 schema 现在就冻结

§4 规定它「只能新增，禁止修改已有用例」—— schema 要是错了就得开新数据集。所以现在定死：

```json
{"id": "qa-001",
 "question": "asyncio.gather 里一个任务抛异常会怎样？",
 "relevant_urls": ["https://docs.python.org/3.12/library/asyncio-task.html"],
 "expected_facts": ["默认会取消其余任务", "return_exceptions=True 时不取消"],
 "topic": "asyncio"}
```

- `relevant_urls` 是 Recall@5 的 ground truth → **按页面路径匹配（忽略锚点）**，
  因为同一个 URL 在库里会派生出成百上千个带不同锚点的 chunk。因此它对齐的是
  `RetrievedChunk.doc_url` 而非 `source_url`（D4/D6 分两个字段正是为了让这一步不需解析字符串）。
- `expected_facts` 供阶段六判"引用是否真的支撑结论"。
- 规模 ≥ 40 条，覆盖 8 个核心章节：asyncio / typing / dataclasses / pathlib / json /
  异常 / 装饰器 / 生成器。
- `tests/test_eval_dataset.py` 断言：JSONL 可解析、`id` 唯一、字段齐全、`expected_facts` 非空、
  条数 ≥ 40、`relevant_urls` 都以 `DOCS_BASE_URL` 或 `PEPS_BASE_URL` 开头。
- **本阶段不写跑分器**（§1.2），但 schema 与 `doc_url` 的对齐关系现在就锁死。

### D12 · 新增 7 条契约守护（#20–#26），匹配面**必须收窄**

阶段四的 #14–#19 用 `_call_name()` 取末段名做精确集合匹配。对 `.upsert(` / `.query(` 这种
**通用方法名太宽**（会命中任何领域层的 upsert/query），必须升级为**按签名特征匹配**：
看 `ast.Call` 的关键字实参名，而不只看方法名。

| # | 断言 | 匹配写法 |
|---|---|---|
| 20 | Chroma 客户端只在 `rag/store.py` 构造 | 调用名 ∈ {`PersistentClient`, `EphemeralClient`, `HttpClient`}，允许面 = `rag/store.py`（**不含裸 `Client(`** —— 太宽） |
| 21 | `chromadb` 只被 `rag/` 的三个文件 import | `ast.Import` / `ImportFrom` 模块根 == `chromadb`；允许面 = {`rag/store.py`, `rag/ingest.py`, `rag/retriever.py`}（这行同时把 `corpus.py` 挡在外面） |
| 22 | `sentence_transformers` / `SentenceTransformer(` 只在 `rag/embedder.py` | 静态 import + 构造调用；**不能用动态 `importlib.import_module` 绕过**（它返回 `Any`，会另触 mypy `no-any-return`） |
| 23 | `.upsert(` **且带 `ids=` 关键字实参** 只在 `rag/ingest.py` | 关键字特征匹配（Chroma upsert 的签名指纹） |
| 24 | `.query(` **且带 `query_embeddings=` 关键字实参** 只在 `rag/retriever.py` | 同上；不加关键字约束会误伤 `session.query(...)` 与将来的领域查询 |
| 25 | `src/` 内不出现 `query_texts=` 关键字、不出现 `DefaultEmbeddingFunction` | AST 关键字检查（**不做文本扫描** —— 会被 docstring 误伤） |
| 26 | `rag/corpus.py` 是纯分层：不 import `rag.embedder` / `rag.store` / `rag.ingest` | `ImportFrom` 模块名匹配 |

全部沿用 `_python_sources()`（AST，扫描面 = `src/`），**测试代码天然不受约束** ——
所以 `tests/fakes/embedder.py` 可以直接 import `sentence_transformers` 之外的东西、
也可以注入任意向量，不需要为它开口子。

---

## 5. 关键文件规格

以下代码是**规格草案**，不是最终代码：签名、常量、边界处理与打点字段是硬要求，
被省略的函数体（bs4 具体选择器、错误文案措辞）留给实现。

### 5.1 `config.py` 增量

```python
# ---------- RAG（阶段五，§1 / §5.2）----------
CORPUS_DIR: Final = DATA_DIR / "corpus"
CHROMA_DIR: Final = DATA_DIR / "chroma"

COLLECTION_NAME: Final = "python_docs"
DISTANCE_SPACE: Final = "cosine"          # C5/C7：不显式指定就是 l2，score 会静默变垃圾
DEFAULT_TOP_K: Final = 5                   # §5.2 的 Recall@5
RETRIEVAL_MIN_SCORE: Final = 0.0           # 不过滤；阈值待阶段六跑分回填（§10.5）

CHUNK_MAX_CHARS: Final = 1000
CHUNK_OVERLAP_CHARS: Final = 150
CHUNKER_VERSION: Final = "1"               # 分块算法一改就 bump：它进索引元数据（D7）

# 向量模型：不在 DeepSeek 这边（§1）。换模型 = 改这一行 + 删 data/chroma 重跑 ingest。
DEFAULT_EMBEDDING_MODEL_ID: Final = "BAAI/bge-m3"

# 语料来源（§2.4 / §2.5）
DOCS_VERSION: Final = "3.12"
DOCS_BASE_URL: Final = f"https://docs.python.org/{DOCS_VERSION}/"
DOCS_DOWNLOAD_PAGE_URL: Final = f"{DOCS_BASE_URL}download.html"
DOCS_ARCHIVE_URL_FALLBACK: Final = f"{DOCS_BASE_URL}archives/python-{DOCS_VERSION}-docs-html.zip"
PEPS_BASE_URL: Final = "https://peps.python.org/"
PEP_INDEX_URL: Final = f"{PEPS_BASE_URL}api/peps.json"
PEP_NUMBERS: Final = (8, 20, 257, 484, 585, 604, 634, 695)   # 风格 / 类型系统 / match 语法

CORPUS_EXCLUDE_DIRS: Final = frozenset({"_sources", "_static", "_modules", "_images"})
CORPUS_EXCLUDE_FILES: Final = frozenset(
    {"genindex.html", "py-modindex.html", "search.html", "404.html"}
)

# 评估集（§4 冻结件）
EVAL_DATASETS_DIR: Final = REPO_ROOT / "src" / "pyassist" / "evals" / "datasets"
QA_DATASET_PATH: Final = EVAL_DATASETS_DIR / "qa.jsonl"
```

`Settings` 的两处改动：

```python
    # 阶段五接上：默认值引用常量，不再允许 None（None 会让每次调用都要判空）
    embedding_model_id: str = Field(default=DEFAULT_EMBEDDING_MODEL_ID, min_length=1)
    daily_token_limit: int | None = None   # 不变
```

**要点**：模型 id 仍然**只出现在 `config.py` 一处**（§2 单点所有权）。
`PEP_NUMBERS` / `CHUNK_*` / `DOCS_*` 是「随代码评审变更」的模块常量，
`embedding_model_id` 是「随部署环境变更」的 Settings 项 —— 两者都在本文件，仍然只有一个入口。

### 5.2 `rag/embedder.py`

```python
EmbeddingVector = list[float]


class Embedder(Protocol):
    """唯一向量化出口的形状（§2）。同步 —— 见 D2 的三条理由。"""

    @property
    def model_id(self) -> str: ...
    @property
    def dimension(self) -> int: ...
    def embed_documents(self, texts: Sequence[str]) -> list[EmbeddingVector]: ...
    def embed_query(self, text: str) -> EmbeddingVector: ...
    def count_tokens(self, texts: Sequence[str]) -> list[int]: ...   # §10 第 14 条


class SentenceTransformerEmbedder:
    def __init__(self, model_id: str) -> None:
        from sentence_transformers import SentenceTransformer  # 惰性：import 期不碰 torch

        self._model_id = model_id
        self._model = SentenceTransformer(model_id)
        self._model.max_seq_length = EMBEDDING_MAX_SEQ_TOKENS   # 内存闸门（§10 第 13 条）

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return int(self._model.get_sentence_embedding_dimension())

    def embed_documents(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        encoded = self._model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        # `encoded` 是 Any（上游无 stub）：先落到显式注解的局部变量再返回，
        # 否则 mypy --strict 的 no-any-return 会红。别改成直接 return。
        vectors: list[EmbeddingVector] = [[float(x) for x in row] for row in encoded]
        return vectors

    def embed_query(self, text: str) -> EmbeddingVector:
        return self.embed_documents([text])[0]

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        # 切片，不一次喂全量：truncation=False 会把全部 input_ids 吐回来，
        # 全量一次调用是百万级 int 同时驻留。内存纪律归本模块（同 max_seq_length）。
        return [
            token_lengths(self._model.tokenizer, texts[i : i + _TOKENIZE_BATCH_SIZE], MAX)
            for i in range(0, len(texts), _TOKENIZE_BATCH_SIZE)
        ]


@cache
def get_embedder(model_id: str) -> Embedder:
    """唯一模型实例工厂。functools.cache：同一进程内一个模型只加载一次。"""
    return SentenceTransformerEmbedder(model_id)


def warm_up(model_id: str) -> None:
    """显式预热（下载 + torch 初始化）。ingest CLI 与阶段六 lifespan 用。"""
    get_embedder(model_id)


def reset_embedder_cache() -> None:
    """换模型 / 测试收尾。唯一清缓存的地方。"""
    get_embedder.cache_clear()
```

**要点**：
- `normalize_embeddings=True` 与 cosine 度量是**配套**的（D3 第 2 条）。归一化后的向量
  cosine 距离才落在 [0,2] 且 `1-distance` 是干净的相似度。
- `get_embedder` 的入参是**必填** `str`，不再允许 `None`：调用方一律显式传
  `settings.embedding_model_id`，避免"有的地方传 None 有的地方传常量"两种语义。
- `dimension` 走 `get_sentence_embedding_dimension()`，**不是**硬编码 1024
  （换 `multilingual-e5-small` 时它是 384，硬编码会让索引元数据撒谎）。

### 5.3 `rag/store.py`

```python
def get_client(path: Path | None = None) -> chromadb.ClientAPI:
    return _client_for(path or CHROMA_DIR)


_clients: dict[Path, chromadb.ClientAPI] = {}   # 手写缓存，不用 lru_cache —— 见 close_clients()


def _client_for(path: Path) -> chromadb.ClientAPI:
    client = _clients.get(path)
    if client is None:
        client = chromadb.PersistentClient(
            path=str(path),
            settings=ChromaSettings(anonymized_telemetry=False),   # C2：不是可选项
        )
        _clients[path] = client
    return client


def build_index_metadata(*, model_id: str, dimension: int) -> dict[str, str | int | float | bool]:
    return {
        "embedding_model_id": model_id,
        "dimension": dimension,
        "space": DISTANCE_SPACE,
        "chunker_version": CHUNKER_VERSION,
    }


def get_or_create_collection(*, client=None, path=None, metadata=None) -> Collection:
    """写路径（ingest 用）。读路径用 open_collection()。"""
    return (client or get_client(path)).get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=None,                       # C3：掐死 Chroma 自带的 ONNX 嵌入
        configuration={                                # C5：默认是 l2
            "hnsw": {
                "space": DISTANCE_SPACE,
                "sync_threshold": HNSW_SYNC_THRESHOLD,  # §10 第 15 条：默认 1000 会丢数据
                "batch_size": HNSW_BATCH_SIZE,          # 必须 ≤ sync_threshold
            }
        },
        metadata=metadata,
    )


def open_collection(*, client=None, path=None) -> Collection:
    """读路径（retriever 用）。不存在或为空 → CorpusMissing（让 §3.2 的 503 可达）。"""
    ...


def assert_index_compatible(collection: Collection, *, model_id: str) -> None:
    """索引元数据与当前配置一致性校验（D7）。不一致 → ConfigError。"""
    ...


def close_clients() -> None:
    """唯一的关闭点：关掉并丢弃所有缓存客户端（SPEC §10 第 15 条）。"""
    for client in _clients.values():
        cast("ChromaClient", client).close()   # ClientAPI 没声明 close()，只有磁盘实现有
    _clients.clear()


def reset_client_cache() -> None:
    """只丢弃、不关闭 —— 测试间隔离用（见下面的分工说明）。"""
    _clients.clear()
```

**要点**：
- `open_collection` 只 `get_collection(name)`，**绝不** `get_or_create` —— 见 D3 第 4 条。
- 缓存**手写**而不 `lru_cache`：`cache` 列不出已缓存的条目，`close_clients()` 就写不出来。
- `close_clients()` / `reset_client_cache()` 的分工见 D3 —— 前者收尾（让 compactor 落盘），
  后者只管隔离。**合二为一是 bug**。
- `assert_index_compatible` 内部：
  1. `collection.metadata` 可能是 `None`（不是本模块建的集合）→ 抛 `ConfigError` 并提示删库重建；
  2. 比 `embedding_model_id`；
  3. 比 `space`（主证 metadata + 佐证 `_find_string_value(collection.configuration_json, "space")`，C12）；
  4. 比 `chunker_version` 与 `config.CHUNKER_VERSION` → 不一致**只 warn 不抛**：
     分块改了不会让相似度失真，只会让库里混着两代切法；重建是期望动作，但不阻塞检索；
  5. `_warn_if_legacy_sync_threshold` → 索引是用小阈值（Chroma 默认 1000）建的也**只 warn 不抛**：
     库读得没问题，但重建要几小时，该由人挑时间付这笔钱（§10 第 15 条）。
- `open_collection` / `assert_index_compatible` 捕获 `chromadb.errors.ChromaError`
  （含 `NotFoundError` / `InvalidDimensionException`，C10）→ 按语义转
  `CorpusMissing` 或 `ConfigError`，**不允许裸 `ChromaError` 冒到路由层**
  （§3.2 的错误码枚举里没有它，冒上去只能落 `INTERNAL_ERROR`，那是在丢信息）。
- **`assert_not_empty` 也必须接住 `count()` 抛出的 `ChromaError`**（2026-10-13 补，
  §10 第 15 条）：索引段损坏时 `count()` 抛 `InternalError: ... Error loading hnsw index`，
  而 `InternalError` 是 `ChromaError` 子类。实测那个真实事故库：`metadata` 与
  `configuration_json` 读得出来（走 sqlite 目录表），只有 `count()` / `get()` / `query()`
  会抛 —— 所以 `count()` 是唯一的修复点，且它必须在 `try` 里（原来它写在 `try` 外，
  于是裸异常直接冒到路由层，把"重建索引"误导成"排查代码"）。转成 `CorpusMissing → 503`：
  库在磁盘上但读不出来，就是"引擎没准备好"，不是 500。

### 5.4 `rag/corpus.py`

```python
@dataclass(frozen=True)
class CorpusDocument:
    path: Path
    doc_url: str
    source_url: str      # 建块后会被锚点改写；此处 = doc_url
    html: str


@dataclass(frozen=True)
class Block:
    text: str
    section: str | None
    anchor: str | None
    kind: Literal["para", "code"]


@dataclass(frozen=True)
class ParsedDocument:
    doc_url: str
    title: str
    blocks: list[Block]


@dataclass(frozen=True)
class FetchReport:
    documents: int
    bytes_written: int
    source_url: str


def iter_corpus_documents(corpus_dir: Path) -> list[CorpusDocument]:
    """扫 corpus_dir/docs/**/*.html 与 corpus_dir/peps/**/*.html，按相对 posix 路径排序。

    确定性是幂等的前提之一（D9 第 1 步），所以是 list 不是 Iterator。
    """
    ...


def parse_document(document: CorpusDocument) -> ParsedDocument:
    """纯函数：HTML 字符串 → 标题 + 有序块（带 section/anchor）。"""
    ...


def fetch_official_docs(target_dir: Path) -> FetchReport: ...
def fetch_peps(target_dir: Path) -> FetchReport: ...
def resolve_docs_archive_url(client: httpx.Client) -> str: ...
```

**要点**：
- `parse_document` 的实现骨架：`BeautifulSoup(html, "html.parser")`（标准库解析器，不引 lxml）；
  `title` 取 `<title>` 并剥掉 `" — Python 3.12.x documentation"` 尾巴；
  文档体取 `div.body`（Python 官方文档的容器），退化为 `<article>` / `<body>`；
  维护一个标题栈（`h1/h2/h3` 的 `id` + 文本），每遇块级元素
  （`p` / `li` / `dt` / `dd` / `pre` / `div.highlight`）产出一个 `Block`；
  **必须剥掉 `.headerlink`（¶ 锚符号）与 `script` / `style` / `nav`**，否则每个块尾巴都挂一个 `¶`。
- `iter_corpus_documents` 的 URL 映射（§2.5）：

  | 落盘相对路径 | `doc_url` |
  |---|---|
  | `docs/library/asyncio-task.html` | `https://docs.python.org/3.12/library/asyncio-task.html` |
  | `peps/pep-0008.html` | `https://peps.python.org/pep-0008/` |

  排除 `CORPUS_EXCLUDE_DIRS` / `CORPUS_EXCLUDE_FILES`；**前缀外的目录整个跳过**（不报错）。
- `resolve_docs_archive_url`：`client.get(DOCS_DOWNLOAD_PAGE_URL)` → 在 HTML 里找
  `href="…docs-html.zip"` → `urljoin` 成绝对 URL；找不到则返回 `DOCS_ARCHIVE_URL_FALLBACK`
  并 `warning` 记一笔（§2.4 的两步解析）。
- `fetch_official_docs` 的安全解压（§4「语料是一切回答的事实来源」）：
  逐 `infolist()` 成员校验，**拒绝**绝对路径 / 含 `..` / 符号链接 / 目标落在 `target_dir` 之外，
  再 `zf.read(info)` 写盘（不用 `extractall`）；解压到临时目录后把 zip 顶层目录的**子项**
  移入 `target_dir`（同名覆盖，**不删目录** —— §4 只允许抓取流程写 `data/corpus/**`，
  不允许它顺手清空用户手工放进去的东西）。所有 httpx 调用**显式传 `timeout=`**。
- `fetch_peps`：先取 `PEP_INDEX_URL` 校验 `PEP_NUMBERS` 里的 PEP 确实存在（数量不符就
  `error` 记日志但不中断），再逐个抓 `{PEPS_BASE_URL}pep-{n:04d}/` 落成 `peps/pep-{n:04d}.html`。

### 5.5 `rag/ingest.py`

```python
@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    body: str          # 存进 collection 的 documents（纯正文，D5）
    embed_text: str    # 送去嵌入的文本（title + section + body）
    doc_url: str
    source_url: str    # 带锚点
    title: str
    section: str | None
    ordinal: int


@dataclass(frozen=True)
class IngestReport:
    documents: int
    chunks: int
    upserted: int
    deleted: int
    duration_ms: int


def build_chunks(document: ParsedDocument, *, max_chars=CHUNK_MAX_CHARS,
                 overlap_chars=CHUNK_OVERLAP_CHARS) -> list[Chunk]: ...


def ingest_corpus(corpus_dir: Path, *, collection: Collection, embedder: Embedder,
                  paths: Sequence[str] | None = None) -> IngestReport: ...


def prune_stale(collection: Collection, desired_ids: set[str], *,
                doc_urls: Sequence[str] | None = None) -> int: ...


def upsert_chunks(collection: Collection, chunks: Sequence[Chunk], embedder: Embedder,
                  *, batch_size: int = 64) -> int: ...


def main(argv: Sequence[str] | None = None) -> int: ...


def verify_index(*, expected_chunks: int | None = None, path: Path | None = None) -> int: ...
```

CLI（`python -m pyassist.rag.ingest`）：

```
[--fetch]                    先跑 fetch_official_docs + fetch_peps，再建索引
[--corpus DIR]               默认 config.CORPUS_DIR
[--paths REL ...]            只重建这些文档（开发期快速迭代；prune 范围随之收窄，D9）
[--model-id ID]              默认 settings.embedding_model_id
[--dry-run]                  只报告将写入/删除的数量，不写库
```

`main` 的固定顺序（**这个顺序是规格的一部分**）：

```
1. embedder := rag.embedder.get_embedder(model_id)          # 需要它的 dimension 建索引元数据
2. collection := store.get_or_create_collection(metadata=build_index_metadata(...))
3. store.assert_index_compatible(collection, model_id=...)  # 漂移 → ConfigError（提示删库重建）
4. (--fetch) fetch_official_docs / fetch_peps
5. documents := iter_corpus_documents(corpus_dir)
6. chunks := [c for d in documents for c in build_chunks(parse_document(d))]
7. prune_stale(...) → upsert_chunks(...)
8. logger.info("ingest.done", **asdict(report))
9. store.close_clients()                                      # compactor 在这里落盘
10. verify_index(expected_chunks=report.chunks)               # 关掉→重开→真读一遍
11. logger.info("ingest.verified", count=...)
```

**退出码**（阶段八的 `docker compose` 与 CI 直接依赖它）：

| 码 | 含义 | 下一步 |
|---|------|--------|
| 0 | 写入完成**且**验收通过 | 无 |
| 1 | ingest 本身失败（语料/模型/配置） | 看 `ingest.failed` 的 `code` |
| 2 | 写进去了，但**读不回来** | 删掉 `data/chroma` 下那个 UUID 段目录重跑；向量还在 WAL 里，**不必重新嵌入** |

**要点**：
- **`ords` / `chunk_id` 不含 ordinal**（D5）：`chunk_id = sha256(f"{source_url}\0{body}")[:32]`。
- 分批 upsert 不是过早优化：Chroma 单次写入过大会撞 `BatchSizeExceededError`。
  **上界由嵌入内存定，不是由 Chroma 定**（2026-10-13 改，见 §10 第 13 条）：
  掩码内存 O(batch × seq²)，于是 batch=64 配 `EMBEDDING_MAX_SEQ_TOKENS=1024`。
- `build_chunks` 的产出上界是 `_MAX_BODY_CHARS`（= 2×`CHUNK_MAX_CHARS` + `CHUNK_OVERLAP_CHARS` + 2，
  代码块的原子性豁免已计入）。**超上界就 `warn`**（`ingest.chunk_oversize`）——
  它是"块没被拆开"的唯一信号，而块没拆开正是 OOM 的前一步。
  别按 `max_chars + overlap` 收紧它：那会让健康语料每次报 73 条，警告随之失效。
- `upsert` / `delete` 是**唯一的两处写库调用**，都在本文件（守护 #23 只看 upsert，
  `delete` 靠"只有本文件 import chromadb"这条（#21）覆盖）。
- `parse_document` 的 `Block` 里带 anchor，但 `Chunk.source_url` 的锚点取**该块所属 section**
  的 `anchor`；同一 section 内的多个块共享同一锚点 —— 这正确，因为点击锚点会落到该小节顶部。
- 空语料（`documents == []`）→ 抛 `CorpusMissing`（§3.2：语料缺失是 503，不是"建了个空索引"）。
- **"写入完成"不等于"入库完成"**（2026-10-13 补，见 §10 第 15 条）：
  `ingest.done` 只说明 upsert 调用返回了。要报成功，必须再走第 9–10 步 ——
  **关掉客户端**（compactor 落盘的唯一机会）后**重开**，并**多要几条检索结果**。
  `count()` 不能当验收：实测删掉损坏的段目录后它仍报 17366，而检索只找得到 983 条。
  验收不过 → 退出码 2 + `ingest.verify_failed`。
- **判定用 `probe.is_readable`，不是 `readable == requested`**（2026-10-13 补）。
  HNSW 是**近似**最近邻，`n_results == count` 是退化查询 —— 实测五个独立进程
  读同一个健康库，短缺分别是 0、0、1、0、0 条。要求严格相等会让验收以约 20% 的概率
  误报，而事故的形态是短 16383 条（差两个数量级）。容差 1%
  （`retriever._PROBE_TOLERANCE_RATIO`，至少 1 条），仍能抓住事故。
  `ingest.probe` 会把 `declared / requested / readable / shortfall / allowed_shortfall`
  五个数字都打进日志 —— 验收报"通过"时也要能看出它离边界有多远。

### 5.6 `rag/retriever.py`

见 D6 的签名与固定解析顺序。补充实现细节：

```python
def _search(query: str, *, top_k: int, min_score: float,
            collection: Collection | None, embedder: Embedder | None) -> list[RetrievedChunk]:
    coll = collection or store.open_collection()
    expected_model = embedder.model_id if embedder is not None else settings.embedding_model_id
    store.assert_index_compatible(coll, model_id=expected_model)
    emb = embedder or rag_embedder.get_embedder(settings.embedding_model_id)

    started = time.perf_counter()
    result = coll.query(
        query_embeddings=[emb.embed_query(query)],     # 守护 #24 的签名指纹
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )
    chunks = _rows_to_chunks(result, min_score=min_score)   # 处理两层嵌套 + None（C6）
    logger.info("retrieval.done", doc_ids=[c.chunk_id for c in chunks],
                scores=[round(c.score, 6) for c in chunks],
                latency_ms=..., top_k=top_k, model_id=emb.model_id)
    return chunks


async def retrieve(query: str, *, top_k: int = DEFAULT_TOP_K,
                   min_score: float = RETRIEVAL_MIN_SCORE, collection=None, embedder=None):
    return await asyncio.to_thread(_search, query, top_k=top_k,
                                   min_score=min_score, collection=collection, embedder=embedder)
```

- `_rows_to_chunks` 必须处理 `result["documents"]` / `metadatas` / `distances` 为 `None`
  或空列表的情况（`or []`），以及**内层 `None` 元素**（Chroma 类型标注是
  `list[list[Document | None]]` 之类）—— mypy `--strict` 会强制你写出来，别 `type: ignore`。
- metadata 取值要过 `_metadata_str(meta, key)` / `_metadata_int(meta, key)` 这类窄化辅助函数，
  因为 `Metadata` 的值类型是 `str | int | float | bool`。
  缺 `title` / `section` 时给 `""` / `None`，**不要**让缺字段把整条检索打挂。
- `logger` 用 `logging.get_logger(__name__)`；`doc_ids` / `scores` 严格按位对齐（§3.5）。

### 5.7 `rag/citations.py`

见 D8 的签名。补充：

```python
_MARKER_RE = re.compile(r"\[(\d+)\]")


def build_citations(chunks: Sequence[RetrievedChunk], *, snippet_chars: int = 240) -> list[Citation]:
    return [
        Citation(index=i + 1, chunk_id=c.chunk_id, title=c.title, section=c.section,
                 doc_url=c.doc_url, url=c.source_url, snippet=_snippet(c.text, snippet_chars))
        for i, c in enumerate(chunks)
    ]
```

- `_snippet`：`re.sub(r"\s+", " ", text).strip()` 后截断，超长补 `"…"`。
- `render_reference_block`：
  ```
  **来源**
  1. [asyncio.gather](https://…/asyncio-task.html#asyncio.gather) — asyncio.gather
  ```
  空列表返回 `""`。
- `extract_markers` 返回**按出现顺序去重**的 int 列表；`find_ungrounded_markers` 返回
  `set(markers) - {c.index for c in citations}`。
- **本模块不 import `chromadb` / `embedder`**：它只吃 `RetrievedChunk` 的形状 ——
  这是"引用构造"能与"向量库"完全解耦、并被纯单测覆盖的原因。

### 5.8 `tests/fakes/embedder.py`

```python
class HashingEmbedder:
    """确定性特征哈希嵌入：离线、跨进程稳定（D10）。"""

    def __init__(self, dimension: int = 64, model_id: str = "test-hashing-64") -> None: ...

    @property
    def model_id(self) -> str: ...
    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def count_tokens(self, texts: Sequence[str]) -> list[int]: ...   # = _TOKEN_RE 切出的词数

    def _vector(self, text: str) -> list[float]:
        # 1. tokens = re.findall(r"[a-z0-9_]+", text.lower())
        # 2. 每个 token: h = hashlib.sha256(token.encode()).digest()
        #      idx  = int.from_bytes(h[:4], "big") % self._dimension
        #      sign = 1.0 if h[4] & 1 else -1.0
        #      v[idx] += sign
        # 3. L2 归一化后返回（零向量时返回原零向量，不要除零）
        ...
```

- ⚠️ **绝不用 `hash()`**（D10）。
- `count_tokens` 返回 `_TOKEN_RE` 切出的词数 —— **与 `_vector` 同一个切法**，不是 `len(text)`。
  替身与真身的 token 口径本来就可以不同（64 维 vs bge-m3），但必须**同源**：
  若替身按字符、生产按 token，`tests/test_ingest_batching.py` 就在验证一个生产不存在的
  分批行为，绿得毫无意义 —— 而"字符序 ≠ token 序"正是 §10 第 14 条踩过的坑。
  推论：那份测试的语料不能用 `"x" * 3000` 造，因为 `[a-z0-9_]+` 会把它当成**一个** token，
  所有长度都等于 1，排序退化成恒等变换而测试照样绿。要用词表拼出真正的长度差。
- 空文本 / 无 token 的文本 → 返回全零向量，**不抛**：检索路径不该因为一条空 chunk 炸掉。
  但要注意：全零向量与任何东西的 cosine 距离都退化为 1.0，因此夹具语料里不要放空文档。
- 这个 fake 是"能进 `src/` 测试后门"的反面案例：它靠**参数注入**进入
  `ingest_corpus(..., embedder=hashing)`，`src/` 里没有任何一行知道它的存在。

### 5.9 `tests/conftest.py` 增量

```python
@pytest.fixture
def hashing_embedder() -> HashingEmbedder: ...          # 64 维

@pytest.fixture
def fixture_corpus_dir() -> Path: ...                   # tests/fixtures/corpus

@pytest.fixture
def chroma_path(tmp_path: Path) -> Path: ...            # tmp_path / "chroma"（C1：永不碰 data/chroma）

@pytest.fixture
def collection(chroma_path, hashing_embedder) -> Collection: ...   # 已 ingest 夹具语料
```

**要点**：`collection` 夹具走的是**生产的 `ingest_corpus`**（只是喂了夹具语料与 fake embedder），
不是一个"测试专用建库函数" —— 否则测试通过而生产崩。所有夹具路径都从 `tmp_path` 派生。

### 5.10 测试文件

| 文件 | 覆盖 |
|---|---|
| `test_corpus_parse.py` | 夹具 HTML → `title` 正确；`<pre>` 中的代码**不被拆**；`section`/`anchor` 映射到正确的小节；`¶` 锚符号被剥掉；`iter_corpus_documents` 顺序确定、排除清单生效、URL 映射符合 §2.5 表；`resolve_docs_archive_url` 在给一段含 zip 链接的 HTML 时能抽出（用 `respx` mock，不出网） |
| `test_ingest_idempotent.py` | 连跑两次 → id 集合与 `count()` 完全一致；改一个字符 → 该块 id 变；删掉一篇夹具文档再跑 → 它的 chunk 从库中消失（**收敛**）；`--paths` 模式只收敛指定页面、**不误删其余文档**（D9 安全阀）；`prune_stale` 返回的 deleted 数正确 |
| `test_retriever.py` | 相关 query 命中正确 `doc_url` 且排第一；`top_k` 生效；`min_score` 过滤到 `[]`（不抛）；集合不存在 → `CorpusMissing`；集合为空 → `CorpusMissing`；model_id 漂移 → `ConfigError`；space 非 cosine → `ConfigError`；`retrieval.done` 带 `doc_ids` / `scores` 且按位对齐（`structlog.testing.capture_logs`）；`fetch_document(url_with_fragment)` 按 `ordinal` 返回整页；`probe_index_depth` 在健康库上 `declared == requested == readable`、空库返回 `(0,0,0)` 且不去查、超上限时 `requested` 被截断而 `declared` 说实话、无向量可读 → `StoreError` |
| `test_citations.py` | 与 chunk 一一对应；`index == rank + 1`；`url` 来自 `source_url` 且带 `#`；snippet 截断补 `…`；空输入 → `[]` / `""`；`extract_markers("见 [1] 与 [3]") == [1, 3]`；重复标记去重；`find_ungrounded_markers("见 [1] [9]", cits) == {9}` |
| `test_eval_dataset.py` | JSONL 可解析；`id` 唯一；字段齐全且 `expected_facts` 非空；条数 ≥ 40；`relevant_urls` 前缀合法（D11） |
| `test_ingest_batching.py` | 分桶后的填充开销远低于文档顺序（**对照一份文档顺序的参照实现**）；**按 token 而非按字符** —— 构造一对"字符序与 token 序结论相反"的文本（两个 70 字母词 = 141 字符 / 2 token vs 十二个四字母词 = 59 字符 / 12 token），断言分桶听 token 的；批次由长到短；长度数与 chunk 数不符 → `ValueError`（不靠 `zip` 静默截断丢 chunk）；正序与倒序落库的 (id → 正文, metadata) **逐字节相同** |
| `test_contract_guardrails.py` | 追加 #20–#26（D12），既有 #1–#19 保持原样 |

### 5.11 契约守护 #20–#26

见 D12 的表。实现要点：
- 沿用 `_python_sources()`（AST，扫描面 `src/`）与 `_call_name()`。
- 新增一个小辅助 `_call_keywords(node: ast.Call) -> set[str]`（返回关键字实参名集合），
  供 #23 / #24 / #25 用 —— 这是"按签名特征匹配"的落地点。
- 断言**必须打印出违规的文件路径**，否则失败信息无法定位。
- 每条守护配一句 docstring 说明它守的是 `AGENTS.md`/本 SPEC 的哪一条。

### 5.12 `pyproject.toml` / `.env.example` / `AGENTS.md` 增量

```toml
# [project] dependencies 追加
  # 唯一向量化实现（阶段五）。DeepSeek 不提供 embedding（§1），故必须另选。
  "sentence-transformers>=3.0,<4.0",
  # 语料 HTML → 结构化块（阶段五）。用标准库 html.parser，不引 lxml。
  "beautifulsoup4>=4.12,<5.0",

# 新增：torch 钉到 CPU 索引，避开 Windows 上约 2.5GB 的 CUDA 轮子（D1）
[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = { index = "pytorch-cpu" }

# 新增：mypy --strict 下对无 stub 的第三方放行（§5.1）
[[tool.mypy.overrides]]
module = ["sentence_transformers.*", "torch.*", "bs4.*"]
ignore_missing_imports = true
```

**不引** `langchain-text-splitters`（D5）、`lxml`、`rank-bm25`。
`numpy` / `onnxruntime` / `tokenizers` / `huggingface_hub` 已随 chromadb 装好，**不重复声明**。

`.env.example`：`EMBEDDING_MODEL_ID=` 的注释改为
`# 留空则用 config.DEFAULT_EMBEDDING_MODEL_ID（BAAI/bge-m3）；换模型须删 data/chroma 重跑 ingest`。

`AGENTS.md`（按 §6 流程，属**新增单点所有权**，需先说明改哪一节、为什么现在不对）：

- §2 目录结构块：`rag/` 下补 `corpus.py`；
- §2 单点所有权表：新增一行

  | 能力 | 唯一归属 | 违反示例（会被打回） |
  |---|---|---|
  | 语料抓取与解析 | `rag/corpus.py` | 在 `ingest.py` 里顺手写下载/HTML 解析，或让 `corpus.py` import `rag.store` |

  理由：抓取/解析是**新能力**（网络 I/O + HTML 解析 + URL 映射），与 ingest 的
  「唯一写向量库」是两种不同的变更理由；塞进 `ingest.py` 会让"唯一写向量库"的文件同时承担抓取，
  守护 #23/#24 的边界也跟着变糊。拆开后 `parse_document` 是**纯函数**，可离线单测，
  不需碰 Chroma/embedder —— 这正是 §5.1 想要的隔离。
  **现有代码里没有平行实现**（`rag/` 目前只有 `__init__.py`），满足 §6 第 2 条。

---

## 6. 任务分解

| 任务 | 内容 | 产物 | 预估 |
|---|---|---|---|
| **T0** | 归档阶段四规格（`cmp` 校验逐行一致） | `docs/specs/阶段四-API契约与错误体系.md` | ✓ 已完成 |
| **T1** | 写阶段五 SPEC（本文件） | `SPEC.md` | ✓ 本文件 |
| **T2** | `curl -I` 核实归档 zip URL（§2.4 / §10.11）；依赖与配置：`pyproject.toml` + `config.py` 常量 + `.env.example` + `AGENTS.md` §2 登记 | 4 文件增量 | 小 |
| **T3** | `rag/embedder.py`：Protocol + ST 实现 + 单例 + `warm_up` | 1 文件 | 小 |
| **T4** | `rag/store.py`：客户端 + 集合 + 索引元数据校验 | 1 文件 | 中 |
| **T5** | `rag/corpus.py`：下载/安全解压/解析/URL 映射 | 1 文件 | **大** |
| **T6** | `rag/ingest.py`：分块 + 向量化 + 收敛写入 + CLI | 1 文件 | **大**（本阶段核心） |
| **T7** | `rag/retriever.py`：async 边界 + `to_thread` + 打点 | 1 文件 | 中 |
| **T8** | `rag/citations.py`：构造 + 渲染 + 标记核验 | 1 文件 | 小 |
| **T9** | 测试基建：`tests/fakes/` + `tests/fixtures/` + `conftest.py` 增量 + 5 个测试文件 | 9 文件 | **大** |
| **T10** | 追加守护 #20–#26 | 1 文件增量 | 中 |
| **T11** | `qa.jsonl`（≥40 条）+ schema 守护 | 2 文件 | 中 |

**硬顺序**：T2 → T3 → T4 → T6（embedder 与 store 是 ingest 的两个输入）。
T5 与 T3/T4 **可并行**（`corpus.py` 不依赖它们，这是分层的收益）。T7 依赖 T4/T3。
T8 只依赖 `RetrievedChunk` 的形状，可与 T5/T6/T7 并行。T9 依赖 T3–T8。
T10 与 T11 可与 T9 并行。
**T5 / T6 / T9 是本阶段的主要工作量**，其余都是小块。

---

## 7. 验收

### 7.1 命令

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src/pyassist
uv run pytest                                   # 退出码 0，全程不触网

# 阶段五专属
uv run pytest tests/test_corpus_parse.py tests/test_ingest_idempotent.py \
              tests/test_retriever.py tests/test_citations.py tests/test_eval_dataset.py -v

# 真实语料（手动，需联网 + 下载模型；只跑一次）
uv run python -m pyassist.rag.ingest --fetch          # 抓语料 + 建索引
uv run python -m pyassist.rag.ingest                  # 再跑一次：deleted 应为 0
uv run python -c "import asyncio; from pyassist.rag.retriever import retrieve; \
                  print(asyncio.run(retrieve('asyncio.gather 异常处理')))"
```

### 7.2 判据

| # | 判据 | 由什么保证 |
|---|---|---|
| 1 | 同一夹具语料跑两次，chunk id 集合与 `count()` **完全一致** | `test_ingest_idempotent` |
| 2 | 删掉语料里一篇文档再跑，它的 chunk **从库中消失** | `test_ingest_idempotent`（收敛，不只是 upsert） |
| 3 | chunk id 由内容哈希 + URL 派生：改一个字 → id 变 | `test_ingest_idempotent` |
| 4 | 检索命中正确文档、`top_k` 生效、无命中返回 `[]`（不抛异常） | `test_retriever` |
| 5 | `retrieval.done` 带 `doc_ids` 与 `scores`（按位对齐） | `test_retriever`（structlog capture） |
| 6 | 集合存在但 model_id 不符 → `ConfigError`（**不是**静默垃圾排序） | `test_retriever` |
| 7 | 集合不存在或为空 → `CorpusMissing`（让 §3.2 的 503 成为**可达**路径） | `test_retriever` |
| 8 | 引用与 chunk **一一对应**，编号 = rank+1，URL 来自 chunk 且**带锚点** | `test_citations` |
| 9 | `find_ungrounded_markers` 能抓出答案里越界的 `[9]` | `test_citations` |
| 10 | HTML → 块解析正确；`<pre>` 不被劈开；`section`/`anchor` 映射正确；`¶` 被剥掉 | `test_corpus_parse` |
| 11 | 集合创建时 `space == "cosine"` 且 `embedding_function is None` | `test_retriever` + 代码审查 |
| 12 | `qa.jsonl` schema 齐全、`id` 唯一、≥40 条 | `test_eval_dataset` |
| 13 | 守护 #20–#26 全绿，且既有 #1–#19 **仍然全绿** | `tests/test_contract_guardrails.py` 全跑 |
| 14 | 测试完全离线（无下载、无 telemetry 外联、**未写 `data/chroma`**） | 全量 `pytest` + 人工核对 `data/chroma` 只有 `.gitkeep` |
| 15 | 未创建任何 router / service / 图 / 跑分器 | 人工核对 §1.2 清单 + `git status` |
| 16 | 真实语料 ingest 可重跑，且第二次 `deleted == 0` | 手动命令（§7.1 第二条） |
| 17 | `--paths` 单页重建**不删**其余文档的 chunk | `test_ingest_idempotent`（D9 安全阀） |
| 18 | `mypy --strict` 在 `rag/` 上通过，**没有 `type: ignore` 兜底** | `uv run mypy --strict src/pyassist` + 人工核对 |

---

## 8. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 首次 ingest 要下载 bge-m3（约 2.3GB）+ torch（CPU 轮子约 200MB），且要在 2 万 chunk 上跑嵌入 | 开发期用 `--paths` 单页重建迭代；全量 ingest 排在**非峰值时段**跑（`AGENTS.md` §1「价格与错峰」，虽然本地嵌入不烧 token，但机器占用是实打实的） |
| R2 | `download.pytorch.org` 在部分网络不可达 → `uv sync` 直接失败 | 删掉 `[tool.uv.sources]` 的 torch 钉，接受 PyPI 的 CUDA 轮子（约 2.5GB，无 GPU 也能跑）。这是 §10.2 的回退路径 |
| R3 | 归档 zip URL 记错 / 上游改版（§2.4，未能代验） | 两步解析（先读 `download.html` 抽链接，再回退常量）；施工第一步 `curl -I` 核实（T2） |
| R4 | 分块参数（1000 / 150）不合适 → Recall@5 达不到 0.80 | §10.5：参数是量级判断，**必须等阶段六跑分回填**。改 `CHUNK_*` 即改 `CHUNKER_VERSION` 并重建索引（D7 第 4 条只 warn 不抛，正是为了让这一步不阻塞） |
| R5 | bge-m3 在中文提问 × 英文语料上召回偏差 | §10.6：先不做 query 改写；若阶段六跑分确实偏差，再在图里加一步改写（**不在阶段五引入**） |
| R6 | Chroma 1.5.9 的 `configuration_json` 内部形状与假设不符（C12） | 防御式 `_find_string_value` 递归找 key，找不到就 `debug` 记一笔、以 metadata 为主证，**不硬编码形状** |
| R7 | `mypy --strict` 在无 stub 的 bs4/torch 上刷屏 | `[[tool.mypy.overrides]]` 放行（§5.12）；**但 `Any` 返回值必须在函数内落到显式注解的局部变量**（见 §5.2 的 `vectors` 陷阱），不允许用 `# type: ignore` 抹平判据 18 |

---

## 9. 与 ROADMAP 的差异（八条，均需知悉）

1. **新增 `rag/corpus.py`**（ROADMAP 未列）。理由见 §5.12 的 `AGENTS.md` 增补说明。
   ⚠️ 既是**新所有者**，就必须在 `AGENTS.md` §2 与阶段五产出表里**显式登记**它，
   否则它就是个未登记的并行件（这是 T2 的一部分，不是可选项）。
2. **新增 `tests/fakes/` 与 `tests/fixtures/`**（ROADMAP 只在阶段七提 `tests/fakes/sandbox.py`）。
   D10 要求离线测试有确定性替身与夹具语料。
3. **新增 `tests/test_corpus_parse.py` 与 `test_eval_dataset.py`**（ROADMAP 未列）。
   前者守护纯函数解析层，后者守护被 §4 冻结的评估集 schema。
4. **ROADMAP 说"chunk id 由内容哈希 + 来源 URL 派生"**；本规格去掉 ordinal 严格贴合该措辞（D5），
   并明确"位置无关"的语义。
5. **ROADMAP 未提"要改既有文件"**；本规格明确改 `config.py` / `pyproject.toml` / `.env.example` /
   `tests/conftest.py` / `tests/test_contract_guardrails.py` / `AGENTS.md`。
6. **ROADMAP 的两个阻塞项（向量模型选型未定 / 语料来源未定）本规格给出了可施工的默认值**
   （D1 + §2.4），并把两者都降级为**不阻塞动工**的待定项（§10.1 / §10.3）——
   因为 fake embedder 与夹具语料让全部测试完全不依赖它们。
7. **夹具语料用单根布局**（`tests/fixtures/corpus/{docs,peps}/`），不是两个平行夹具目录：
   这样夹具目录可以直接当 `--corpus` 参数用，测试路径与生产路径**走同一段代码**（§2.5）。
8. **`read_doc` 的取全文接口提前给了**（D6 的 `fetch_document`）——
   ROADMAP 把它算在阶段六，但接口在这里定型可以防阶段六回头改 `retriever.py`。

---

## 10. 待定项（每条给推荐值，均不阻塞动工）

1. **向量模型最终选型**：推荐 `BAAI/bge-m3`（2.27GB / 1024 维 / 多语）。
   轻量替代 `intfloat/multilingual-e5-small`（约 470MB / 384 维，需 `query:` / `passage:` 前缀 ——
   这正是 D2 把 Protocol 拆成 `embed_query` / `embed_documents` 两个方法的原因）。
   免 torch 替代：`fastembed`（ONNX，复用已装的 `onnxruntime`，但模型清单不如 ST 全）。
   换模型 = 改 `config.DEFAULT_EMBEDDING_MODEL_ID` 一行 + 删 `data/chroma` 重跑 ingest。
2. **torch 安装体积**：CPU 索引在部分网络下不可达。回退见 R2。
3. **语料范围**：推荐 `3.12` 全量（`tutorial/` + `library/` + `reference/`，约 12MB zip /
   约 2 万 chunk）。`--paths` 供开发期快速迭代。
4. **PEP 固定清单**：推荐 PEP 8 / 20 / 257 / 484 / 585 / 604 / 634 / 695 共 8 个，
   覆盖"风格 + 类型系统 + match 语法"三个与学习强相关的方向。清单写进 `config.PEP_NUMBERS` 一处。
5. **chunk 参数（1000 / 150）与 `min_score` 阈值**：现在是量级判断，
   **必须等阶段六跑分后回填**。`min_score` 默认 `0.0`（不过滤），
   避免在没跑分前瞎猜阈值把召回压没。
6. **中英混合 query**：多语模型能直接覆盖；若阶段六发现中文提问召回明显偏差，
   再考虑 query 改写（把中文问题翻成英文检索词）。**不在阶段五引入**。
7. **`evals/datasets/` 的路径歧义**：`AGENTS.md` §4 写 `evals/datasets/**`，
   ROADMAP 写 `src/pyassist/evals/datasets/qa.jsonl`。本规格按 ROADMAP（`config.QA_DATASET_PATH`），
   并把 §4 那行读作该目录。建议顺手在 `AGENTS.md` §4 把路径写全，避免歧义。
8. **`data/chroma` 的忽略**：`.gitignore` 的 `data/**` + `!data/**/.gitkeep` 已覆盖，确认无需改动。
9. **pytest marker**：本规格**不新增 marker**（所有测试离线，真实网络路径用手动命令验收）。
   若实现时确实要加联网测试，**必须同时在 `pyproject.toml` 注册**，否则 `--strict-markers` 直接报错。
10. **`collection.configuration_json` 的读取**：目前计划用递归找 key（C12）。
    若实现时发现该属性在当前版本上稳定为 `{"hnsw": {"space": ...}}`，
    可退化为直读并删掉递归辅助 —— **但 `space` 的校验本身不能删**（C7 靠它成立）。
11. **归档 zip 的 URL 已代验**（2026-10-13 施工时核实，本条原有的"未能代验"作废）：
    `curl -I https://docs.python.org/3.12/archives/python-3.12-docs-html.zip`
    → **HTTP/1.1 200 OK**，`Content-Length: 51717518`（约 49.3 MiB），`content-type: application/zip`。
    即 `config.DOCS_ARCHIVE_URL_FALLBACK` 的取值正确，R3 的"URL 记错"分支排除。
    两步解析（先读 `download.html` 抽链接、再回退常量）**保留不变** ——
    验的是**今天**对，兜的是上游改版，两者不冲突。
    ⚠️ 仍未代验的是 `download.html` 页面里那个链接的**路径写法**（本机 WebFetch 被策略拦截，
    只有 `curl` 可用），故 `resolve_docs_archive_url` 的抽链接逻辑由
    `test_corpus_parse.py::test_resolve_docs_archive_url_extracts_the_link` 用 `respx` 覆盖，
    实测留待首次 `--fetch`。
12. **新增 `store.index_exists()`**（2026-10-13 施工中发现，非原计划）：
    §5.5 承诺 `--dry-run` 是只读预演，但原实现直接 `open_collection()`，
    而它会构造 `PersistentClient` → **在 `data/chroma` 里建出 `chroma.sqlite3`**（C1）。
    于是"先看一眼要动多少数据"会在 §4 禁区留下产物，并把"索引没建"变成"索引建了个空的"。
    修法：新增只读盘的 `store.index_exists()`（`_dry_run` 先问它，再决定要不要开客户端）。
    这条**偏差是加了一个 §5.7 未列的函数**，遵循"Chroma 知识只住在 `store.py`"的单点所有权，
    对应测试 `test_ingest_idempotent.py::test_index_exists_never_creates_the_directory`
    与 `::test_dry_run_does_not_write_the_chroma_dir`。顺带确认：`data/chroma` 现只有 `.gitkeep`。
13. **嵌入期 OOM：分块上界 + token 闸门**（2026-10-13 施工中被真实崩溃逼出来）：
    首次 `python -m pyassist.rag.ingest` 崩在
    `transformers/modeling_attn_mask_utils.py` → `DefaultCPUAllocator: not enough memory:
    you tried to allocate 4899708032 bytes`。链路是三项各自的"合理"叠加：
    `parse_document` 把嵌套 `<li>` 展平成一个 47723 字符的块 → `_iter_pieces` 只对
    `kind == "code"` 做超长拆分（正文放行）→ 嵌入时 padding 到批内最长（2187 token）
    × batch 256 → 注意力掩码 O(batch × seq²) ≈ 4.9GB。
    三处各修一刀，缺一刀都不成立：
    - `rag/ingest.py`：新增 `_split_prose`，正文块超 `CHUNK_MAX_CHARS` 按词边界拆（D5 第 5 条）；
    - `rag/ingest.py`：`_UPSERT_BATCH_SIZE` 256 → 64，上界改由内存推出；
    - `rag/embedder.py`：显式设 `max_seq_length = EMBEDDING_MAX_SEQ_TOKENS`（1024），
      并在截断发生时打 `embed.truncated` —— 闸门放在**唯一向量化出口**上，
      调用方给什么批量都不该能把它撑爆。
    配套：`config.CHUNKER_VERSION` 1 → 2（切法变了，索引元数据要能分辨）；
    `EMBEDDING_MAX_SEQ_TOKENS = 1024` 的依据是全量实测（修分块后 p50=254 / p90=336 /
    **max=860** token，512 会截 48 条、1024 一条不截）。
    回归测试 `tests/test_ingest_chunk_size.py` / `tests/test_embedder.py`；
    实测脚本 `experiments/verify_embedder_memory.py`（换模型后重跑，更新数字）。
    **索引必须重建**：`rm -rf data/chroma` 后重跑 ingest（§4「重建 = 删目录」）。
14. **CPU 嵌入的墙钟时间：长度分桶批处理（2026-10-13 已做，选了 ①）**
    —— 修完 OOM 后实测（本机，bge-m3 全量 17366 chunk）：
    - 真实 token 总量 **4.08M**，实测吞吐 **0.4~0.7 chunk/s**（约 250~350 token/s）
      → 全量 ingest 预计 **3~5 小时**（CPU 推理，与本次改动无关，改批次也不变）；
    - batch 是**填充**到批内最长：文档顺序 batch=64 时模型实际要算 **7.13M** token（1.75×）。
      按 **token** 长度排序后进批 → **4.12M（1.01×）**，即省掉 **42% 的算力**。
      （顺带确认：`_UPSERT_BATCH_SIZE` 256 → 64 **不是**变慢的原因 ——
      batch=128 时填充率是 1.88×，越大越费。）
    - 候选（未选）：① `upsert_chunks` 内按长度排序后分批 ——
      正确性无风险（`chunk_id` 是内容哈希，`ordinal` 由 `build_chunks` 定，与写入顺序无关），
      但要动 §5.5 固定顺序里的写库步骤，且排序会让日志里的写入顺序不再等于文档顺序；
      ② ONNX / 更小模型（`multilingual-e5-small`，384 维）；③ 只在开发期用全量子集。
    **倾向 ①**：42% 是实测，改动局限在一个函数里，且不改任何契约。

    **落地（2026-10-13）**：`ingest._length_bucketed(chunks, batch_size, token_lengths)`，
    按 **token 数**排序后 `batched`。三个决定：

    - **排序键必须是 token 数，不是 `len(embed_text)`。** 这里必须记一笔，因为
      **第一版就是按字符数写的，而且它错得很隐蔽**：上线时文档里写着"省 42%"，
      实测只有 **20.1%**。字符序与 token 序在这份语料上不是同一个序 ——
      中文正文一个字符约一个 token，而代码块里成串的缩进空格与三引号要摊到好几个 token。
      差的那 22 个百分点是**每次重建索引 1.3 小时**。
      长度改由 `embedder.count_tokens`（`Embedder` 协议的新成员）给出：数 token 需要
      tokenizer，而 tokenizer 只在 `rag/embedder.py` 里（§2 单点所有权），
      所以这个能力长在向量化出口上，测试替身也被迫实现它 —— 于是离线测试与生产
      **走同一条判据**。全量扫一遍 **1.8 秒**（实测），相对 6 小时可忽略。
      **教训**：文档里的百分比必须由**已上线的那个函数**量出来。当时的 bench 量的是
      一份"按长度排序"的独立脚本，与 `_length_bucketed` 是两份代码，于是两者可以
      悄悄分叉，而分叉的地方恰好就是那 22 个百分点。现在 bench 直接调生产函数。

      **实测（`experiments/bench_embed_speed.py`，全量 17366 chunk，batch=64）**：

      | 策略 | 真实 token | padding 后 | 比率 | 省 |
      |---|---|---|---|---|
      | 文档顺序 | 4,082,001 | 7,127,044 | 1.75× | — |
      | **token 分桶（现行为）** | 4,082,001 | **4,115,980** | **1.01×** | **42.2%** |
      | 字符分桶（已废弃） | 4,082,001 | 5,691,408 | 1.39× | 20.1% |

      跑这个脚本前记得 `HF_HUB_OFFLINE=1`：模型已缓存在本地，但 `sentence-transformers`
      启动时会去 `huggingface.co` 问一次版本，网络不通时**先干等几分钟再崩**。
    - **降序，不是升序。** ETA 是拿"已完成批次的平均速度"外推的（`_log_progress`）。
      先跑最短的批次会让 6 小时任务的 ETA 从"还剩 2 小时"一路坏到"还剩 5 小时"，
      而人看到 ETA 变坏的第一反应是 kill 掉它。倒序让 ETA 从偏悲观**单调收敛**到真值。
      两种方向的**分桶结果完全相同**（同一组批次、顺序相反），填充收益一分不少。
    - **重排是安全的**，理由要摆出来而不是假定：`chunk_id` 是内容 sha256、`ordinal` 由
      `build_chunks` 定、`upsert` 幂等（C8）—— 三者都与写入先后无关。唯一可见的变化是
      **日志里的写入顺序不再等于文档顺序**。

    回归网 `tests/test_ingest_batching.py`：分桶确实省了（对照一份**文档顺序的参照实现**，
    否则"省了"只是说法）、**按 token 而不是按字符**（构造一对两种口径结论相反的文本）、
    批次由长到短、以及**正序倒序落库内容逐字节相同**（证明省下的算力不是拿正确性换的）。
    另有一条：chunk 数与长度数对不上时**必须抛**，不能靠 `zip` 静默截断 ——
    截断会丢 chunk，而那正是第 15 条事故的形状。

    注意这次改动**发生在 2026-10-13 那次 5.96 小时重跑之后** —— 重跑用的是旧的分批顺序。
    下次重建才会吃到这 42%。
15. **索引耐久性：HNSW 不是持久层，WAL 才是（2026-10-13 已修，**索引必须重建**）**
    —— 全量 ingest 跑完 4.8 小时、打印 `ingest.done`、退出码 0，但重开报
    `chromadb.errors.InternalError: Error executing plan: Error sending backfill request to
    compactor: Error constructing hnsw segment reader: Error creating hnsw segment reader:
    Error loading hnsw index`。

    **落盘现场**（这是全部分析的依据）：

    | 证据 | 值 | 含义 |
    |---|---|---|
    | 段目录内容 | 只有 `index_metadata.pickle`（`total_elements_added: 16384`，1,376,448 B） | HNSW 的 `data_level0.bin` / `header.bin` / `length.bin` / `link_lists.bin` **一个都没有** |
    | `embeddings`（元数据段） | 17366 行 | 文档与 metadata 完好 |
    | `embeddings_queue`（WAL） | 983 行，seq 16385..17366 | **前 16384 条已被清空** |
    | 向量段 `max_seq_id` | 16384 | compactor 自认已消费到这里 |
    | stderr | 无 panic、无 error | 是**静默**失败，不是崩溃 |

    即：最后一次全量索引重写写了元数据、没写完数据文件，进程就退出了，
    而 WAL 已经被清 —— **前 16384 条向量从此不存在于任何地方**。
    Chroma 的 `sync_threshold`（默认 1000）就是这个"重写整个索引"的触发阈值。

    **修法三件，缺一不可**：

    1. `config.HNSW_SYNC_THRESHOLD = 1_000_000` + `HNSW_BATCH_SIZE = 100`，
       `store.get_or_create_collection` 显式写进 `configuration=`。
       写入期间不再有任何全量重写 → **失败模式从"数据没了"变成"索引是空的"**。
       实测（`experiments/repro_index_durability.py`）：`sync_threshold=1e6` 时硬退出，
       WAL 仍持有全部 20000 条**带向量的**记录，重开可 `count()` 并完整检索
       （要 1000 条回来 1000 条）。
    2. `store.close_clients()`：CLI 原来跑完就 `sys.exit()`，compactor 被连人带活掐断。
       现在显式 `close()` 等它把待落盘的活干完（实测这一步会把整个索引真落盘，且**不清 WAL**）。
    3. `ingest.verify_index()`：**关掉→重开→多要结果**，不过就返回 **2**。
       `count()` 单独用会说谎：把损坏的段目录删掉之后 `count()` 报 17366（元数据段的条数），
       检索却只找得到 **983** 条 —— 残库被验收盖章"通过"比不验收更坏。
       "多要结果"那一步是 `rag/retriever.py::probe_index_depth`（不写在 ingest 里，
       是因为 §2 把读向量库整体划给了 retriever，守护
       `test_chroma_query_only_in_rag_retriever` 就是这条边界的看门人）。

    **恢复路径**（下次真出事时按这个来）：段文件坏了但 WAL 完好的话，
    **删掉 `data/chroma` 下那个 UUID 段目录**（保留 `chroma.sqlite3`）即可 ——
    读路径会从 WAL 现场重建，不必重新嵌入。本次不适用（WAL 已被清空），
    所以 **2026-10-13 这次必须重跑 ingest（约 4.8 小时）**，并用
    `HF_HUB_OFFLINE=1 uv run python -m pyassist.rag.ingest`（模型已缓存在
    `~/.cache/huggingface`，不加这个变量会卡在 huggingface.co 的连接超时上）。

    回归网：`tests/test_store.py`（阈值、关闭后仍可读、旧阈值要被点名、损坏段 → `CorpusMissing`）、
    `tests/test_ingest_verify.py`（验收判据、容差、退出码、关掉再重开的调用序列）、
    `tests/test_retriever.py`（探针本身）。

    ---

    **2026-10-13 重跑结果（已完结）**：删掉 `data/chroma` 后重跑，540 篇 / 17366 chunk，
    用时 **5.96 小时**，退出码 0，日志 `ingest.done` + `ingest.verified`。
    独立进程复验通过；真实查询可用（`walrus operator` → `whatsnew/3.8.html` 排第一，
    0.594；模型冷启动 40s，热查询约 0.25s）。

    这次重跑还暴露出**两个必须记下来的事实**：

    1. **验收判据原本是错的（已修）**。`readable == requested` 拿近似结构做精确断言：
       五个独立进程读同一个健康库，短缺是 0、0、1、0、0。也就是说那道闸门本身有约 20%
       的误报率 —— 它这次"通过"是运气。改成 1% 容差（`IndexProbe.is_readable`），
       事故的 5.7% 可读仍然会被抓住。**教训是一般性的：验收闸门的误报率必须自己被测过。**
    2. **`sync_threshold=1e6` 的代价是索引永不落盘**。实测最终状态：段目录**空的**
       （一个 `.bin` 都没有），WAL 持有全部 17366 条带向量的记录，`chroma.sqlite3`
       **207MB**（旧配置下是 117MB）。索引只在读路径上于内存里重建 ——
       冷开 2.5s + 全库探针 1.2s，进程内只付一次。
       **这是刻意换来的**：磁盘上不再存在"写了一半的索引"，事故从"数据永久丢失"
       降级为"内存里重建"。若哪天冷启动成为瓶颈，再谈是否调低阈值 ——
       注意改配置对已有集合无效（C9），调它必须重建。

    **恢复路径（就绪版）**：段文件坏了但 WAL 完好时，删掉 `data/chroma` 下那个 UUID
    段目录（保留 `chroma.sqlite3`）即可，读路径会从 WAL 现场重建，**不必重新嵌入**。

---

## 11. 本阶段为阶段六预留的接口（不写空壳，只列形状）

阶段六**不需要**回头改本阶段的任何文件。它要做的只是"用"：

| 阶段六要用的 | 本阶段给它的 |
|---|---|
| `search_docs` 工具 | `await retriever.retrieve(query, top_k=...)` → `list[RetrievedChunk]` |
| `read_doc` 工具 | `await retriever.fetch_document(url)`（入参可带锚点，内部会剥，D6） |
| 引用与 prompt 编号 | `citations.build_citations(chunks)` 的 `index`（**禁止自行编号**，D8） |
| 引用准确率核验 | `Citation.chunk_id` / `doc_url` / `snippet`（D8） |
| Recall@5 跑分 | `RetrievedChunk.doc_url` 与 `qa.jsonl` 的 `relevant_urls` **同类可直接比**（D11） |
| "文档里没有"分支 | `retrieve()` 返回 `[]` → 图节点抛 `RetrievalEmpty`（§3.2 映射为 200） |
| 索引没建 / 建错 | `CorpusMissing` → 503、`ConfigError` → 500，**映射已在 `api/errors.py` 就位**（§2.1） |
| 语料更新 | `python -m pyassist.rag.ingest --fetch`（§5.5 CLI） |

**明确不在本阶段做的**：`api/routers/knowledge.py`、`services/qa.py`、`agent/graphs/qa.py`、
`agent/tools/docs.py`、`evals/run_rag_eval.py`、`agent/registry.py` 的 `@tool` 定义（§1.2）。
