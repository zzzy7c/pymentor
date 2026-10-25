"""★ 唯一检索入口（§2 单点所有权）。`collection.query()` 只在本文件出现（守护 #24）。

**为什么整个"嵌入 + 查询"要一起丢进线程**（SPEC D6）：
Chroma 客户端是**同步阻塞**的，`collection.query` 一样会堵事件循环 —— 只把
`embed_query` 包进 `to_thread` 是漏的（§3.4「异步优先」）。所以边界划在
`retrieve()` 这一层：`async def` 里**只做一次** `await asyncio.to_thread(_search, ...)`。

**模型加载绝不发生在事件循环里**：下载 + torch 初始化是秒级到分钟级的重阻塞。
它落在 `to_thread` 内的首次 `embed_*` 里完成，或由 `embedder.warm_up()` 预热。

本模块**还**持有索引深度的探针（`probe_index_depth`）。它读的不是"某个问题命中什么"，
而是"这个索引到底读得出多少条" —— 归属这里是因为 §2 把**读向量库**这件事
整体划给了本模块：让 `rag/ingest.py` 自己 `collection.query()` 会开出第二个读入口，
`tests/test_contract_guardrails.py::test_chroma_query_only_in_rag_retriever` 正是为此存在。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from math import ceil
from typing import Any, cast

from chromadb.api.models.Collection import Collection

from pyassist.config import DEFAULT_TOP_K, RETRIEVAL_MIN_SCORE, settings
from pyassist.errors import StoreError
from pyassist.logging import get_logger
from pyassist.rag import embedder as embedder_module
from pyassist.rag import store as store_module
from pyassist.rag.embedder import Embedder

logger = get_logger(__name__)

# 整页读取的上限：一页 Python 文档最多几百个 chunk，取 1000 条是"够用且不会无限"。
_DOCUMENT_FETCH_LIMIT = 1000

# 深度探针一次最多要多少条。**多要结果是判据本身，不是保险丝。**
#
# `count()` 会说谎：2026-10-13 实测，删掉损坏的段目录之后 count() 仍报 17366
# （那是元数据段的条数），而检索只找得到 983 条 —— 差的 16383 条向量已经永远回不来了。
# 只 count() 的验收会对着一个残库盖章"通过"，比不验收更坏。
_PROBE_MAX_RESULTS = 50_000

# 允许"要了没给全"的比例（SPEC §10 第 15 条）。
#
# **不是手抖，是 HNSW 的性质**：它是**近似**最近邻。`n_results == count` 是一次退化查询 ——
# 向一个近似结构索要精确的全集保证。实测（真实库，17366 条，五个独立进程）：
# 短 0、0、1、0、0 条。要求严格相等，等于让验收以约 20% 的概率误报。
#
# 而事故当天的形态是短 **16383** 条（17366 → 983，只有 5.7% 可读），
# 比 1% 的容差高出两个数量级。一个偶尔误报的闸门会被无视 ——
# 那正是 `_warn_oversize` 已经为同一件事吃过一次的亏。
_PROBE_TOLERANCE_RATIO = 0.01


@dataclass(frozen=True)
class RetrievedChunk:
    """检索结果的一条。形状是阶段五对阶段六的**契约**（SPEC §11 / D6）。

    `doc_url` 与 `source_url` 都要留（正是为了让 Recall@5 的比对不必解析字符串，
    见 SPEC D4/D11）：前者对齐 `qa.jsonl` 的 `relevant_urls`，
    后者用于引用与 grounding 核验。
    """

    chunk_id: str
    text: str
    doc_url: str
    source_url: str
    title: str
    section: str | None
    score: float


@dataclass(frozen=True)
class IndexProbe:
    """索引深度探针的结果（SPEC §10 第 15 条）。三个数字缺一不可：

    - `declared`：`count()` 说的条数 —— **元数据段**的条数，可能说谎；
    - `requested`：实际向索引索要的条数（`min(declared, _PROBE_MAX_RESULTS)`）；
    - `readable`：索引真正还回来的条数。

    `declared != requested` 只可能在超过上限时发生。判"索引可读"用 `is_readable`，
    **不要**直接比 `readable == requested` —— 见 `_PROBE_TOLERANCE_RATIO`。
    """

    declared: int
    requested: int
    readable: int

    @property
    def shortfall(self) -> int:
        """差了多少条。负数按 0 算（多给不该发生，但也不该让判据崩）。"""
        return max(0, self.requested - self.readable)

    @property
    def allowed_shortfall(self) -> int:
        """容差内允许差多少条。至少 1 —— 小库上按比例算会得到 0，又会退回严格相等。"""
        return max(1, ceil(self.requested * _PROBE_TOLERANCE_RATIO))

    @property
    def is_readable(self) -> bool:
        """索引读得回来。**这是验收判据**，别在外面重写一遍比较。"""
        return self.shortfall <= self.allowed_shortfall


def probe_index_depth(
    collection: Collection, *, max_results: int = _PROBE_MAX_RESULTS
) -> IndexProbe:
    """把整个索引**完整读一遍**，返回声明条数与真正读得回的条数。

    **同步阻塞**，与 `_search` 同一条理由（SPEC D6）：它是给 CLI 的 ingest 验收用的，
    不在事件循环里跑；要放进 async 上下文必须先 `asyncio.to_thread(...)`。

    为什么不能只 `count()`：2026-10-13 那次，段目录里只剩一个 `index_metadata.pickle`，
    `count()` 照样报 17366，而检索只找得到 983 —— 那 16383 条向量已经不在任何地方了。
    **`count()` 读的是元数据段，`query()` 才真正走 HNSW**，两者撕裂正是事故的形态。
    """
    declared = collection.count()
    requested = min(declared, max_results)
    if requested == 0:
        return IndexProbe(declared=declared, requested=0, readable=0)
    probe = cast("list[Sequence[float]]", [_first_embedding(collection)])
    result = collection.query(query_embeddings=probe, n_results=requested)
    return IndexProbe(
        declared=declared,
        requested=requested,
        readable=len(_flatten(result.get("ids"))),
    )


def _first_embedding(collection: Collection) -> list[float]:
    """从库里**取一条真实向量**当探针。

    不自己造一个全 1 / 全 0 的常量：全 0 在 cosine 下是未定义距离，全 1 与谁都不像，
    两者都可能让"索引其实是空的"表现成"查得到但没意义"。
    取真向量还顺带证明了向量本身读得回来，且维度天然对齐 —— 换模型（1024 → 384）不用改这里。

    `_flatten` 在这里**不能**用：它在"第一层是列表"时会取出 `values[0]` 本身，
    而 `get(include=["embeddings"])` 的第一层就是一条向量 —— 用它会把向量拆成标量。
    """
    got = collection.get(limit=1, include=["embeddings"])
    embeddings = got.get("embeddings")
    # `is None or len(...) == 0` 而不是 `not embeddings`：真库返回的是 numpy 数组，
    # 对它取真值会抛 `ValueError: truth value of an array ... is ambiguous`。
    if embeddings is None or len(embeddings) == 0:
        raise StoreError("从库里读不回任何向量", details={"count": collection.count()})
    return [float(x) for x in embeddings[0]]


async def retrieve(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = RETRIEVAL_MIN_SCORE,
    collection: Collection | None = None,
    embedder: Embedder | None = None,
) -> list[RetrievedChunk]:
    """检索。**无命中返回 `[]`，不抛异常**（SPEC D6）。

    这条边界是刻意的：`RetrievalEmpty` 由阶段六的图节点在 `not chunks` 时抛 ——
    这样"文档里没有"能走图里的一条**正常分支**，而不是靠异常做控制流
    （§3.2 也把它定为 200）。于是两个 API 的职责唯一、无歧义。

    真正该抛的是"引擎坏了"：语料没建（`CorpusMissing`）、索引漂移（`ConfigError`）。
    它们都不是"这次查询没结果"，而是"这个检索器现在不可信"。
    """
    return await asyncio.to_thread(
        _search,
        query,
        top_k=top_k,
        min_score=min_score,
        collection=collection,
        embedder=embedder,
    )


async def assert_index_ready(
    *,
    collection: Collection | None = None,
    embedder: Embedder | None = None,
) -> None:
    """**开流前的廉价预检**（SPEC D6）：开集合 → 非空 → 索引元数据与当前配置兼容。

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
    """`assert_index_ready` 的同步核心。经 `to_thread` 调用，不得在事件循环里直调。"""
    target = collection or store_module.open_collection()
    if collection is not None:
        store_module.assert_not_empty(target)
    expected_model = embedder.model_id if embedder is not None else settings.embedding_model_id
    store_module.assert_index_compatible(target, model_id=expected_model)


