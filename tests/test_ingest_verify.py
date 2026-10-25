"""ingest 的**验收关**：写完必须"关掉、重开、真读一遍"（SPEC §10 第 15 条）。

2026-10-13 的事故里最坏的部分不是索引坏了，而是**没有人知道它坏了**：
CLI 打印 `ingest.done`、返回 0，4.8 小时的活其实白干。
所以这一关的判据不是"写入没报错"，而是"库能被一个**全新客户端完整读出来**"。

这里同时钉住三个容易退化的点：

1. `count()` **不能**当验收 —— 实测删掉损坏段目录后它仍报 17366，而检索只找得到 983 条；
2. 验收必须发生在**写入之后**（不是启动时跑一次就算）；
3. 验不过就**返回非 0**，让 CI / `docker compose` / 人都有信号可接。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from chromadb.api.models.Collection import Collection

from fakes.embedder import HashingEmbedder
from pyassist.errors import CorpusMissing, StoreError
from pyassist.rag import ingest as ingest_module
from pyassist.rag import store as store_module
from pyassist.rag.ingest import verify_index


class _StubCollection:
    """一个"读得回来、但内容不全"的集合替身。

    只有 `count()` / `get()` / `query()` 三个方法 —— 因为 `verify_index` 只该用这三个。
    真去构造一个"索引残了的 Chroma 库"是做不到的：那种状态恰恰是"数据已经丢了"，
    构造它等于把事故复现一遍。所以这里只替身**判据的那一半**。
    """

    def __init__(self, *, count: int, hits: int, embeddings: list[list[float]] | None = None):
        self._count = count
        self._hits = hits
        self._embeddings = embeddings if embeddings is not None else [[1.0, 0.0]]

    def count(self) -> int:
        return self._count

    def get(self, **_: Any) -> dict[str, Any]:
        return {"embeddings": self._embeddings}

    def query(self, **_: Any) -> dict[str, Any]:
        return {"ids": [[f"c{i}" for i in range(self._hits)]]}


def test_verify_index_passes_on_a_freshly_written_index(
    chroma_path: Path, collection: Collection
) -> None:
    """正常路径：写完的库能被重开的客户端读全。

    `expected` 必须在 `verify_index` **之前**取 —— 它会把客户端关掉，
    之后旧句柄就失效了（那正是它的用途）。
    """
    expected = collection.count()
    assert expected > 0

    verified = verify_index(expected_chunks=expected, path=chroma_path)

    assert verified == expected


def test_verify_index_rejects_a_short_index(
    chroma_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """要得到、给不全 → 报错。**这条就是事故当天缺的那一关。**

    它守的是"元数据说有 17366 条、向量索引里只有 983 条"：`count()` 完全正常，
    只有"多要结果"才暴露得出来（实测要 1000 条只回来 983 条）。

    与容差的关系：短 16383 条，而容差只给 1%（173 条）——
    加了容差之后那天**仍然会被抓住**，这正是容差可以存在的前提。
    """
    stub = _StubCollection(count=17366, hits=983)
    monkeypatch.setattr(store_module, "open_collection", lambda **_: stub)

    with pytest.raises(StoreError) as excinfo:
        verify_index(expected_chunks=17366, path=chroma_path)

    assert excinfo.value.details == {"count": 17366, "requested": 17366, "hits": 983}


@pytest.mark.parametrize(
    ("count", "hits"),
    [
        (17366, 17366),  # 全给
        (17366, 17365),  # 实测真实库上出现过的那种抖动（5 个进程：短 0、0、1、0、0）
        (17366, 17366 - 173),  # 正好用满 1% 容差
        (7, 7),  # 小库：容差至少 1，不能因为 1% 算成 0 就退回严格相等
        (7, 6),
    ],
)
def test_verify_index_tolerates_hnsw_approximation_below_the_boundary(
    chroma_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, hits: int
) -> None:
    """容差内的短缺**不许**报警 —— 这条才是"事故事实"与"闸门可信"的分界线。

    判据曾是 `readable == requested`，而 HNSW 是**近似**结构：向它索要 `count` 条
    （等于要全集）实测会随机少一两条。严格相等 → 约 20% 概率误报 → 闸门被无视，
    重演 2026-10-13 之前那种"没有人知道它坏了"的局面。
    """
    stub = _StubCollection(count=count, hits=hits)
    monkeypatch.setattr(store_module, "open_collection", lambda **_: stub)

    assert verify_index(expected_chunks=count, path=chroma_path) == count


def test_verify_index_rejects_a_count_mismatch(chroma_path: Path, collection: Collection) -> None:
    """库里条数与语料算出来的对不上 → 报错（写入没落全，也没落对）。"""
    actual = collection.count()

    with pytest.raises(StoreError) as excinfo:
        verify_index(expected_chunks=actual + 1, path=chroma_path)

    assert excinfo.value.details == {"count": actual, "expected": actual + 1}


def test_verify_index_rejects_an_index_without_vectors(
    chroma_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连一条向量都读不回来 → 报错，而不是拿一个常量向量去查。

    查询向量是从库里取的**真实向量**：常量（全 1 / 全 0）会让"索引其实是空的"
    表现成"查得到但没意义"，那就又回到"验收看不出问题"的老路上了。
    """
    stub = _StubCollection(count=10, hits=10, embeddings=[])
    monkeypatch.setattr(store_module, "open_collection", lambda **_: stub)

    with pytest.raises(StoreError, match="读不回任何向量"):
        verify_index(expected_chunks=10, path=chroma_path)


