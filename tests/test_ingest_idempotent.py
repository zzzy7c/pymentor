"""ingest 的幂等性与收敛语义（SPEC D9 / 判据 11、17）。

这是阶段五最容易"看起来对、实际悄悄坏掉"的一块：
盲目 upsert 也能让 re-run 的 `count()` 不变，但它**掩盖了残留**——
改了内容、删了页面之后，旧 chunk 会永久留在库里继续被检索到。
所以每一个测试都同时断言"新增的进来了"和"过期的出去了"。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from chromadb.api.models.Collection import Collection

# pytest 默认的 prepend 导入模式把 `tests/` 放进 sys.path，所以 `fakes` 是顶层包名。
from fakes.embedder import HashingEmbedder
from pyassist.errors import CorpusMissing
from pyassist.rag import store as store_module
from pyassist.rag.ingest import IngestReport, ingest_corpus, main

JSON_RELATIVE = "docs/library/json.html"

# 夹具里的一句原文。改它一个字符，验证 chunk id 跟内容绑定（不是跟序号绑定）。
ORIGINAL_SENTENCE = (
    "This module implements a subset of the JavaScript Object Notation specification."
)


def _mutable_corpus(tmp_path: Path, fixture_corpus_dir: Path) -> Path:
    """夹具语料的**可写副本**。

    绝不直接在 `tests/fixtures/` 上改 —— 那里是版本受控的输入，
    被一个测试改坏的夹具会让另一个测试莫名其妙地挂掉。
    """
    target = tmp_path / "corpus"
    shutil.copytree(fixture_corpus_dir, target)
    return target


def _ids(collection: Collection) -> set[str]:
    result = collection.get(include=["metadatas"])
    return {str(item) for item in (result["ids"] or [])}


def _doc_urls(collection: Collection) -> set[str]:
    result = collection.get(include=["metadatas"])
    metadatas = result["metadatas"] or []
    return {str(meta["doc_url"]) for meta in metadatas}


def _ingest(corpus: Path, collection: Collection, embedder: HashingEmbedder) -> IngestReport:
    return ingest_corpus(corpus, collection=collection, embedder=embedder)


def test_second_run_is_a_no_op(
    tmp_path: Path,
    fixture_corpus_dir: Path,
    collection: Collection,
    hashing_embedder: HashingEmbedder,
) -> None:
    """连跑两次：id 集合与 `count()` 逐字节一致，且第二次**一条都没删**。

    `deleted == 0` 这一半才是有信息量的：只看 `count()` 不变的话，
    "删了 5 条又加了 5 条"和"什么都没发生"长得一模一样。
    """
    corpus = _mutable_corpus(tmp_path, fixture_corpus_dir)

    first = _ingest(corpus, collection, hashing_embedder)
    ids_before = _ids(collection)
    count_before = collection.count()

    second = _ingest(corpus, collection, hashing_embedder)

    assert second.deleted == 0, "同样的输入不该产生任何删除"
    assert second.upserted == first.upserted
    assert _ids(collection) == ids_before
    assert collection.count() == count_before


def test_chunk_id_is_bound_to_content_not_position(
    tmp_path: Path,
    fixture_corpus_dir: Path,
    collection: Collection,
    hashing_embedder: HashingEmbedder,
) -> None:
    """改一个字符 = 换一个 id。

    若 id 是 `f"{doc}#{序号}"`，这段文字被编辑后 id 不变，`upsert` 会**覆盖**它 ——
    而覆盖是对的；错的是编辑**前面**的段落时后面全部错位，检索会返回张冠李戴的引用。
    内容哈希把这个失败模式整个消掉。
    """
    corpus = _mutable_corpus(tmp_path, fixture_corpus_dir)
    _ingest(corpus, collection, hashing_embedder)
    ids_before = _ids(collection)

    json_path = corpus / JSON_RELATIVE
    text = json_path.read_text(encoding="utf-8")
    assert ORIGINAL_SENTENCE in text
    # 只插一个字符："a subset of" → "a subsets of"。
    edited = text.replace("a subset of", "a subsets of")
    assert edited != text
    json_path.write_text(edited, encoding="utf-8")

    report = _ingest(corpus, collection, hashing_embedder)
    ids_after = _ids(collection)

    assert report.deleted == 1, "改了内容的那一段应当恰好淘汰一个旧 chunk"
    assert len(ids_before - ids_after) == 1
    assert len(ids_after - ids_before) == 1


def test_removing_a_document_prunes_every_chunk_of_it(
    tmp_path: Path,
    fixture_corpus_dir: Path,
    collection: Collection,
    hashing_embedder: HashingEmbedder,
) -> None:
    """删掉一页，它的 chunk 必须**全部**出库 —— 否则答案会引用一个 404 链接。"""
    corpus = _mutable_corpus(tmp_path, fixture_corpus_dir)
    _ingest(corpus, collection, hashing_embedder)
    assert "https://docs.python.org/3.12/library/json.html" in _doc_urls(collection)

    (corpus / JSON_RELATIVE).unlink()
    report = _ingest(corpus, collection, hashing_embedder)

    assert report.deleted > 0
    assert "https://docs.python.org/3.12/library/json.html" not in _doc_urls(collection)


def test_paths_mode_leaves_other_pages_untouched(
    tmp_path: Path,
    fixture_corpus_dir: Path,
    collection: Collection,
    hashing_embedder: HashingEmbedder,
) -> None:
    """D9 的安全阀：`--paths` 收敛后，**范围外**的页面必须原封不动。

    没有这道闸，开发期"我只重建 json 这一页"会变成整库清空 ——
    而且不报错，只是从此什么都检索不到。
    """
    corpus = _mutable_corpus(tmp_path, fixture_corpus_dir)
    _ingest(corpus, collection, hashing_embedder)

    # 先记下范围外页面的全部 chunk id。
    outside = {
        str(chunk_id)
        for chunk_id, meta in zip(
            collection.get(include=["metadatas"])["ids"],
            collection.get(include=["metadatas"])["metadatas"] or [],
            strict=True,
        )
        if meta["doc_url"] != "https://docs.python.org/3.12/library/json.html"
    }
    assert outside, "夹具里应有 json 之外的页面"

    report = ingest_corpus(
        corpus, collection=collection, embedder=hashing_embedder, paths=[JSON_RELATIVE]
    )

    assert report.documents == 1
    assert outside <= _ids(collection), "范围外页面的 chunk 被 --paths 模式误删了"


def test_paths_mode_still_prunes_inside_its_scope(
    tmp_path: Path,
    fixture_corpus_dir: Path,
    collection: Collection,
    hashing_embedder: HashingEmbedder,
) -> None:
    """安全阀收窄的是**范围**，不是**语义**：范围内的过期 chunk 照样要清。"""
    corpus = _mutable_corpus(tmp_path, fixture_corpus_dir)
    _ingest(corpus, collection, hashing_embedder)

    json_path = corpus / JSON_RELATIVE
    json_path.write_text(
        json_path.read_text(encoding="utf-8").replace(ORIGINAL_SENTENCE, "Rewritten entirely."),
        encoding="utf-8",
    )

    report = ingest_corpus(
        corpus, collection=collection, embedder=hashing_embedder, paths=[JSON_RELATIVE]
    )

    assert report.deleted == 1


def test_paths_filter_matching_nothing_raises_corpus_missing(
    tmp_path: Path,
    fixture_corpus_dir: Path,
    collection: Collection,
    hashing_embedder: HashingEmbedder,
) -> None:
    """拼错路径时要**报错**，不能静默跑成"0 文档 0 chunk，一切正常"。"""
    corpus = _mutable_corpus(tmp_path, fixture_corpus_dir)

    with pytest.raises(CorpusMissing):
        ingest_corpus(
            corpus, collection=collection, embedder=hashing_embedder, paths=["docs/nope.html"]
        )


def test_empty_corpus_dir_raises_corpus_missing(
    tmp_path: Path, collection: Collection, hashing_embedder: HashingEmbedder
) -> None:
    """空目录同样要报错（§3.2：`CorpusMissing` → 503，不是"答了个空"）。"""
    empty = tmp_path / "empty-corpus"
    empty.mkdir()

    with pytest.raises(CorpusMissing):
        ingest_corpus(empty, collection=collection, embedder=hashing_embedder)


# ---------- dry-run 的只读承诺（§5.5 / 判据 14）----------


def test_index_exists_never_creates_the_directory(chroma_path: Path) -> None:
    """`index_exists()` 只读盘。**"打开看看"不能有副作用**。

    这条守的是 C1：`PersistentClient` 的构造函数会立刻建目录和库文件，
    所以判断"索引建过没有"这件事不能靠开一个客户端来做。
    第二段同时把 `chroma.sqlite3` 这个**上游文件名假设**钉住 ——
    上游哪天改名，这里会红，而不是让 `index_exists()` 静默地永远返回 `False`
    （那样 `--dry-run` 会一直报"删除 0 块"，看起来完全正常）。
    """
    assert store_module.index_exists(chroma_path) is False
    assert not chroma_path.exists(), "查询索引是否存在本身建了目录"

    store_module.get_or_create_collection(
        path=chroma_path,
        metadata=store_module.build_index_metadata(model_id="test-hashing-64", dimension=64),
    )

    assert store_module.index_exists(chroma_path) is True


def test_dry_run_does_not_write_the_chroma_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_corpus_dir: Path
) -> None:
    """`ingest --dry-run` 是**只读预演**，跑完不许多出任何文件（§5.5 / §4）。

    这条是补一个真出现过的漏洞：`_dry_run` 原本直接 `open_collection()`，
    而它会构造客户端 → 在 `data/chroma` 里建出 `chroma.sqlite3`。
    一次"先看一眼要动多少数据"的预演不该在禁区里留下产物。

    `CHROMA_DIR` 必须**同时**打在 `store` 模块上：`ingest` 持有的是模块对象引用，
    改 `config.CHROMA_DIR` 影响不到 `store` 里那个 `from ... import` 绑定。
    """
    target = tmp_path / "chroma"
    monkeypatch.setattr(store_module, "CHROMA_DIR", target)

    exit_code = main(["--dry-run", "--corpus", str(fixture_corpus_dir)])

    assert exit_code == 0
    assert not target.exists(), "dry-run 建出了向量库目录"


def test_dry_run_counts_chunks_without_touching_the_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture_corpus_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """预演的数字要**对**：只读不等于可以不做事。

    "3 篇 / 7 块" 是夹具语料的既定事实（与 CLI 手工跑出来的输出一致）；
    写成 0 也能让上一条测试通过 —— 那正是"关闭功能"冒充"修好 bug"。
    """
    monkeypatch.setattr(store_module, "CHROMA_DIR", tmp_path / "chroma")

    main(["--dry-run", "--corpus", str(fixture_corpus_dir)])

    assert "文档 3 篇" in capsys.readouterr().out
