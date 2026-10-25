"""引用构造与核验（SPEC §5.10 / 判据 12、14）。

引用是本项目的**产品承诺**：§5.2 要求"每一个引用链接指向的文档确实包含支撑该结论的片段"。
所以这里的断言比的不是格式好不好看，而是"编号会不会指错地方"：

- **不去重**（D8）：两条 chunk 指向同一页同一锚点是常事（分块重叠窗口造成），
  去重会让 `[2]` 变成 `[3]` 而答案里的编号是模型按提供的清单写的 —— 一错就是全错。
- **编号 = 排名 + 1**：模型看到的 `[n]` 必须与前端渲染的来源列表**逐个对上**。
"""

from __future__ import annotations

from chromadb.api.models.Collection import Collection

from fakes.embedder import HashingEmbedder
from pyassist.rag.citations import (
    build_citations,
    extract_markers,
    find_ungrounded_markers,
    render_reference_block,
)
from pyassist.rag.retriever import RetrievedChunk, retrieve


def _chunk(
    *,
    chunk_id: str = "c1",
    text: str = "Some documentation body.",
    doc_url: str = "https://docs.python.org/3.12/library/json.html",
    source_url: str | None = None,
    title: str = "json",
    section: str | None = "Basic Usage",
    score: float = 0.9,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=text,
        doc_url=doc_url,
        source_url=source_url if source_url is not None else doc_url,
        title=title,
        section=section,
        score=score,
    )


def test_one_citation_per_chunk_and_no_deduplication() -> None:
    """**不去重**（D8）：两条同源 chunk 必须得到两条引用，编号各占一位。

    分块的重叠窗口本来就会产出"同页同锚点、正文不同"的相邻条目。
    去重会让答案里模型写的 `[2]` 指向别的东西 —— 编号是位置敏感的，不能收缩。
    """
    chunks = [
        _chunk(chunk_id="c1"),
        _chunk(chunk_id="c2"),  # 同样的 doc_url / section
        _chunk(chunk_id="c3"),
    ]

    citations = build_citations(chunks)

    assert [citation.index for citation in citations] == [1, 2, 3]
    assert [citation.chunk_id for citation in citations] == ["c1", "c2", "c3"]
    assert len(citations) == len(chunks)


def test_index_is_rank_plus_one() -> None:
    """排名 0 的 chunk 拿到 `[1]` —— 不是 `[0]`。人类的引用从 1 数起。"""
    citations = build_citations([_chunk(chunk_id="top"), _chunk(chunk_id="second")])

    assert citations[0].index == 1
    assert citations[1].index == 2


def test_url_comes_from_source_url_and_keeps_the_fragment() -> None:
    """可点击 URL 用**带锚点**的 `source_url`，而 `doc_url` 保留无锚点版本。

    两个字段各有用处：`doc_url` 用于按页归组（`read_doc` / grounding 核验），
    `url` 用于让人一眼跳到那一段。合成一个字段就必然有一边用错。
    """
    chunk = _chunk(
        doc_url="https://docs.python.org/3.12/library/asyncio-task.html",
        source_url="https://docs.python.org/3.12/library/asyncio-task.html#asyncio.gather",
    )

    citation = build_citations([chunk])[0]

    assert citation.doc_url == "https://docs.python.org/3.12/library/asyncio-task.html"
    assert citation.url.endswith("#asyncio.gather")
    assert "#" not in citation.doc_url


def test_snippet_collapses_whitespace_and_truncates_with_an_ellipsis() -> None:
    """snippet 是给前端做预览的：折掉换行，超长补 `…`。"""
    text = "first   line\n\n   second line\tthird line"

    citation = build_citations([_chunk(text=text)], snippet_chars=20)[0]

    assert "\n" not in citation.snippet
    assert "  " not in citation.snippet
    assert citation.snippet.endswith("…")
    assert len(citation.snippet) == 21  # 20 字符 + 省略号本身


def test_short_text_is_not_ellipsized() -> None:
    """短文本原样返回。给它补一个 `…` 会让"被截断了"这个信号失去意义。"""
    citation = build_citations([_chunk(text="short text")], snippet_chars=240)[0]

    assert citation.snippet == "short text"


def test_empty_input_yields_empty_outputs() -> None:
    """空列表 → `[]` 与 `""`（D8）：前端据 `""` 判断"这次没有来源"，
    而不是收到一个只有 `**来源**` 标题的空壳。"""
    assert build_citations([]) == []
    assert render_reference_block([]) == ""


def test_reference_block_is_numbered_clickable_markdown() -> None:
    citations = build_citations(
        [
            _chunk(
                chunk_id="c1",
                title="json — JSON encoder and decoder",
                section="Basic Usage",
                source_url="https://docs.python.org/3.12/library/json.html#basic-usage",
            )
        ]
    )

    block = render_reference_block(citations)

    assert "1. [json — JSON encoder and decoder]" in block
    assert "(https://docs.python.org/3.12/library/json.html#basic-usage)" in block
    assert "Basic Usage" in block


def test_reference_block_falls_back_to_the_url_when_title_is_missing() -> None:
    """`title` 为空时用 URL 当链接文字 —— 空标签会渲染成一个点不动的 `[]`。"""
    citation = build_citations([_chunk(title="", section=None)])[0]

    assert "[https://docs.python.org" in render_reference_block([citation])


def test_extract_markers_keeps_first_appearance_order_and_deduplicates() -> None:
    """顺序是**信息**：它反映模型实际引用的次序，用集合会让这个信号消失。"""
    assert extract_markers("见 [3]，另见 [1]，再看 [3]。") == [3, 1]


def test_extract_markers_on_text_without_markers_is_empty() -> None:
    assert extract_markers("这段话没有任何引用。") == []


def test_find_ungrounded_markers_reports_only_out_of_range_numbers() -> None:
    """越界标记**只判定、不抛错**（D8）：处置权在阶段六的图里。

    在这里抛错等于把"模型多写了个 `[9]`"升级成"这次问答失败"，
    而前者一次重生成就能修好。
    """
    citations = build_citations([_chunk(chunk_id="c1"), _chunk(chunk_id="c2")])

    assert find_ungrounded_markers("见 [1] 与 [9]。", citations) == {9}
    assert find_ungrounded_markers("见 [1] 与 [2]。", citations) == set()
    assert find_ungrounded_markers("没有任何标记。", citations) == set()


async def test_citations_from_a_real_retrieval_carry_the_section_anchor(
    collection: Collection,
) -> None:
    """端到端：检索 → 引用，锚点确确实实来自索引，而不是被某处拼丢掉。

    只用 `RetrievedChunk` 手搓的测试**测不出**这个 —— 它们直接给了 `source_url`。
    这条走真实的 `build_chunks` → `upsert` → `retrieve` 链路。
    """
    results = await retrieve(
        "If return_exceptions is False, the first raised exception is immediately propagated",
        collection=collection,
        embedder=HashingEmbedder(),
    )
    assert results, "夹具语料里应有讲 return_exceptions 的 chunk"

    citations = build_citations(results)

    assert len(citations) == len(results)
    assert all(citation.url.startswith(citation.doc_url) for citation in citations)
    assert any(citation.url.endswith("#asyncio.gather") for citation in citations)
    assert any(
        citation.section and citation.section.endswith("asyncio.gather") for citation in citations
    )
