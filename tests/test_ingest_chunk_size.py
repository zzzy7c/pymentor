"""chunk 的**字符上界**（2026-10-13 OOM 的回归测试）。

事故现场：`uv run python -m pyassist.rag.ingest` 崩在
`sentence_transformers` 的 `_prepare_4d_attention_mask_for_sdpa`，
`DefaultCPUAllocator: not enough memory: you tried to allocate 4899708032 bytes`。

链路是：`parse_document` 用 `get_text(" ")` 把 `<li>` 的整棵子树展平成一个块
（Python 官方文档里最大 47723 字符）→ `_iter_pieces` 只对 `kind == "code"` 做超长拆分
→ 一个 4.7 万字符的块原样进 `Chunk.body` → 嵌入时 padding 到批内最长
（2187 token）× batch 256 → 注意力掩码 O(batch × seq²) 4.9GB → OOM。

所以这里测的不是"分块好不好看"，而是**"谁负责给 block 定上界"**：
CPU 内存是硬的，语义是软的，冲突时内存赢。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyassist.config import CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS
from pyassist.rag import corpus as corpus_module
from pyassist.rag.corpus import Block, CorpusDocument, ParsedDocument
from pyassist.rag.ingest import _MAX_BODY_CHARS, _split_prose, build_chunks

DOC_URL = "https://docs.python.org/3.12/library/big.html"


def _document(*blocks: Block) -> ParsedDocument:
    return ParsedDocument(doc_url=DOC_URL, title="Big Page", blocks=list(blocks))


def _para(text: str, *, section: str | None = "Big", anchor: str | None = "big") -> Block:
    return Block(text=text, section=section, anchor=anchor, kind="para")


# ---------- _split_prose：纯函数 ----------


def test_short_text_is_returned_untouched() -> None:
    """没超限就**原样返回**（连首尾空白都不动）—— 分块不许在正常输入上做任何加工。"""
    text = "a b c"
    assert _split_prose(text, max_chars=100) == [text]


def test_pieces_never_exceed_the_budget_and_lose_no_words() -> None:
    """超长正文按**词边界**拆：片片合规，且所有词都还在（不丢内容）。

    用 1..500 这种编号词而不是随机串，是为了让"第 137 个词去哪了"这种断言读得出来。
    """
    words = [f"w{i}" for i in range(500)]
    text = " ".join(words)

    pieces = _split_prose(text, max_chars=100)

    assert len(pieces) > 1, "这个长度本来就该被拆开，拆不开说明上界没生效"
    assert all(len(piece) <= 100 for piece in pieces)
    assert " ".join(pieces).split() == words, "拆分改动了词序列"


def test_a_single_overlong_word_is_hard_cut() -> None:
    """没有空白的超长串（base64 / 压缩数据 / 超长 URL）只能硬切。

    这是唯一允许在"词中间"切的地方 —— 因为没有词边界可切。
    不切的话，一条这样的数据就能把整批嵌入拖爆，代价远大于切坏一个词。
    """
    text = "x" * 250

    pieces = _split_prose(text, max_chars=100)

    assert [len(piece) for piece in pieces] == [100, 100, 50]
    assert "".join(pieces) == text


# ---------- build_chunks：上界就是从块级一路继承下来的 ----------


def test_a_single_giant_paragraph_block_is_split() -> None:
    """事故的直接复现：一个 47723 字符的块。

    这条测试在修复前会拿到一个 47723 字符的 `body`（= 崩溃的前一步），
    修复后拿到一串合规片段。
    """
    giant = _para(" ".join(f"sentence-{i}" for i in range(6000)))  # ≈ 6 万字符
    assert len(giant.text) > 40000

    chunks = build_chunks(_document(giant))

    assert len(chunks) > 1
    assert max(len(chunk.body) for chunk in chunks) <= _MAX_BODY_CHARS
    assert " ".join(chunk.body for chunk in chunks) == giant.text, "拆分丢了内容"


def test_bodies_stay_within_the_bound_that_embedding_memory_assumes() -> None:
    """上界的用处是**算内存**：`EMBEDDING_MAX_SEQ_TOKENS` 就按它推出来的。

    所以这里钉的不是某个数字好看，而是"`build_chunks` 的产出不许超过
    `_MAX_BODY_CHARS`"这个契约 —— 改分块参数时，这条先红。
    """
    blocks = [
        _para(" ".join(f"a{i}" for i in range(3000))),
        _para(" ".join(f"b{i}" for i in range(50))),
        _para(" ".join(f"c{i}" for i in range(3000))),
    ]

    chunks = build_chunks(_document(*blocks))

    assert chunks, "夹具本身应当产出 chunk"
    assert max(len(chunk.body) for chunk in chunks) <= _MAX_BODY_CHARS


def test_the_reported_bound_accounts_for_code_atomicity() -> None:
    """`_MAX_BODY_CHARS` 必须把代码块 2×max 的原子性豁免算进去。

    这条看着像在复述公式，其实在守一个具体的失败：把它收紧成 `max_chars + overlap`
    之后，`_warn_oversize` 会在**健康语料**上每次 ingest 报 73 条
    （本机 3.12 文档实测，最长 2096 字符 —— 全是被 D5 判定为"该原子保留"的代码块）。
    天天响的警告等于没有警告，真正出事那次反而没人看。
    """
    assert _MAX_BODY_CHARS == 2 * CHUNK_MAX_CHARS + CHUNK_OVERLAP_CHARS + 2


def test_a_code_block_inside_the_atomicity_window_is_not_reported_as_oversize() -> None:
    """健康语料不该触发 `_warn_oversize`：1.5×max 的代码块既是**合规**的，
    也必须**低于上界**。两件事同时成立，警告才有信息量。"""
    code = Block(
        text="\n".join(f"    register_name_{i} = handler({i})" for i in range(50)),
        section="Code",
        anchor="code",
        kind="code",
    )
    assert CHUNK_MAX_CHARS < len(code.text) < 2 * CHUNK_MAX_CHARS

    chunks = build_chunks(_document(code))

    assert max(len(chunk.body) for chunk in chunks) <= _MAX_BODY_CHARS


def test_code_blocks_keep_their_atomicity_under_two_times_max() -> None:
    """对照面：**代码块不受这条新规则影响**（SPEC D5 第 3 条）。

    正文可以按词切，代码不行 —— 从一段程序中间劈开会给出半截代码的引用。
    1.5 倍 max 的代码块必须仍是**一整块**，尽管它超过了 `CHUNK_MAX_CHARS`。
    """
    code = Block(
        text="\n".join(f"    value_{i} = compute({i})" for i in range(60)),
        section="Code",
        anchor="code",
        kind="code",
    )
    assert CHUNK_MAX_CHARS < len(code.text) < 2 * CHUNK_MAX_CHARS

    chunks = build_chunks(_document(code))

    assert len(chunks) == 1, "低于 2×max 的代码块被拆了"
    assert chunks[0].body == code.text


def test_an_overlong_code_line_is_left_whole_but_still_reported() -> None:
    """超长单行代码：**不硬切**（切了就是半行代码），但必须仍在长度上界之外可被察觉。

    这条同时说明 `_warn_oversize` 不是死代码 —— 它有真实可达的触发路径，
    只是概率低。嵌入那一侧的 token 上限是最终兜底。
    """
    code = Block(
        text="x = " + "1" * (3 * CHUNK_MAX_CHARS),
        section="Code",
        anchor="code",
        kind="code",
    )

    chunks = build_chunks(_document(code))

    assert len(chunks) == 1
    assert len(chunks[0].body) > _MAX_BODY_CHARS


# ---------- 端到端：真实 HTML 的展平路径 ----------


def test_nested_list_html_becomes_many_bounded_chunks(tmp_path: Path) -> None:
    """走**真解析器**复现事故的入口：`<li>` 里嵌 `<ul>`，`get_text(" ")` 展平成一整块。

    不构造 `ParsedDocument` 而是喂 HTML，是因为真正的坑在 `parse_document` 这一侧：
    `_extract_blocks` 只取**最外层**块级元素（内层 `<li>` 因有块级祖先被跳过），
    于是"一个列表"在形状上表现为"一个巨大的块"，而不是很多小条。
    `contents.html` 的整页目录就是这么变成 47723 字符的。
    """
    sub_items = "".join(f"<li>entry {i}</li>" for i in range(2000))
    html = (
        "<html><head><title>Big Page — Python 3.12.7 documentation</title></head><body>"
        f'<h1 id="big">Big</h1><ul><li>top entry<ul>{sub_items}</ul></li></ul>'
        "</body></html>"
    )
    document = CorpusDocument(
        path=tmp_path / "big.html", doc_url=DOC_URL, source_url=DOC_URL, html=html
    )

    parsed = corpus_module.parse_document(document)
    assert max(len(block.text) for block in parsed.blocks) > CHUNK_MAX_CHARS, (
        "解析器不再产出超长块了 —— 那这条测试的前提变了，请重新确认它是否还有意义"
    )

    chunks = build_chunks(parsed)

    assert len(chunks) > 1
    assert max(len(chunk.body) for chunk in chunks) <= _MAX_BODY_CHARS


@pytest.mark.parametrize("max_chars", [50, 200, 1000])
def test_the_bound_holds_for_any_budget(max_chars: int) -> None:
    """上界随参数走，不是把 1000 写死在某处 —— 参数化跑三档，防止只对默认值成立。"""
    giant = _para(" ".join(f"w{i}" for i in range(2000)))

    chunks = build_chunks(_document(giant), max_chars=max_chars, overlap_chars=10)

    assert max(len(chunk.body) for chunk in chunks) <= max_chars + 10 + 2