async def fetch_document(
    doc_url: str, *, collection: Collection | None = None
) -> list[RetrievedChunk]:
    """读整页，按 `ordinal` 升序（阶段六 `read_doc` 工具用）。入参可带锚点，内部剥掉。

    **本阶段不定型这个接口的话，阶段六就得回头改 `retriever.py`**（SPEC §9.8）——
    那正是"引擎先于壳"想避免的事。
    """
    return await asyncio.to_thread(_fetch_document, doc_url, collection=collection)


def _search(
    query: str,
    *,
    top_k: int,
    min_score: float,
    collection: Collection | None,
    embedder: Embedder | None,
) -> list[RetrievedChunk]:
    """同步检索核心。经 `to_thread` 调用，**不得**在事件循环里直接调用。

    解析顺序**固定**，且这个顺序本身是可测的契约（SPEC D6 / 判据 6、7）：

        1. collection := 入参 or store.open_collection()      # 缺失/空 → CorpusMissing（廉价）
           ↑ 注入的入参也补一次 store.assert_not_empty()：读路径的空检查只在
             `open_collection()` 里，而注入句柄恰恰是"被清库后仍被持有"的那种
        2. expected   := embedder.model_id if ... else settings.embedding_model_id
        3. store.assert_index_compatible(collection, model_id=expected)
           # ↑ 漂移 → ConfigError（廉价）
        4. embedder   := 入参 or get_embedder(settings.embedding_model_id)  # 此刻才加载模型
        5. 嵌入 + 查询

    第 2/3 步**故意排在加载模型之前**：`settings.embedding_model_id` 只是一个字符串，
    没必要先把 2GB 的模型拉起来才发现索引是别的模型建的。注入 embedder 时用它的
    `model_id` 做期望值，语义是「这个集合必须是用**我实际要用的这个 embedder**建的」——
    于是测试无需打桩 `settings`（SPEC D6）。
    """
    target = collection or store_module.open_collection()
    # 注入的 collection 也走一次空检查：`open_collection()` 只在读路径生效，
    # 而长期句柄被清库后是最容易静默返回 `[]` 的地方（判据 7）。
    if collection is not None:
        store_module.assert_not_empty(collection)
    expected_model = embedder.model_id if embedder is not None else settings.embedding_model_id
    store_module.assert_index_compatible(target, model_id=expected_model)

    active = embedder or embedder_module.get_embedder(settings.embedding_model_id)

    started = time.perf_counter()
    # `cast` 而不是复制：`list[float]` 本来就**是** `Sequence[float]`，
    # 只是 list 的不变性让 mypy 不肯自动放行 `list[list[float]]` →
    # Chroma `query_embeddings=` 联合里的 `list[Sequence[float]]`。视图变化，不是数据变化。
    query_embeddings = cast("list[Sequence[float]]", [active.embed_query(query)])
    result = target.query(
        query_embeddings=query_embeddings,  # 守护 #24 的签名指纹
        n_results=max(1, top_k),
        include=["documents", "metadatas", "distances"],
    )
    chunks = [
        chunk
        for chunk in (
            _to_chunk(chunk_id, text, metadata, distance)
            for chunk_id, text, metadata, distance in _iter_rows(result)
        )
        if chunk.score >= min_score
    ]
    chunks.sort(key=lambda chunk: chunk.score, reverse=True)
    latency_ms = int((time.perf_counter() - started) * 1000)

    logger.info(
        "retrieval.done",
        doc_ids=[chunk.chunk_id for chunk in chunks],
        # scores 与 doc_ids **严格按位对齐**（§3.5 冻结的字段名与语义）
        scores=[round(chunk.score, 6) for chunk in chunks],
        latency_ms=latency_ms,
        top_k=top_k,
        model_id=active.model_id,
    )
    return chunks


