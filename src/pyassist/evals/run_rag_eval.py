"""v1 跑分器：Recall@5 + 引用准确率（SPEC §5.12）。**手动命令，不进 CI。**

不进 CI 的理由很直白：它要**真实索引 + 真实 API key**，而 AGENTS.md §5.1 要求
测试全部离线。于是分工是：算术在 `evals/metrics.py`（纯函数、有单测、进 CI），
本文件只负责"取数据 → 调两段真实链路 → 把算术拼起来"。

    python -m pyassist.evals.run_rag_eval
    python -m pyassist.evals.run_rag_eval --limit 5 --k 5

开销提示：每条记录 = 1 次检索 + 1 次完整图运行（可能多轮工具调用）。
评估属于批量任务，**应安排在 DeepSeek 非峰值时段**（AGENTS.md §1「价格与错峰」）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pyassist.agent.checkpointer import open_checkpointer
from pyassist.agent.graphs.qa import QAGraph, build_qa_graph
from pyassist.config import DEFAULT_TOP_K, QA_DATASET_PATH
from pyassist.errors import AppError
from pyassist.evals.metrics import answers_without_citation, citation_accuracy, recall_at_k
from pyassist.logging import configure_logging, get_logger
from pyassist.rag import retriever
from pyassist.services.qa import answer

logger = get_logger(__name__)

_DEFAULT_K: Final = DEFAULT_TOP_K


@dataclass(frozen=True, slots=True)
class _Row:
    """一条用例的跑分结果。`error` 非空时该条的准确率按 0 计并计入失败数。"""

    record_id: str
    recall: float
    accuracy: float
    has_citation: bool
    error: str | None = None


async def run(*, k: int = _DEFAULT_K, limit: int | None = None) -> list[_Row]:
    """逐条跑。返回每一行的结果，汇总交给 `_report()`。"""
    records = _load_records(limit=limit)
    run_id = uuid.uuid4().hex[:8]  # 每次运行独立 thread 前缀：不复用上次的历史（D7）

    # 检查点落在临时目录：评估不该污染 `data/app.db`，重启即可丢。
    with tempfile.TemporaryDirectory(prefix="pyassist-eval-") as tmp:
        database_url = f"sqlite:///{(Path(tmp) / 'eval.db').as_posix()}"
        async with open_checkpointer(database_url=database_url) as saver:
            qa = build_qa_graph(checkpointer=saver)
            await qa.preflight()

            rows: list[_Row] = []
            for record in records:
                rows.append(
                    await _evaluate_record(
                        record, k=k, qa=qa, thread_id=f"eval-{run_id}-{record['id']}"
                    )
                )
                logger.info(
                    "eval.record",
                    record_id=record["id"],
                    recall=rows[-1].recall,
                    citation_accuracy=rows[-1].accuracy,
                    error=rows[-1].error,
                )
            return rows


async def _evaluate_record(record: dict[str, Any], *, k: int, qa: QAGraph, thread_id: str) -> _Row:
    question = str(record["question"])
    relevant_urls = [str(url) for url in record["relevant_urls"]]
    record_id = str(record["id"])

    chunks = await retriever.retrieve(question, top_k=k)
    recall = recall_at_k([chunk.doc_url for chunk in chunks], relevant_urls, k=k)

    try:
        result = await answer(
            question,
            graph=qa.graph,
            thread_id=thread_id,
            request_id=f"eval-{record_id}",
        )
    except AppError as exc:
        # 单条失败不该让整轮跑分报废：记下来，按 0 计，继续下一条。
        return _Row(record_id, recall, 0.0, False, error=f"{type(exc).__name__}: {exc.message}")

    accuracy = citation_accuracy(result.answer, result.citations, relevant_urls)
    return _Row(
        record_id,
        recall,
        accuracy,
        not answers_without_citation(result.answer),
        error=None if result.status == "ok" else "retrieval_empty",
    )


def _load_records(*, limit: int | None) -> list[dict[str, Any]]:
    """按行读 `qa.jsonl`。**只读**：评估集是回归基线，跑分器不写它（§4）。"""
    path: Path = QA_DATASET_PATH
    lines = path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    return records[:limit] if limit is not None else records


def _report(rows: list[_Row], *, k: int) -> None:
    """SPEC §5.12 的**唯一**输出行（`docs/evals/阶段六-跑分.md` 抄的就是它）。

    分母是**全部用例**：失败与空检索按 0 计，不把它们从分母里踢掉 ——
    否则"跑挂一半"会表现为分数变高。
    """
    total = len(rows)
    if total == 0:
        print("records=0  （评估集为空）")
        return
    mean_recall = sum(row.recall for row in rows) / total
    mean_accuracy = sum(row.accuracy for row in rows) / total
    without = sum(1 for row in rows if not row.has_citation)
    failed = sum(1 for row in rows if row.error is not None)
    print(
        f"records={total}  recall@{k}={mean_recall:.3f}  "
        f"citation_accuracy={mean_accuracy:.3f}  answers_without_citation={without}"
    )
    if failed:
        print(f"failed_records={failed}（按 0 计入上面的均值，明细见日志）")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyassist.evals.run_rag_eval",
        description="v1 跑分：Recall@k + 引用准确率（需真实索引与 DEEPSEEK_API_KEY）",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（调试用）")
    parser.add_argument(
        "--k", type=int, default=_DEFAULT_K, help=f"Recall@k 的 k（默认 {_DEFAULT_K}）"
    )
    args = parser.parse_args(argv)

    configure_logging("info", json_output=False)
    try:
        rows = asyncio.run(run(k=args.k, limit=args.limit))
    except AppError as exc:
        # 典型：语料没建（CorpusMissing）或缺 key（ConfigError）。这是配置问题，不是跑分结果。
        print(f"跑分无法开始：{exc.message}", file=sys.stderr)
        return 2
    _report(rows, k=args.k)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
