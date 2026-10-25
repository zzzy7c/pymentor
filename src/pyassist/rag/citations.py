"""★ 唯一引用构造与校验（§2 单点所有权 / §5.2「引用由 `rag/citations.py` 生成」）。

**引用不是 LLM 自由发挥的文本**，而是由检索结果机械派生的对象。§5.2 要求
引用准确率 ≥ 0.90，这个门能不能守住，取决于"答案里的 `[1]` 到底指向哪一段"
这件事是否有唯一、可核验的答案。所以：

**编号 = 检索序位（rank + 1），chunk 与 Citation 一一对应，不去重、不重排。**

同一页的两个 chunk 拿两个编号、指向同一 `doc_url` 的**不同锚点** ——
这比按 URL 去重**更有利于 grounding**：引用准确率的核验是"引用 → 片段"级别的，
按页去重会把"具体哪一段支撑了这个结论"这个信息抹掉。

阶段六的 prompt **必须复用** `build_citations` 产出的 `index`，禁止自行编号 ——
编号与最终引用一旦错位，那个门直接失守（SPEC D8）。

本模块**不 import `chromadb` / `rag.embedder`**：它只吃 `RetrievedChunk` 的形状。
这是"引用构造"能与"向量库"完全解耦、并被纯单测覆盖的原因。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pyassist.rag.retriever import RetrievedChunk

# 引用标记：`[1]`。`\d+` 而非 `\d` —— 检索到 10 条以上时 `[10]` 必须能被认出，
# 否则第 10 条引用会被静默当成"越界标记"或被漏掉。
_MARKER_RE = re.compile(r"\[(\d+)\]")

_WHITESPACE_RE = re.compile(r"\s+")

_DEFAULT_SNIPPET_CHARS = 240
_ELLIPSIS = "…"


@dataclass(frozen=True)
class Citation:
    """一条引用。`chunk_id` / `doc_url` 是阶段六做 grounding 核验时的抓手（SPEC D8）。"""

    index: int
    chunk_id: str
    title: str
    section: str | None
    doc_url: str
    url: str  # 带锚点的可点击 URL
    snippet: str


def build_citations(
    chunks: Sequence[RetrievedChunk], *, snippet_chars: int = _DEFAULT_SNIPPET_CHARS
) -> list[Citation]:
    """检索结果 → 引用列表。**一一对应、按序编号，不去重**（SPEC D8）。"""
    return [
        Citation(
            index=position + 1,
            chunk_id=chunk.chunk_id,
            title=chunk.title,
            section=chunk.section,
            doc_url=chunk.doc_url,
            url=chunk.source_url,
            snippet=_snippet(chunk.text, snippet_chars),
        )
        for position, chunk in enumerate(chunks)
    ]


def render_reference_block(citations: Sequence[Citation]) -> str:
    """渲染可点击的 Markdown 来源块。**空列表返回 `""`** —— 前端据此判断"无来源"，
    而不是收到一个只有标题的空壳（SPEC D8）。"""
    if not citations:
        return ""

    lines = ["**来源**"]
    for citation in citations:
        label = citation.title or citation.url
        line = f"{citation.index}. [{label}]({citation.url})"
        if citation.section:
            line = f"{line} — {citation.section}"
        lines.append(line)
    return "\n".join(lines)


def extract_markers(answer: str) -> list[int]:
    """抽出答案里的 `[n]` 标记，**按出现顺序去重**（SPEC D8）。

    顺序要留：它反映模型实际引用的次序，用集合会让"先引谁"这个信号消失。
    """
    seen: list[int] = []
    for match in _MARKER_RE.finditer(answer):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen


def find_ungrounded_markers(answer: str, citations: Sequence[Citation]) -> set[int]:
    """答案里出现、但没有对应 `Citation` 的标记（越界标记）。

    **本阶段只提供判定，不抛错** —— 处置权在阶段六的图里（重生成、或剥掉该标记）。
    在这里抛错等于把"模型多写了个 `[9]`"升级成"这次问答失败"，
    而前者是可以通过一次重生成修好的小问题（SPEC D8）。
    """
    known = {citation.index for citation in citations}
    return set(extract_markers(answer)) - known


def _snippet(text: str, snippet_chars: int) -> str:
    """折叠空白后截断，截断则补 `…`。阶段六可直接拿去给前端做预览（SPEC D8）。"""
    folded = _WHITESPACE_RE.sub(" ", text).strip()
    if snippet_chars <= 0 or len(folded) <= snippet_chars:
        return folded
    return folded[:snippet_chars].rstrip() + _ELLIPSIS
