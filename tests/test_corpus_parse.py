"""语料解析层守护（SPEC §5.10 / 判据 10）。

全部离线：`parse_document` 与 `iter_corpus_documents` 是纯函数 / 只读盘，
`resolve_docs_archive_url` 用 `respx` mock —— 一个字节都不出网（§5.1）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from pyassist.config import DOCS_ARCHIVE_URL_FALLBACK, DOCS_DOWNLOAD_PAGE_URL
from pyassist.rag.corpus import (
    CorpusDocument,
    iter_corpus_documents,
    parse_document,
    resolve_docs_archive_url,
)

ASYNCIO_RELATIVE = "docs/library/asyncio-task.html"
PEP_RELATIVE = "peps/pep-0008.html"


def _load(corpus_dir: Path, relative: str) -> CorpusDocument:
    """按相对路径取一篇夹具文档 —— 走生产的 URL 映射，不手搓 `CorpusDocument`。"""
    for document in iter_corpus_documents(corpus_dir):
        if document.path.relative_to(corpus_dir).as_posix() == relative:
            return document
    raise AssertionError(f"夹具语料里找不到 {relative}")


def test_title_has_sphinx_suffix_stripped(fixture_corpus_dir: Path) -> None:
    """`<title>` 尾巴 " — Python 3.12.7 documentation" 必须剥掉：
    它留在 `title` 里会进每一个 chunk 的嵌入前缀，且对检索毫无信息量。"""
    parsed = parse_document(_load(fixture_corpus_dir, ASYNCIO_RELATIVE))

    assert parsed.title == "Coroutines and Tasks"
    assert "documentation" not in parsed.title


def test_script_and_nav_are_not_parsed(fixture_corpus_dir: Path) -> None:
    """`nav` / `script` 一旦混进正文，检索会命中"导航栏"这种零信息量的块。"""
    parsed = parse_document(_load(fixture_corpus_dir, ASYNCIO_RELATIVE))
    joined = "\n".join(block.text for block in parsed.blocks)

    # `<nav>` 里那串字和 `<script>` 里那串字都不许出现在任何块里。
    assert "navigation must be stripped" not in joined
    assert "var analytics" not in joined


def test_headerlink_paragraph_marker_is_removed(fixture_corpus_dir: Path) -> None:
    """Sphinx 的 ¶ 锚符号必须剥掉 —— 否则每个块的尾巴都挂一个 `¶`。"""
    parsed = parse_document(_load(fixture_corpus_dir, ASYNCIO_RELATIVE))

    assert not any("¶" in block.text for block in parsed.blocks)
    assert not any("Link to this heading" in block.text for block in parsed.blocks)


def test_code_block_stays_atomic_and_keeps_indentation(fixture_corpus_dir: Path) -> None:
    """`<pre>` 必须原子保留、缩进必须原样：

    折叠空白会把缩进这个 Python 语义的一部分抹掉。一段 `def` 缩进没了，
    检索能命中，引用却给出错误代码 —— 比召回不到更糟。
    """
    parsed = parse_document(_load(fixture_corpus_dir, ASYNCIO_RELATIVE))
    code_blocks = [block for block in parsed.blocks if block.kind == "code"]

    assert len(code_blocks) == 1
    code = code_blocks[0].text
    assert "asyncio.gather" in code
    assert code.startswith("await asyncio.gather(*aws,")
    # 续行的对齐空格是原文的一部分，不能被折掉。
    assert "\n                       return_exceptions=False)" in code


def test_section_and_anchor_follow_the_heading_stack(fixture_corpus_dir: Path) -> None:
    """`section` 是标题链，`anchor` 是**最近**那个标题的 id —— 引用靠它落到小节顶部。"""
    parsed = parse_document(_load(fixture_corpus_dir, ASYNCIO_RELATIVE))

    gather_paragraphs = [
        block for block in parsed.blocks if block.text.startswith("If return_exceptions")
    ]
    assert gather_paragraphs, "夹具里应有讲 return_exceptions 的段落"
    block = gather_paragraphs[0]

    assert block.anchor == "asyncio.gather"
    assert block.section is not None
    assert block.section.endswith("asyncio.gather")
    assert block.section.startswith("Coroutines and Tasks")


def test_definition_list_and_list_items_become_blocks(fixture_corpus_dir: Path) -> None:
    """`dt` / `dd` / `li` 都要成块。它们是 API 签名的载体，漏掉会直接损失召回。"""
    parsed = parse_document(_load(fixture_corpus_dir, ASYNCIO_RELATIVE))
    texts = [block.text for block in parsed.blocks]

    assert any("CancelledError" in text for text in texts)
    assert any("support cancellation and introspection" in text for text in texts)


def test_iter_corpus_documents_is_sorted_and_filtered(fixture_corpus_dir: Path) -> None:
    """扫描面：确定的顺序 + 排除清单生效 + 未知前缀整个跳过（SPEC §2.5）。"""
    relatives = [
        document.path.relative_to(fixture_corpus_dir).as_posix()
        for document in iter_corpus_documents(fixture_corpus_dir)
    ]

    assert relatives == sorted(relatives), "顺序必须确定 —— 幂等的前提之一"
    assert ASYNCIO_RELATIVE in relatives
    assert PEP_RELATIVE in relatives
    # 排除清单：`_static/` 目录与 `genindex.html` 文件各走一条规则。
    assert not any("_static" in relative for relative in relatives)
    assert "docs/genindex.html" not in relatives


def test_doc_url_mapping_matches_the_spec_table(fixture_corpus_dir: Path) -> None:
    """§2.5 的映射表。

    PEP 那条是重点：落盘是 `peps/pep-0008.html`，而**上游 URL 没有 `.html` 后缀**
    （`peps.python.org/pep-0008/`）。按落盘路径硬拼会得到 404 的引用链接。
    """
    by_relative = {
        document.path.relative_to(fixture_corpus_dir).as_posix(): document
        for document in iter_corpus_documents(fixture_corpus_dir)
    }

    assert (
        by_relative[ASYNCIO_RELATIVE].doc_url
        == "https://docs.python.org/3.12/library/asyncio-task.html"
    )
    assert by_relative[PEP_RELATIVE].doc_url == "https://peps.python.org/pep-0008/"


def test_resolve_docs_archive_url_extracts_the_link() -> None:
    """两步解析的**第一步**：从 `download.html` 抽出 zip 链接（SPEC §2.4）。"""
    page = """
    <html><body>
      <a href="archives/python-3.12-docs-html.zip">HTML</a>
      <a href="archives/python-3.12-docs-pdf-a4.zip">PDF</a>
    </body></html>
    """
    with respx.mock:
        respx.get(DOCS_DOWNLOAD_PAGE_URL).mock(return_value=httpx.Response(200, text=page))
        with httpx.Client() as client:
            resolved = resolve_docs_archive_url(client)

    assert resolved == DOCS_ARCHIVE_URL_FALLBACK


def test_resolve_docs_archive_url_falls_back_on_unreachable_page() -> None:
    """抓不到页面时回退到常量，**不抛** —— 上游抖动不该让整次 ingest 停摆。"""
    with respx.mock:
        respx.get(DOCS_DOWNLOAD_PAGE_URL).mock(side_effect=httpx.ConnectError("boom"))
        with httpx.Client() as client:
            resolved = resolve_docs_archive_url(client)

    assert resolved == DOCS_ARCHIVE_URL_FALLBACK


@pytest.mark.parametrize("relative", [ASYNCIO_RELATIVE, PEP_RELATIVE])
def test_every_fixture_document_yields_blocks(fixture_corpus_dir: Path, relative: str) -> None:
    """夹具语料里**不放空文档**：全零嵌入与任何东西的余弦距离都退化为 1.0，
    会让排序看起来像巧合（见 `tests/fakes/embedder.py` 的说明）。"""
    parsed = parse_document(_load(fixture_corpus_dir, relative))

    assert parsed.blocks
    assert all(block.text.strip() for block in parsed.blocks)
