"""跑分器的算术（SPEC §5.12 / §5.17）。

`run_rag_eval.py` 需要真实索引 + API key，只能手动跑；但"分数算得对不对"不该
跟着变成手动步骤。这里用固定假数据把两个纯函数钉死，并顺带断言输出行格式 ——
`docs/evals/阶段六-跑分.md` 抄的就是那一行。
"""

from __future__ import annotations

from pyassist.evals.metrics import answers_without_citation, citation_accuracy, recall_at_k
from pyassist.evals.run_rag_eval import _load_records, _report, _Row
from pyassist.rag.citations import Citation

_A = "https://docs.python.org/3.12/library/asyncio-task.html"
_B = "https://docs.python.org/3.12/library/json.html"
_C = "https://peps.python.org/pep-0008/"


def _citation(index: int, doc_url: str) -> Citation:
    return Citation(
        index=index,
        chunk_id=f"chunk-{index}",
        title=f"title-{index}",
        section=None,
        doc_url=doc_url,
        url=f"{doc_url}#section-{index}",
        snippet="snippet",
    )


# ---------- recall_at_k ----------


def test_recall_at_k_counts_hits_over_relevant() -> None:
    assert recall_at_k([_A, _B], [_A, _C], k=5) == 0.5
    assert recall_at_k([_A, _C], [_A, _C], k=5) == 1.0
    assert recall_at_k([_B], [_A, _C], k=5) == 0.0


def test_recall_at_k_only_looks_at_top_k() -> None:
    # _A 在第 3 位，k=2 时不算命中
    assert recall_at_k([_B, _C, _A], [_A], k=2) == 0.0
    assert recall_at_k([_B, _C, _A], [_A], k=3) == 1.0


def test_recall_at_k_handles_short_and_empty_inputs() -> None:
    assert recall_at_k([], [_A], k=5) == 0.0
    assert recall_at_k([_A], [], k=5) == 0.0
    assert recall_at_k([_A], [_A], k=0) == 0.0
    # k 超过列表长度不越界
    assert recall_at_k([_A], [_A], k=99) == 1.0


def test_recall_at_k_dedupes_repeated_urls() -> None:
    """同一页的多个 chunk 只算一次命中；重复不会把召回率推到 > 1。"""
    assert recall_at_k([_A, _A, _A], [_A], k=5) == 1.0
    assert recall_at_k([_A, _A], [_A, _B], k=5) == 0.5


# ---------- citation_accuracy ----------


def test_citation_accuracy_counts_each_marker() -> None:
    citations = [_citation(1, _A), _citation(2, _B)]
    assert citation_accuracy("见 [1] 和 [2]。", citations, [_A, _B]) == 1.0
    assert citation_accuracy("见 [1] 和 [2]。", citations, [_A]) == 0.5


def test_citation_accuracy_zero_when_no_markers() -> None:
    """没有引用 → 0.0，**不是** 1.0：不能奖励"什么都不溯源"。"""
    assert citation_accuracy("没有标记的答案。", [_citation(1, _A)], [_A]) == 0.0
    assert answers_without_citation("没有标记的答案。") is True


def test_citation_accuracy_counts_out_of_range_markers_as_wrong() -> None:
    citations = [_citation(1, _A)]
    # [9] 越界：读者点不到它，记 0 分（D8）
    assert citation_accuracy("见 [9]。", citations, [_A]) == 0.0
    assert citation_accuracy("见 [1] 与 [9]。", citations, [_A]) == 0.5


def test_citation_accuracy_ignores_irrelevant_doc() -> None:
    citations = [_citation(1, _C)]
    assert citation_accuracy("见 [1]。", citations, [_A]) == 0.0


# ---------- 汇总行 ----------


def test_report_line_matches_spec_format(capsys) -> None:
    rows = [
        _Row("a", recall=1.0, accuracy=1.0, has_citation=True),
        _Row("b", recall=0.5, accuracy=0.5, has_citation=False),
        _Row("c", recall=0.0, accuracy=0.0, has_citation=False, error="LLMError"),
    ]
    _report(rows, k=5)

    out = capsys.readouterr().out
    first = out.splitlines()[0]
    assert first == (
        "records=3  recall@5=0.500  citation_accuracy=0.500  answers_without_citation=2"
    )
    assert "failed_records=1" in out


def test_report_uses_all_records_as_denominator(capsys) -> None:
    """失败的那条按 0 计，**不从分母里踢掉** —— 否则跑挂一半会让分数变高。"""
    rows = [
        _Row("ok", recall=1.0, accuracy=1.0, has_citation=True),
        _Row("bad", recall=0.0, accuracy=0.0, has_citation=False, error="boom"),
    ]
    _report(rows, k=5)
    assert "recall@5=0.500" in capsys.readouterr().out


# ---------- 数据集（纯文件 I/O，不触网）----------


def test_load_records_reads_real_dataset() -> None:
    records = _load_records(limit=None)
    assert len(records) >= 40
    assert all({"id", "question", "relevant_urls"} <= set(record) for record in records)
    assert len(_load_records(limit=2)) == 2