def _fetch_document(doc_url: str, *, collection: Collection | None) -> list[RetrievedChunk]:
    """取整页并按 `ordinal` 升序返回。

    先剥锚点：阶段六的 `read_doc` 收到的可能是引用里那个带 `#fragment` 的 URL，
    而库里按 `doc_url`（无锚点）归组（SPEC D4）。
    """
    target = collection or store_module.open_collection()
    document_url = doc_url.split("#", 1)[0]
    result = target.get(
        where={"doc_url": document_url},
        limit=_DOCUMENT_FETCH_LIMIT,
        include=["documents", "metadatas"],
    )

    rows: list[tuple[int, RetrievedChunk]] = [
        (_metadata_int(metadata, "ordinal"), _to_chunk(chunk_id, text, metadata, None))
        for chunk_id, text, metadata, _ in _iter_rows(result)
    ]

    # `ordinal` 是文档内的原始位置 —— 整页读取要按它还原文档顺序，
    # 而不是按 Chroma 的返回顺序（那是实现细节，不该被引用）。
    rows.sort(key=lambda item: item[0])
    return [chunk for _, chunk in rows]


def _iter_rows(
    result: Mapping[str, object],
) -> Iterator[tuple[str, str, dict[str, Any], float | None]]:
    """把 Chroma 的返回值拉平成 `(chunk_id, text, metadata, distance)` 四元组。

    两处 Chroma 的脾气在这里吸收掉（SPEC C6）：
    - `query` 返回的是**两层嵌套列表**（`[0]` 取第一组），`get` 返回**一层**；
    - 内外层都可能是 `None`，也可能含 `None` 元素。

    用"第一层是列表且其元素是列表"来判定嵌套，而不是靠调用方传标志位 ——
    调用方少一个必填参数，就少一处能传错的地方。
    """
    ids = _flatten(result.get("ids"))
    documents = _flatten(result.get("documents"))
    metadatas = _flatten(result.get("metadatas"))
    distances = _flatten(result.get("distances"))

    for position, chunk_id in enumerate(ids):
        metadata = _at(metadatas, position)
        text = _at(documents, position)
        distance = _at(distances, position)
        yield (
            str(chunk_id),
            text if isinstance(text, str) else "",
            metadata if isinstance(metadata, dict) else {},
            float(distance) if isinstance(distance, int | float) else None,
        )


