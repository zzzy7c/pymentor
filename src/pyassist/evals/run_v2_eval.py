"""v2 跑分器：错误类型识别准确率 + 修复通过率（SPEC §5.14）。**手动命令，不进 CI。**

和 `run_rag_eval.py` 同一分工：算术在 `evals/metrics.py`（纯函数、进 CI），
本文件只负责"取数据 → 跑真实诊断图 → 把算术拼起来"。

    uv run python -m pyassist.evals.run_v2_eval
    uv run python -m pyassist.evals.run_v2_eval --limit 3
    uv run python -m pyassist.evals.run_v2_eval --dataset path/to/errors.jsonl

需要**真实 Docker（`pyassist-sandbox:1` 镜像）+ DEEPSEEK_API_KEY + 已建索引**。
每条 = 一张图（可能多轮工具 + 修复回环）+ 至少一次容器执行，
开销远高于 v1 跑分 —— 安排在非峰值时段（AGENTS.md §1「价格与错峰」）。
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
from typing import Any

from pyassist.agent.checkpointer import open_checkpointer
from pyassist.agent.graphs.diagnose import DiagnoseGraph, build_diagnose_graph
from pyassist.config import ERRORS_DATASET_PATH
from pyassist.errors import AppError, ConfigError
from pyassist.evals.metrics import error_type_accuracy, fix_pass_rate
from pyassist.logging import configure_logging, get_logger
from pyassist.sandbox.runner import build_sandbox_runner
from pyassist.services.diagnose import run_diagnosis

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Row:
    """一条用例的跑分结果。

    `predicted` 为 `None` 表示该条没有产出结论（异常或空检索）；
    `status` 为 `None` 表示**没有**沙箱结果。两者都按 0 计，不从分母里剔除。
    """

    record_id: str
    predicted: str | None
    expected: str
    status: str | None
    error: str | None = None


async def run(*, dataset: Path = ERRORS_DATASET_PATH, limit: int | None = None) -> list[_Row]:
    """逐条跑。返回每行结果，汇总交给 `_report()`。"""
    records = _load_records(dataset, limit=limit)
    runner = build_sandbox_runner()
    if runner is None:
        # 配置问题，不是跑分结果：沙箱都没起来，跑出来的 0 分没有信息量。
        raise ConfigError("沙箱不可用：Docker daemon 未启动或 pyassist-sandbox:1 镜像缺失")
    run_id = uuid.uuid4().hex[:8]  # 每次运行独立 thread 前缀：不复用上次的历史

    with tempfile.TemporaryDirectory(prefix="pyassist-v2-eval-") as tmp:
        database_url = f"sqlite:///{(Path(tmp) / 'eval.db').as_posix()}"
        async with open_checkpointer(database_url=database_url) as saver:
            diagnose = build_diagnose_graph(checkpointer=saver, runner=runner)
            await diagnose.preflight()

            rows: list[_Row] = []
            for record in records:
                row = await _evaluate_record(
                    record, diagnose=diagnose, thread_id=f"eval-{run_id}-{record['id']}"
                )
                rows.append(row)
                logger.info(
                    "eval.record",
                    record_id=row.record_id,
                    predicted=row.predicted,
                    expected=row.expected,
                    sandbox_status=row.status,
                    error=row.error,
                )
            return rows


async def _evaluate_record(
    record: dict[str, Any], *, diagnose: DiagnoseGraph, thread_id: str
) -> _Row:
    record_id = str(record["id"])
    expected = str(record["expected_error_type"])
    try:
        result = await run_diagnosis(
            error_text=str(record["error_text"]),
            code=str(record["broken_code"]),
            graph=diagnose.graph,
            thread_id=thread_id,
            request_id=f"eval-{record_id}",
        )
    except AppError as exc:
        # 单条失败不该让整轮跑分报废：记下来，按 0 计，继续下一条。
        return _Row(record_id, None, expected, None, f"{type(exc).__name__}: {exc.message}")

    status = None if result.verification is None else result.verification.status
    return _Row(record_id, result.error_type, expected, status)


def _load_records(dataset: Path, *, limit: int | None) -> list[dict[str, Any]]:
    """按行读 `errors.jsonl`。**只读**：评估集是回归基线，跑分器不写它（§4）。"""
    lines = dataset.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    return records[:limit] if limit is not None else records


def _report(rows: list[_Row]) -> None:
    """SPEC §5.14 的唯一输出行（`docs/evals/阶段七-跑分.md` 抄的就是它）。

    分母是**全部用例**：异常、空检索、未验证都按 0 计 —— 否则"跑挂一半"
    会表现为分数变高。
    """
    total = len(rows)
    if total == 0:
        print("records=0  （评估集为空）")
        return
    accuracy = error_type_accuracy([row.predicted for row in rows], [row.expected for row in rows])
    pass_rate = fix_pass_rate([row.status for row in rows])
    timeouts = sum(1 for row in rows if row.status == "timeout")
    failed = sum(1 for row in rows if row.error is not None)
    print(
        f"records={total}  error_type_accuracy={accuracy:.3f}  "
        f"fix_pass_rate={pass_rate:.3f}  timeouts={timeouts}"
    )
    if failed:
        print(f"failed_records={failed}（按 0 计入上面的均值，明细见日志）")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyassist.evals.run_v2_eval",
        description="v2 跑分：错误类型识别准确率 + 修复通过率（需 Docker、索引与 API key）",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（调试用）")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ERRORS_DATASET_PATH,
        help=f"评估集路径（默认 {ERRORS_DATASET_PATH}）",
    )
    args = parser.parse_args(argv)

    configure_logging("info", json_output=False)
    try:
        rows = asyncio.run(run(dataset=args.dataset, limit=args.limit))
    except AppError as exc:
        # 典型：语料没建、缺 key、Docker 没起。这是配置问题，不是跑分结果。
        print(f"跑分无法开始：{exc.message}", file=sys.stderr)
        return 2
    _report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
