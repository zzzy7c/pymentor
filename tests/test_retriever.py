"""检索层守护（SPEC §5.10 / 判据 6、7、12、13）。

这里的断言对象是**解析顺序**，不只是排序结果。D6 那一串「先开集合、再校验模型、
最后才加载模型」的顺序有两个肉眼可见的后果，两个都必须被钉住：

- 语料没建 → `CorpusMissing`（廉价失败，不碰 embedding）
- 模型漂移 → `ConfigError`（廉价失败，不加载 2GB 权重）

顺序一旦被"优化"成先加载模型，错误码不会变，只是每次失败都要多等十几秒 ——
那种退化没有任何测试会发现，除非像这样把错误类型当契约来断言。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from chromadb.api.models.Collection import Collection
from structlog.testing import capture_logs

from fakes.embedder import HashingEmbedder
from pyassist.config import COLLECTION_NAME, DISTANCE_SPACE
from pyassist.errors import ConfigError, CorpusMissing, StoreError
from pyassist.rag import store as store_module
from pyassist.rag.retriever import fetch_document, probe_index_depth, retrieve

JSON_DOC_URL = "https://docs.python.org/3.12/library/json.html"
ASYNCIO_DOC_URL = "https://docs.python.org/3.12/library/asyncio-task.html"

# 逐字取自夹具 json.html 的段落 —— 哈希嵌入是词袋模型，逐字重合才是有意义的查询。
JSON_QUERY = "Serialize an object to a JSON formatted string with the dumps function"


def _metadata(hashing_embedder: HashingEmbedder) -> dict[str, str | int | float | bool]:
    return store_module.build_index_metadata(
        model_id=hashing_embedder.model_id, dimension=hashing_embedder.dimension
    )


async def test_matching_page_ranks_first(collection: Collection) -> None:
    """检索得到**正确的那一页**，且分数是 `1 - distance`（只在 cosine 下成立，C7）。"""
    results = await retrieve(JSON_QUERY, collection=collection, embedder=HashingEmbedder())

    assert results
    assert results[0].doc_url == JSON_DOC_URL
    assert results[0].score > 0.5, "逐字重合的查询应当拿到高分，否则嵌入的余弦语义没接上"
    assert results[0].text.strip()


async def test_results_are_sorted_by_score_descending(collection: Collection) -> None:
    """`sort` 是显式的，不依赖 Chroma 的返回顺序 —— 引用编号 = 排名 + 1，排序错等于引用错。"""
    results = await retrieve(JSON_QUERY, collection=collection, embedder=HashingEmbedder())

    scores = [chunk.score for chunk in results]
    assert scores == sorted(scores, reverse=True)


async def test_top_k_caps_the_result_count(collection: Collection) -> None:
    results = await retrieve(JSON_QUERY, top_k=2, collection=collection, embedder=HashingEmbedder())

    assert len(results) == 2


async def test_min_score_above_everything_returns_empty_not_an_error(
    collection: Collection,
) -> None:
    """D6：无命中是 `[]`，**不是** `RetrievalEmpty`。

    `RetrievalEmpty` 由阶段六的图节点在 `not chunks` 时抛，这样"文档里没有"
    能走图里一条正常分支。两层都抛的话，这个语义就被重复实现了两遍。
    """
    results = await retrieve(
        JSON_QUERY, min_score=2.0, collection=collection, embedder=HashingEmbedder()
    )

    assert results == []


async def test_empty_collection_raises_corpus_missing(
    chroma_path: Path, hashing_embedder: HashingEmbedder
) -> None:
    """建的集合但一条都没写 = 语料没建（`CorpusMissing` → 503，而不是"答了个空"）。"""
    empty = store_module.get_or_create_collection(
        path=chroma_path, metadata=_metadata(hashing_embedder)
    )
    assert empty.count() == 0

    with pytest.raises(CorpusMissing):
        await retrieve(JSON_QUERY, collection=empty, embedder=hashing_embedder)


async def test_model_drift_raises_config_error(collection: Collection) -> None:
    """换模型忘了重建索引 → `ConfigError`。

    这是全项目最难靠肉眼发现的故障：不报错，只是排序悄悄变成垃圾。
    注入的 embedder 的 `model_id` 就是期望值（D6），所以这里不需要打桩 `settings`。
    """
    drifted = HashingEmbedder(model_id="some-other-model")

    with pytest.raises(ConfigError):
        await retrieve(JSON_QUERY, collection=collection, embedder=drifted)


async def test_non_cosine_space_raises_config_error(
    chroma_path: Path, hashing_embedder: HashingEmbedder
) -> None:
    """度量不是 cosine 时必须抛 —— 因为 `score = 1 - distance` 只在 cosine 下有意义。

    Chroma 的**默认**度量是 l2（C5），所以这条不是假想：少写一个 `configuration=`
    参数就落在这里，而 l2 距离减 1 得到的"分数"仍然是个浮点数，排序照样出结果。
    """
    client = store_module.get_client(chroma_path)
    l2_collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=None,
        configuration={"hnsw": {"space": "l2"}},
        metadata=_metadata(hashing_embedder) | {"space": "l2"},
    )
    # 必须放一条进去：空集合会先撞上 `CorpusMissing`，而这里的被测对象是 space 校验。
    l2_collection.upsert(
        ids=["l2-row"],
        embeddings=[[1.0] + [0.0] * (hashing_embedder.dimension - 1)],
        documents=["placeholder"],
        metadatas=[
            {
                "doc_url": JSON_DOC_URL,
                "source_url": JSON_DOC_URL,
                "title": "json",
                "section": "",
                "ordinal": 0,
            }
        ],
    )

    with pytest.raises(ConfigError):
        await retrieve(JSON_QUERY, collection=l2_collection, embedder=hashing_embedder)


async def test_l2_configuration_is_caught_even_when_metadata_claims_cosine(
    chroma_path: Path, hashing_embedder: HashingEmbedder
) -> None:
    """**佐证**那一半必须真的在工作（SPEC D7 第 2 条 / 判据 11）。

    `space` 是我们自己写进 `collection.metadata` 的，所以"元数据说 cosine"是一句
    **自述**，它证明不了实际配置。真正的证据是 `configuration_json` 里的 hnsw space。

    这个夹具模拟的正是最阴的那种事故：元数据写着 cosine，而 `get_or_create_collection`
    的 `configuration=` 参数被人删掉了 —— 于是索引实际是 l2。只看元数据会全部放行。
    """
    client = store_module.get_client(chroma_path)
    mismatched = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=None,
        configuration={"hnsw": {"space": "l2"}},  # 实际是 l2
        # 却声称 cosine（`_metadata` 造的 space 就是 DISTANCE_SPACE）
        metadata=_metadata(hashing_embedder),
    )
    assert mismatched.metadata is not None
    assert mismatched.metadata["space"] == DISTANCE_SPACE, "夹具前提：元数据声称 cosine"
    mismatched.upsert(
        ids=["row"],
        embeddings=[[1.0] + [0.0] * (hashing_embedder.dimension - 1)],
        documents=["placeholder"],
        metadatas=[{"doc_url": JSON_DOC_URL}],
    )

    with pytest.raises(ConfigError):
        await retrieve(JSON_QUERY, collection=mismatched, embedder=hashing_embedder)


def test_fresh_collection_is_cosine_and_cannot_embed_on_its_own(
    chroma_path: Path, hashing_embedder: HashingEmbedder
) -> None:
    """判据 11：新建集合的 `space == "cosine"`，且 `embedding_function is None`。

    "没有自带嵌入函数"这件事**不靠读私有属性**来断言 —— Chroma 1.5.9 上没有
    `collection.embedding_function` 这个属性，而且它随时可能加回来。改为观察行为：
    显式传 `None` 之后，让 Chroma 自己算向量的调用会直接抛 `ValueError`（C4）。

    这是判据 11 唯一可靠的正面证据：属性可以不存在也可以改名，
    但"调 `query_texts=` 会炸"证明的是**这条路径确实被掐死了**。
    """
    collection = store_module.get_or_create_collection(
        path=chroma_path, metadata=_metadata(hashing_embedder)
    )

    assert collection.metadata is not None
    assert collection.metadata["space"] == DISTANCE_SPACE
    # 元数据对上之后，生产的那道校验也必须放行（否则就是校验本身写错了）。
    store_module.assert_index_compatible(collection, model_id=hashing_embedder.model_id)

    collection.upsert(
        ids=["row"],
        embeddings=[[1.0] + [0.0] * (hashing_embedder.dimension - 1)],
        documents=["placeholder"],
        # ⚠️ 空 metadata 会被 Chroma 直接拒（`got 0 metadata attributes in upsert`），
        # 所以这里的 metadata 不能省 —— 那是个与嵌入无关的独立约束。
        metadatas=[{"doc_url": JSON_DOC_URL}],
    )

    with pytest.raises(ValueError, match="embedding function"):
        collection.query(query_texts=["hello"], n_results=1)


async def test_retrieval_done_log_is_position_aligned(collection: Collection) -> None:
    """§3.5 冻结的字段名与语义：`doc_ids` 与 `scores` **严格按位对齐**。

    错位不会让任何东西崩溃 —— 它只是让成本/质量报表里的分数挂到别的文档上。
    """
    with capture_logs() as logs:
        results = await retrieve(JSON_QUERY, collection=collection, embedder=HashingEmbedder())

    entry = next(log for log in logs if log["event"] == "retrieval.done")

    assert entry["doc_ids"] == [chunk.chunk_id for chunk in results]
    assert entry["scores"] == [round(chunk.score, 6) for chunk in results]
    assert entry["model_id"] == HashingEmbedder().model_id
    assert isinstance(entry["latency_ms"], int)


async def test_fetch_document_strips_the_fragment_and_restores_ordinal_order(
    collection: Collection,
) -> None:
    """整页读取（阶段六 `read_doc` 用）。

    入参带锚点（引用 URL 就长那样），库里按无锚点的 `doc_url` 归组（D4）——
    不剥锚点会得到空结果，而且不报错。
    """
    chunks = await fetch_document(f"{ASYNCIO_DOC_URL}#asyncio.gather", collection=collection)

    assert chunks
    assert all(chunk.doc_url == ASYNCIO_DOC_URL for chunk in chunks)
    assert all("#" not in chunk.doc_url for chunk in chunks)
    # 整页读取路径没有 distance，score 记 0.0（不是 1.0 —— 0.0 才是"未知"）。
    assert all(chunk.score == 0.0 for chunk in chunks)

    # 顺序按 `ordinal` 还原成原文顺序：首段的句子必须排在末段的句子之前。
    joined = "\n".join(chunk.text for chunk in chunks)
    assert joined.index("outlines the high-level asyncio APIs") < joined.index(
        "support cancellation and introspection"
    )


async def test_fetch_document_returns_only_that_page(collection: Collection) -> None:
    """一次只读一页：`where` 过滤失效会让 `read_doc` 悄悄把整库塞进上下文。"""
    chunks = await fetch_document(JSON_DOC_URL, collection=collection)

    assert chunks
    assert {chunk.doc_url for chunk in chunks} == {JSON_DOC_URL}


async def test_fetch_document_on_unknown_url_returns_empty(collection: Collection) -> None:
    """未知页面返回 `[]`：这是"这页不在语料里"，由调用方决定怎么表达，不是引擎故障。"""
    chunks = await fetch_document(
        "https://docs.python.org/3.12/library/nope.html", collection=collection
    )

    assert chunks == []


async def test_the_embedding_runs_off_the_event_loop(collection: Collection) -> None:
    """整个嵌入 + 查询必须包在 `to_thread` 里（D2/D6）。

    `Embedder` 协议是同步的（真身在 CPU 上算 bge-m3）。谁在事件循环里直接调它，
    一个慢查询就拖垮所有连接 —— §2 流式约定里那条"不许同步 `graph.stream()`"
    是同一件事的另一面。

    判据用**线程身份**而不是计时：计时类的异步断言在忙的 CI 上必然 flaky，
    而"嵌入跑在哪个线程"是确定性的。
    """
    threads: list[threading.Thread] = []

    class RecordingEmbedder(HashingEmbedder):
        def embed_query(self, text: str) -> list[float]:
            threads.append(threading.current_thread())
            return super().embed_query(text)

        def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
            threads.append(threading.current_thread())
            return super().embed_documents(texts)

    await retrieve(JSON_QUERY, collection=collection, embedder=RecordingEmbedder())

    assert threads, "嵌入根本没被调用 —— 测试自身失效了"
    assert all(thread is not threading.main_thread() for thread in threads), (
        "嵌入跑在主线程上：检索在阻塞事件循环"
    )


def test_client_cache_is_keyed_by_path(tmp_path: Path, chroma_path: Path) -> None:
    """同一个路径复用同一个 client，不同路径各自独立。

    这同时是 §4 禁区的第一道防线：测试全程显式传 `tmp_path` 下的路径，
    任何"忘了传 path 于是 chromadb 用了默认目录"的改动都会在这里变成
    两个路径拿到同一个 client，从而被断言抓住。
    """
    first = store_module.get_client(chroma_path)
    again = store_module.get_client(chroma_path)
    other = store_module.get_client(tmp_path / "chroma-elsewhere")

    assert first is again
    assert first is not other


# ---------- 索引深度探针：ingest 的验收判据（SPEC §10 第 15 条）----------


def test_probe_index_depth_reads_the_whole_index_back(collection: Collection) -> None:
    """索引完好时三个数字应当一致：声明多少、要多少、读回来多少。

    这条同时钉住判据本身**没有过严**：正常库上 `readable == requested`，
    所以 ingest 的验收不会在健康库上误报（一个永远失败的验收等于没有验收）。
    """
    probe = probe_index_depth(collection)

    assert probe.declared > 0
    assert probe.requested == probe.declared
    assert probe.readable == probe.requested


def test_probe_index_depth_does_not_query_an_empty_collection(chroma_path: Path) -> None:
    """空集合：三个 0，且**不去查**。

    `n_results=0` 在 Chroma 上是非法参数 —— 若探针不先短路，空库会从
    "语料没建好"（`CorpusMissing`）退化成一句上游的参数异常。
    """
    collection = store_module.get_or_create_collection(
        path=chroma_path,
        metadata=store_module.build_index_metadata(model_id="test-hashing-64", dimension=64),
    )

    probe = probe_index_depth(collection)

    assert (probe.declared, probe.requested, probe.readable) == (0, 0, 0)


def test_probe_index_depth_reports_a_capped_request(chroma_path: Path) -> None:
    """超过上限时 `declared` 仍然说实话，`requested` 才是被截断的那个。

    这个区分非有不可：验收比的是 `readable == requested` —— 若把两者混成一个字段，
    大库（> 上限）上就会永远报"少了 N 条"，而它其实一条没少。
    """
    collection = store_module.get_or_create_collection(
        path=chroma_path,
        metadata=store_module.build_index_metadata(model_id="test-hashing-64", dimension=64),
    )
    embedder = HashingEmbedder()
    collection.add(
        ids=["c0", "c1", "c2"],
        documents=["a", "b", "c"],
        embeddings=embedder.embed_documents(["a", "b", "c"]),
        metadatas=[{"doc_url": "u"}] * 3,
    )

    probe = probe_index_depth(collection, max_results=2)

    assert probe.declared == 3
    assert probe.requested == 2
    assert probe.readable == 2


def test_probe_index_depth_raises_when_no_vector_can_be_read() -> None:
    """连探针向量都取不回来 → `StoreError`，而不是拿个常量向量去查。

    常量（全 0）在 cosine 下是未定义距离，会让"索引是空的"表现成
    "查得到但没意义" —— 那正是验收看不出问题的那条老路。

    只能替身：真库里 `count() > 0` 就一定有向量读得回来，这个状态造不出来。
    替身顺带断言**没有走到查询** —— 没探针就该当场失败，而不是拿个空向量去问。
    """

    class _VectorlessCollection:
        def count(self) -> int:
            return 5

        def get(self, **_: Any) -> dict[str, Any]:
            return {"embeddings": []}

        def query(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("没有探针向量时不该发起查询")

    with pytest.raises(StoreError, match="读不回任何向量"):
        probe_index_depth(cast("Collection", _VectorlessCollection()))
