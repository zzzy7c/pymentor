"""向量库连接的**耐久性**约定（SPEC §10 第 15 条）。

这一整个文件都是 2026-10-13 那次事故的回归网。那天的事实是：

- 全量 ingest 跑了 4.8 小时，打印 `ingest.done`，退出码 0；
- 重开集合却报 `Error loading hnsw index`；
- 段目录里只有 `index_metadata.pickle`（`total_elements_added: 16384`），HNSW 的 `*.bin` 全无；
- WAL 已被清到 16384 之前 —— **前 16384 条向量只存在于那个已退出进程的内存里**。

根因是 Chroma 默认的 `sync_threshold=1000`：写入期间每 1000 条就把整个索引重写一遍，
重写失败/被打断一次，WAL 又已被清空，就只能重新嵌入。

所以这里钉住三件事：**阈值必须是抬高的那个值**、**关闭后数据仍在**、**旧阈值要被点名**。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import chromadb
import pytest
from chromadb.api.models.Collection import Collection
from structlog.testing import capture_logs

from fakes.embedder import HashingEmbedder
from pyassist.config import HNSW_BATCH_SIZE, HNSW_SYNC_THRESHOLD
from pyassist.errors import CorpusMissing
from pyassist.rag import store as store_module

_LEGACY_THRESHOLD = 1000  # Chroma 的默认值，也就是事故当时的配置


def _make_collection(path: Path, *, sync_threshold: int | None = None) -> Collection:
    """建一个集合。`sync_threshold=None` 走生产写路径，否则手工造一个"旧索引"。"""
    embedder = HashingEmbedder()
    metadata = store_module.build_index_metadata(
        model_id=embedder.model_id, dimension=embedder.dimension
    )
    if sync_threshold is None:
        return store_module.get_or_create_collection(path=path, metadata=metadata)

    # 直连客户端是为了**只**改这一个旋钮：`get_or_create_collection` 不接受注入配置，
    # 而这里要复现的正是"库里那个集合是用别的阈值建的"。
    hnsw = {"space": "cosine", "sync_threshold": sync_threshold, "batch_size": HNSW_BATCH_SIZE}
    return store_module.get_client(path).get_or_create_collection(
        name=store_module.COLLECTION_NAME,
        embedding_function=None,
        configuration={"hnsw": hnsw},
        metadata=metadata,
    )


def test_write_collection_carries_the_durability_threshold(chroma_path: Path) -> None:
    """写路径建的集合必须带 `HNSW_SYNC_THRESHOLD`，不能退回 Chroma 的默认 1000。

    这条守的是"有人重构时顺手把 `configuration=` 去掉"：那不会报任何错，
    只是从此每次 ingest 又回到"每 1000 条重写一次全量索引"的高危模式 ——
    正是 2026-10-13 那种"跑 4.8 小时然后索引读不出来"的复发条件。
    """
    collection = _make_collection(chroma_path)

    indexed = store_module._find_int_value(collection.configuration_json, "sync_threshold")

    assert indexed == HNSW_SYNC_THRESHOLD
    assert indexed != _LEGACY_THRESHOLD, "阈值没被抬起来"
    # 度量仍必须是 cosine（C5/C7）：顺手改配置时最容易碰掉的就是它。
    assert store_module._find_string_value(collection.configuration_json, "space") == "cosine"


def test_close_clients_leaves_a_readable_index_behind(chroma_path: Path) -> None:
    """`close_clients()` 之后数据仍在 —— 而且拿得回一个新客户端。

    这是事故修复点的正面断言：`close()` 是 compactor 落盘的机会，
    但"关掉"本身不能把数据关没了。旧句柄失效，新句柄必须读得出同样的东西。
    """
    embedder = HashingEmbedder()
    collection = _make_collection(chroma_path)
    collection.add(
        ids=["c0", "c1"],
        documents=["body 0", "body 1"],
        embeddings=embedder.embed_documents(["body 0", "body 1"]),
        metadatas=[{"doc_url": "u"}, {"doc_url": "u"}],
    )
    before = collection.count()

    store_module.close_clients()
    reopened = store_module.open_collection(path=chroma_path)

    assert reopened is not collection
    assert reopened.count() == before


def test_corrupt_index_segment_surfaces_as_corpus_missing() -> None:
    """索引段损坏（`Error loading hnsw index`）→ `CorpusMissing`，不是裸 `InternalError`。

    这是 2026-10-13 那个真实事故库的**实际行为**：`metadata` 与 `configuration_json`
    读得出来（走 sqlite 目录表），`count()` 却抛 `InternalError`。而 `InternalError`
    是 `ChromaError` 的子类，不接住就会冒到路由层，§3.2 的错误码枚举里没有它 ——
    只能落成 `INTERNAL_ERROR`，把"重建索引"误导成"排查代码"。

    `test_error_mapping` 管的是"异常 → HTTP"，这里只管"Chroma 异常 → 业务异常"这一段。
    """
    collection = cast("Collection", _UndreadableCollection())

    with pytest.raises(CorpusMissing) as excinfo:
        store_module.assert_not_empty(collection)

    assert excinfo.value.details == {"collection": store_module.COLLECTION_NAME}


class _UndreadableCollection:
    """`count()` 抛 `InternalError` 的替身 —— 真库造不出这个状态（构造它就等于复现事故）。"""

    def count(self) -> int:
        raise chromadb.errors.InternalError(
            "Error executing plan: Error sending backfill request to compactor: "
            "Error constructing hnsw segment reader: Error loading hnsw index"
        )


def test_stale_sync_threshold_is_warned_about(chroma_path: Path) -> None:
    """用旧阈值建的库要**被点名**，且带上两个数字。

    不抛是因为库本身读得没问题，重建却要几小时 —— 该由人挑时间付这笔钱。
    但必须出声：新配置对已有集合不生效（Chroma 忽略重复 `get_or_create` 的 configuration，
    C9），于是"我改了 config 就安全了"是个错觉。
    """
    collection = _make_collection(chroma_path, sync_threshold=_LEGACY_THRESHOLD)

    with capture_logs() as logs:
        store_module.assert_index_compatible(collection, model_id="test-hashing-64")

    entries = [entry for entry in logs if entry["event"] == "rag.index.sync_threshold_stale"]
    assert len(entries) == 1
    assert entries[0]["indexed_sync_threshold"] == _LEGACY_THRESHOLD
    assert entries[0]["current_sync_threshold"] == HNSW_SYNC_THRESHOLD


def test_matching_sync_threshold_does_not_warn(chroma_path: Path) -> None:
    """反面同样要钉住：阈值一致时**一条都不许响**。

    一个每次都响的警告等于没有警告 —— 真出事那天正好会被淹掉
    （`_warn_oversize` 的注释里已经为同一件事吃过一次亏）。
    """
    collection = _make_collection(chroma_path)

    with capture_logs() as logs:
        store_module.assert_index_compatible(collection, model_id="test-hashing-64")

    assert [entry for entry in logs if entry["event"] == "rag.index.sync_threshold_stale"] == []


def test_client_cache_reset_does_not_close(chroma_path: Path) -> None:
    """`reset_client_cache()` 只丢弃缓存，**旧句柄还能用**。

    这条把两个函数的语义差钉住：`close_clients()` 是"收尾"，`reset_client_cache()`
    是"测试间隔离"。若有人把后者改成前者，会有一堆"对象忽然不能用了"的怪故障。
    """
    assert store_module.get_client(chroma_path) is store_module.get_client(chroma_path)

    held = store_module.get_client(chroma_path)
    store_module.reset_client_cache()

    assert store_module.get_client(chroma_path) is not held
    assert held.heartbeat() > 0, "被丢弃的客户端不该失效"


@pytest.mark.parametrize("threshold", [_LEGACY_THRESHOLD, HNSW_SYNC_THRESHOLD])
def test_configured_threshold_is_what_ends_up_on_disk(chroma_path: Path, threshold: int) -> None:
    """参数化地把"配置 → 落库"这条路走两遍，防止只测了其中一支。"""
    collection = _make_collection(chroma_path, sync_threshold=threshold)
    indexed = store_module._find_int_value(collection.configuration_json, "sync_threshold")

    assert indexed == threshold
