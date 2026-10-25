"""跑分器的算术（SPEC §5.12）。**纯函数、零 I/O** —— 于是它能进 CI。

`run_rag_eval.py` 需要真实索引 + 真实 API key，只能手动跑；但"数字算得对不对"
不该因此变成手动步骤。两个函数各有一组固定假数据的单测（`tests/test_rag_eval.py`），
覆盖边界：`k` 超过结果条数、重复 URL、答案里零标记、标记越界。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pyassist.rag.citations import Citation

# `[1]` / `[10]`。与 `rag/citations.py` 的同名正则是同一条语法 —— 那边是私有的，
# 它负责"生成与校验"，这边只负责"数"，所以各留一份而不是反向依赖。
_MARKER_RE = re.compile(r"\[(\d+)\]")


def recall_at_k(
    retrieved_urls: Sequence[str], relevant_urls: Sequence[str], *, k: int = 5
) -> float:
    """命中的相关页数 / 相关页总数。

    `retrieved_urls` 取**前 k 条**，按 `doc_url` **去重**后与相关集求交 ——
    同一页的多个 chunk 是同一页的命中，不能重复计数。`k <= 0` 视为零命中。

    相关集为空时返回 `0.0`：一条没有 ground truth 的用例不该被算成满分。
    """
    relevant = set(relevant_urls)
    if not relevant or k <= 0:
        return 0.0
    # `dict.fromkeys` 去重且保序：评测报表要能按顺序回看"第几条命中"
    hit = len(set(dict.fromkeys(retrieved_urls[:k])) & relevant)
    return hit / len(relevant)


def citation_accuracy(
    answer: str, citations: Sequence[Citation], relevant_urls: Sequence[str]
) -> float:
    """答案里出现的每个 `[n]`，其 `Citation.doc_url` 是否在 `relevant_urls` 里。

    **这是对 `AGENTS.md` §5.2「引用准确率」的可判定代理**：真正的定义是
    "链接指向的文档确实包含支撑该结论的片段"，那需要语义判断；本阶段用
    "链接指向的文档是否属于该题的相关文档集"来近似（SPEC §10.3 记录了偏差）。

    边界：
    - 答案里**零标记** → `0.0`（不是 1.0）：没有引用就不算"引用准确"，
      否则"什么都不引用"反而拿满分，那等于奖励不溯源；
    - 标记越界（`[9]` 但只有 3 条引用）→ 记为**不准确**，
      因为读者点不到它 —— 这正是要防的"LLM 自由发挥引用格式"（D8）。
    """
    markers = _MARKER_RE.findall(answer)
    if not markers:
        return 0.0
    by_index = {citation.index: citation for citation in citations}
    relevant = set(relevant_urls)
    correct = sum(
        1
        for marker in markers
        if (citation := by_index.get(int(marker))) is not None and citation.doc_url in relevant
    )
    return correct / len(markers)


def answers_without_citation(answer: str) -> bool:
    """答案里一个 `[n]` 都没有。§5.2 要求这类回答计数为 0。"""
    return not _MARKER_RE.search(answer)


# ---------- v2：错误类型识别 / 修复通过（SPEC §5.14）----------


def error_type_accuracy(predicted: Sequence[str | None], expected: Sequence[str]) -> float:
    """预测的异常类型与期望**逐条全等**的比例。

    不做 LLM 裁判（SPEC §5.14）：异常类型是可判定的字符串比较。
    只归一化两处**格式噪声**，不放过任何语义差异：

    - 去掉首尾空白；
    - 取最后一段：`json.decoder.JSONDecodeError` → `JSONDecodeError`
      （与 `parse_traceback` 的归一化规则一致）。

    大小写**不归一化** —— `valueerror` 是错的类型名，应该计错。
    `None`（该条没产出结论）计错，而不是从分母里剔除。
    """
    if len(predicted) != len(expected):
        raise ValueError(f"predicted 与 expected 必须等长：{len(predicted)} != {len(expected)}")
    if not expected:
        return 0.0
    correct = sum(
        1
        for item, want in zip(predicted, expected, strict=True)
        if _normalize_type(item) == want.strip()
    )
    return correct / len(expected)


def fix_pass_rate(statuses: Sequence[str | None]) -> float:
    """沙箱 `status == "ok"` 的比例。`None`（未验证）与 timeout/rejected 都算不通过。

    判定依据只有退出状态 —— "看起来对"不算（§5.3）。空输入返回 `0.0`。
    """
    if not statuses:
        return 0.0
    return sum(1 for status in statuses if status == "ok") / len(statuses)


def _normalize_type(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().rsplit(".", 1)[-1]


# ---------- v3：练习自洽率 / 批改一致率（SPEC §5.18）----------


def self_consistency_rate(passed: Sequence[bool]) -> float:
    """参考答案通过自身测试的比例（v3 要求 100%，§5.4）。

    空输入返回 `0.0`：没有样本不是满分。
    """
    if not passed:
        return 0.0
    return sum(1 for item in passed if item) / len(passed)


def grading_agreement(
    scores: Sequence[int], human_scores: Sequence[int], tolerance: int = 10
) -> float:
    """模型分数与人工分数"相差不超过 `tolerance`"的比例（§5.4）。

    分母是**全部样本**：人工标错、模型离谱都算不一致，不从分母里剔除。
    空输入返回 `0.0`；长度不等是调用方 bug，直接抛 `ValueError`，
    避免"少传一半样本反而一致率 100%"。
    """
    if len(scores) != len(human_scores):
        raise ValueError(f"scores 与 human_scores 必须等长：{len(scores)} != {len(human_scores)}")
    if not scores:
        return 0.0
    agreed = sum(
        1
        for score, human in zip(scores, human_scores, strict=True)
        if abs(score - human) <= tolerance
    )
    return agreed / len(scores)