# ---------- CLI 的接线：验收必须真的发生在写入之后 ----------


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, chroma_path: Path) -> list[str]:
    """把 CLI 跑到夹具语料 + 假嵌入上，并记下验收那几步的调用顺序。"""
    calls: list[str] = []
    monkeypatch.setattr(store_module, "CHROMA_DIR", chroma_path)
    monkeypatch.setattr(ingest_module, "get_embedder", lambda _model_id: HashingEmbedder())

    real_close = store_module.close_clients
    real_open = store_module.open_collection

    def close() -> None:
        calls.append("close")
        real_close()

    def open_(**kwargs: Any) -> Collection:
        calls.append("open")
        return real_open(**kwargs)

    monkeypatch.setattr(store_module, "close_clients", close)
    monkeypatch.setattr(store_module, "open_collection", open_)
    return calls


def test_cli_closes_and_reopens_after_writing(
    monkeypatch: pytest.MonkeyPatch, chroma_path: Path, fixture_corpus_dir: Path
) -> None:
    """`--corpus` 跑一遍：退出码 0，且**关掉再重开**这两步确实发生过。

    只在启动时 `open_collection` 一次也能让"退出码 0"成立 —— 所以这里断言的是
    调用序列里同时有 `close` 和 `open`，而不是只看返回码。
    """
    calls = _patch_pipeline(monkeypatch, chroma_path)

    exit_code = ingest_module.main(["--corpus", str(fixture_corpus_dir)])

    assert exit_code == 0
    assert calls == ["close", "open"], f"验收没有在写入之后跑完整：{calls}"


def test_cli_returns_nonzero_when_the_index_cannot_be_read_back(
    monkeypatch: pytest.MonkeyPatch, chroma_path: Path, fixture_corpus_dir: Path
) -> None:
    """读不回来就返回 **2**（不是 0，也不是 1）。

    与"ingest 本身失败"（1）区分开很重要：两者要做的下一步完全不同 ——
    1 去看语料/模型，2 去删那个损坏的段目录（向量还在 WAL 里，不必重新嵌入）。
    """
    monkeypatch.setattr(store_module, "CHROMA_DIR", chroma_path)
    monkeypatch.setattr(ingest_module, "get_embedder", lambda _model_id: HashingEmbedder())

    def explode(**_: Any) -> Collection:
        raise CorpusMissing("索引读不出来（测试注入）")

    monkeypatch.setattr(store_module, "open_collection", explode)

    exit_code = ingest_module.main(["--corpus", str(fixture_corpus_dir)])

    assert exit_code == 2
