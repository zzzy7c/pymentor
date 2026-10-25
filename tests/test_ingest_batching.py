"""分批策略：**填充**是嵌入期最大的浪费（SPEC §10 第 14 条）。

嵌入器把一批 padding 到批内最长的那条，于是注意力掩码的长度由**最长的那条**决定，
而不是由这一批真正要算的内容决定。真实语料的形态恰好最不利于这一点：
绝大多数是几百字符的正文，少数是几千字符的代码块 —— 文档顺序分批时，
每批里的那块长代码都会把同批其余七条一起撑大。

实测全量 4.08M 真实 token 被算成 7.13M（1.75×，文档顺序）。
按**字符**分桶是 5.69M（1.39×，省 20.1%）——这版 2026-10-13 当天就被推翻了。
按 **token** 分桶是 4.12M（1.01×，省 42.2%），是现在这一版。

这里钉四件事：**分桶确实省了**、**按 token 而不是字符**、**是降序**（ETA 的收敛方向）、
**重排不改变写进库的内容**（否则省下的算力是用正确性换的）。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from itertools import batched
from pathlib import Path

import pytest
from chromadb.api.models.Collection import Collection

from fakes.embedder import HashingEmbedder
from pyassist.rag import store as store_module
from pyassist.rag.ingest import Chunk, _length_bucketed, upsert_chunks

_BATCH_SIZE = 8

# 替身的 `_TOKEN_RE` 是 `[a-z0-9_]+`，所以 `"x" * 3000` 只是**一个** token。
# 语料因此不能拿重复字符造 —— 那会让所有长度都等于 1，排序退化成恒等变换，
# 而测试照样绿。下面用词表拼出真正的长度差。
_WORDS = ("alpha", "beta", "gamma", "delta")


def _text(tokens: int) -> str:
    """恰好 `tokens` 个 token 的文本（替身按 `_TOKEN_RE` 切，所以词数与 token 数相同）。"""
    return " ".join(_WORDS[i % len(_WORDS)] for i in range(tokens))


def _chunk(index: int, tokens: int) -> Chunk:
    return _chunk_from_text(index, _text(tokens))


def _chunk_from_text(index: int, text: str) -> Chunk:
    return Chunk(
        chunk_id=f"c{index:05d}",
        body=text,
        embed_text=text,
        doc_url="https://example.invalid/page.html",
        source_url=f"https://example.invalid/page.html#s{index}",
        title="Title",
        section="Section",
        ordinal=index,
    )


def _corpus() -> list[Chunk]:
    """真实语料的形态：大量短正文 + 每 20 条一个长代码块，且长块**散布**在开头到结尾。"""
    return [_chunk(i, 600 if i % 20 == 0 else 20) for i in range(200)]


def _lengths(chunks: Sequence[Chunk], embedder: HashingEmbedder) -> list[int]:
    """走 `Embedder` 协议拿长度 —— 与 `upsert_chunks` 内部**同一个来源**。

    测试里自己写一遍 `len(chunk.embed_text)` 就测不到生产的口径了，
    而"口径错"正是 20.1% 那一版翻车的原因。
    """
    return embedder.count_tokens([chunk.embed_text for chunk in chunks])


def _batches(
    chunks: Sequence[Chunk], embedder: HashingEmbedder, *, bucketed: bool
) -> Iterator[tuple[Chunk, ...]]:
    if not bucketed:
        return batched(chunks, _BATCH_SIZE)
    return _length_bucketed(chunks, _BATCH_SIZE, _lengths(chunks, embedder))


def _padded_tokens(batches: Iterable[Sequence[Chunk]], embedder: HashingEmbedder) -> int:
    """填充开销：每批按**批内最长**那条乘批大小 —— 这正是注意力掩码的长度。

    用 token 数而不是字符数，因为被优化的量就是 token 数（见模块 docstring）。
    替身数 token 不需要加载 2GB 模型，所以这一条仍然是离线可跑的（§5.1）。
    """
    return sum(
        len(batch) * max(embedder.count_tokens([chunk.embed_text for chunk in batch]))
        for batch in batches
    )


def test_bucketing_cuts_the_padding_cost(hashing_embedder: HashingEmbedder) -> None:
    """按 token 长度分桶后，填充开销必须**大幅**低于文档顺序。

    断言"低于"而不是钉死比例：这一条守的是**机制还在**（同批长度相近），
    不是某个具体百分点。真实语料上的 42.2% 是 SPEC §10 第 14 条的实测，
    这里这个小语料只复现机制。
    """
    chunks = _corpus()

    bucketed = _padded_tokens(_batches(chunks, hashing_embedder, bucketed=True), hashing_embedder)
    doc_order = _padded_tokens(_batches(chunks, hashing_embedder, bucketed=False), hashing_embedder)

    assert bucketed < doc_order * 0.5, f"分桶没起作用：{bucketed} vs 文档顺序 {doc_order}"
    # 上界不收在 1.0×：**批次的边界处必然还剩一次填充**。这份语料 10 条长块、
    # 批大小 8，于是最后 2 条长块和 6 条短正文凑成一整批 —— 那 6 条被撑到 600。
    # 实测 1.36×（13280 / 9800）：residue 全部来自这一个跨界批次，不是散落的。
    # 真实语料上这个效应被 17366 条摊薄到 1.01×（SPEC §10 第 14 条）。
    real = sum(_lengths(chunks, hashing_embedder))
    assert bucketed < real * 1.5, f"填充没收干净：{bucketed} vs 真实 {real}"


def test_bucketing_is_by_token_not_by_character(hashing_embedder: HashingEmbedder) -> None:
    """按 token 数排序，**不是**按 `len(embed_text)`。

    这一条直接钉住 2026-10-13 那次翻车：字符序与 token 序**不是同一个序**，
    差的那 22 个百分点（20.1% → 42.2%）就是每次重建索引 1.3 小时。

    构造一对**两种口径结论相反**的文本，否则这条测试证明不了任何事：
    - `few_wide`：两个七十字母的"词" → 141 字符，**2** token；
    - `many_narrow`：十二个四字母词 → 59 字符，**12** token。

    按字符排，`few_wide` 在前；按 token 排，`many_narrow` 在前。断言后者。
    """
    few_wide = _chunk_from_text(0, " ".join(["a" * 70, "b" * 70]))
    many_narrow = _chunk_from_text(1, _text(12))

    assert len(few_wide.embed_text) > len(many_narrow.embed_text), "构造没成立：字符数没拉开"
    lengths = _lengths([few_wide, many_narrow], hashing_embedder)
    assert lengths == [2, 12], f"token 口径不对：{lengths}"

    batches = list(_length_bucketed([few_wide, many_narrow], 2, lengths))
    assert [chunk.chunk_id for chunk in batches[0]] == ["c00001", "c00000"], (
        f"没按 token 降序（按字符排就会得到相反的次序）：{batches}"
    )


def test_length_bucketed_refuses_mismatched_lengths(hashing_embedder: HashingEmbedder) -> None:
    """长度数量对不上时必须炸，**不能靠 `zip` 静默截断**。

    截断会丢 chunk：索引里少几条，而 ingest 照样报成功 —— 那正是 2026-10-13 那次
    "CLI 报成功、向量丢了 16384 条"的同一个形状。
    """
    chunks = _corpus()
    with pytest.raises(ValueError, match="数量不一致"):
        list(_length_bucketed(chunks, _BATCH_SIZE, _lengths(chunks, hashing_embedder)[:-1]))


def test_batches_are_ordered_longest_first(hashing_embedder: HashingEmbedder) -> None:
    """批次必须**由长到短**。

    这不是风格问题：`_log_progress` 拿"已完成批次的平均速度"外推 ETA。
    先跑短的会让 6 小时任务的 ETA 从"还剩 2 小时"一路坏到"还剩 5 小时"，
    而人看到 ETA 变坏的第一反应是 kill 掉它。倒序让 ETA 单调收敛。
    """
    chunks = _corpus()
    lengths = _lengths(chunks, hashing_embedder)
    batch_lengths = [
        max(_lengths(list(batch), hashing_embedder))
        for batch in _length_bucketed(chunks, _BATCH_SIZE, lengths)
    ]

    assert batch_lengths == sorted(batch_lengths, reverse=True), (
        f"批次没有从长到短：{batch_lengths}"
    )


def test_upsert_writes_the_same_content_in_either_order(
    tmp_path: Path, hashing_embedder: HashingEmbedder
) -> None:
    """重排**不改变写进库的内容** —— 这是"省算力"没有拿正确性去换的证据。

    两个库，同一批 chunk，一个正序一个倒序。`chunk_id` 是内容哈希、
    `ordinal` 由 `build_chunks` 定、`upsert` 是幂等替换（C8），
    所以两边落库的 (id → 正文, metadata) 必须**逐字节相同**。
    """
    chunks = _corpus()
    metadata = store_module.build_index_metadata(
        model_id=hashing_embedder.model_id, dimension=hashing_embedder.dimension
    )

    written: list[dict[str, tuple[str, str]]] = []
    for name, items in (("forward", chunks), ("reversed", list(reversed(chunks)))):
        collection = store_module.get_or_create_collection(path=tmp_path / name, metadata=metadata)
        assert upsert_chunks(collection, items, hashing_embedder, batch_size=_BATCH_SIZE) == len(
            items
        )
        written.append(_snapshot(collection))

    assert written[0] == written[1]
    assert len(written[0]) == len(chunks)


def _snapshot(collection: Collection) -> dict[str, tuple[str, str]]:
    """`chunk_id` → (正文, `ordinal`) —— 足以抓住任何顺序相关的写入差异。"""
    result = collection.get(include=["documents", "metadatas"])
    documents = result["documents"] or []
    metadatas = result["metadatas"] or []
    return {
        str(chunk_id): (str(document), str(metadata.get("ordinal")))
        for chunk_id, document, metadata in zip(result["ids"], documents, metadatas, strict=True)
    }