def _to_chunk(
    chunk_id: str, text: str, metadata: dict[str, Any], distance: float | None
) -> RetrievedChunk:
    """一行 → `RetrievedChunk`。`distance` 为 `None`（整页读取路径）时 score 记 0.0。"""
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=text,
        doc_url=_metadata_str(metadata, "doc_url"),
        source_url=_metadata_str(metadata, "source_url"),
        title=_metadata_str(metadata, "title"),
        # 写库时 None 落成了 ""（C11 不允许 None），读回时还原成 None。
        section=_metadata_str(metadata, "section") or None,
        # cosine 距离 = 1 - 余弦相似度，范围 [0,2]，**越小越相似**（C7）。
        # 仅在度量确实是 cosine 时成立 —— `assert_index_compatible` 已保证这一点；
        # 少了那一步校验，这一行就会**静默**变成垃圾（这正是 D7 存在的理由）。
        score=1.0 - distance if distance is not None else 0.0,
    )


def _flatten(values: Any) -> list[Any]:
    """把 Chroma 那种"可能嵌套一层、可能含 None"的列表拉平。"""
    if not values:
        return []
    first = values[0]
    if isinstance(first, list):
        return list(first)
    return list(values)


def _at(values: list[Any], position: int) -> Any:
    return values[position] if position < len(values) else None


def _metadata_str(metadata: dict[str, Any], key: str) -> str:
    """`Metadata` 的值类型是 `str | int | float | bool`，取值一律窄化成 `str`。

    缺字段给 `""` 而不是抛：一条 metadata 不完整不该把整次检索打挂 ——
    那会让"某一页少了个字段"升级成"整个问答不可用"（SPEC §5.6）。
    """
    value = metadata.get(key)
    return value if isinstance(value, str) else ""


def _metadata_int(metadata: dict[str, Any], key: str) -> int:
    """同上的整数版本。取不到给 0，排到最前 —— 位置信息缺失不该让整页读取失败。"""
    value = metadata.get(key)
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉，免得 True 变成 1
        return 0
    return value if isinstance(value, int) else 0
