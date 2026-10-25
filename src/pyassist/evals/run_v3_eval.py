"""v3 跑分器（SPEC §5.18）。**手动命令，不进 CI。**

四种模式：

    uv run python -m pyassist.evals.run_v3_eval --self-consistency
    uv run python -m pyassist.evals.run_v3_eval --generate --per-topic 5
    uv run python -m pyassist.evals.run_v3_eval --export-labels 30
    uv run python -m pyassist.evals.run_v3_eval --agreement labels.csv

分工与 v1/v2 一致：算术在 `evals/metrics.py`（纯函数、进 CI），本文件只负责
"取数据 → 跑真实链路 → 把算术拼起来"。

需要 Docker 的模式在条件不满足时**以退出码 2 结束并打印缺什么**，不打印 0 分 ——
"沙箱没起来"不是跑分结果，编一个 0 会让人误以为题目全坏了。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, cast

from pyassist.agent.checkpointer import open_checkpointer
from pyassist.agent.graphs.exercise import build_exercise_graph
from pyassist.agent.state import ExerciseSpec, TestCaseSpec, Topic
from pyassist.agent.tools.grading import compose_test_script, is_passed, parse_test_report
from pyassist.config import EXERCISE_TOPICS, EXERCISES_DATASET_PATH
from pyassist.db import repositories
from pyassist.db.session import open_engine, open_session
from pyassist.errors import ConfigError
from pyassist.evals.metrics import grading_agreement, self_consistency_rate
from pyassist.logging import configure_logging, get_logger
from pyassist.sandbox.runner import SandboxRunner, build_sandbox_runner

logger = get_logger(__name__)

_DATASET_FIELDS: Final = frozenset(
    {"id", "topic", "title", "prompt", "starter_code", "reference_solution", "tests"}
)
_TEST_FIELDS: Final = frozenset({"name", "source"})
_EXPORT_HEADER: Final = [
    "sample_id",
    "prompt",
    "submitted_code",
    "test_summary",
    "model_score",
    "human_score",
]
_EXIT_MISSING_DEPENDENCY: Final = 2


@dataclass(frozen=True, slots=True)
class _SelfRow:
    record_id: str
    status: str
    passed: bool
    total: int


def _load_records(path: Path) -> list[dict[str, Any]]:
    """读夹具并校验字段集合**恰好**是约定的七个（SPEC §5.18）。

    多一个少一个都抛 `ValueError`：数据集是回归基线，schema 漂移必须当场暴露。
    """
    if not path.is_file():
        raise ValueError(f"找不到评估集：{path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number} 不是合法 JSON：{exc}") from exc
        if not isinstance(record, dict) or set(record) != _DATASET_FIELDS:
            raise ValueError(
                f"{path}:{line_number} 字段集合必须恰好是 {sorted(_DATASET_FIELDS)}"
                f"，实际是 {sorted(record) if isinstance(record, dict) else type(record).__name__}"
            )
        if record["topic"] not in EXERCISE_TOPICS:
            raise ValueError(
                f"{path}:{line_number} topic 必须是 {list(EXERCISE_TOPICS)} 之一，"
                f"实际是 {record['topic']!r}"
            )
        tests = record["tests"]
        if not isinstance(tests, list) or not tests:
            raise ValueError(f"{path}:{line_number} tests 必须是非空列表")
        for test in tests:
            if not isinstance(test, dict) or set(test) != _TEST_FIELDS:
                raise ValueError(f"{path}:{line_number} 每条测试只允许 {sorted(_TEST_FIELDS)}")
        records.append(record)
    if not records:
        raise ValueError(f"{path} 是空数据集")
    return records


def _spec_of(record: Mapping[str, Any]) -> ExerciseSpec:
    """夹具 → `ExerciseSpec`。测试与参考答案都来自夹具，不来自模型。"""
    tests = record["tests"]
    return ExerciseSpec(
        exercise_id=str(record["id"]),
        topic=cast("Topic", record["topic"]),
        title=str(record["title"]),
        prompt=str(record["prompt"]),
        starter_code=str(record["starter_code"]),
        reference_solution=str(record["reference_solution"]),
        tests=[TestCaseSpec(name=str(test["name"]), source=str(test["source"])) for test in tests],
        self_check_attempts=1,
    )


def _require_runner() -> SandboxRunner:
    runner = build_sandbox_runner()
    if runner is None:
        raise ConfigError("需要 Docker daemon 与 pyassist-sandbox:1 镜像（见 README「开发启动」）")
    return runner


async def _run_self_consistency(
    records: Sequence[Mapping[str, Any]], runner: SandboxRunner
) -> list[_SelfRow]:
    """夹具参考答案过自己的测试。**这是"题目自洽"的最低门槛。**"""
    rows: list[_SelfRow] = []
    for record in records:
        spec = _spec_of(record)
        tests_source = "\n\n".join(test.source for test in spec.tests)
        script = compose_test_script(spec.reference_solution, tests_source)
        result = await runner.run(script)
        report = parse_test_report(result)
        rows.append(
            _SelfRow(
                record_id=spec.exercise_id,
                status=result.status,
                passed=is_passed(report),
                total=report.total,
            )
        )
        logger.info(
            "eval.v3.self_consistency",
            record_id=spec.exercise_id,
            sandbox_status=result.status,
            passed=report.passed,
            total=report.total,
        )
    return rows


async def _run_generate(*, per_topic: int, runner: SandboxRunner) -> list[bool]:
    """真实生成 + 自校验（走生产图，不重写一遍自校验循环）。"""
    try:
        graphs = build_exercise_graph(checkpointer=None, runner=runner)
    except ConfigError as exc:
        raise ConfigError(f"生成模式缺少模型配置：{exc}") from exc

    run_id = uuid.uuid4().hex[:8]
    passed: list[bool] = []
    for topic in EXERCISE_TOPICS:
        for position in range(per_topic):
            thread_id = f"eval-v3-{run_id}-{topic}-{position}"
            try:
                state = await graphs.generation.ainvoke(
                    {"topic": topic},
                    {"configurable": {"thread_id": thread_id}},
                )
            except ConfigError:
                raise
            except Exception as exc:
                # 单条失败要计入分母，不能中断整个跑分；异常类型与 topic 进日志，
                # 不打印 message（可能含模型输出/用户资料）。
                # 失败样本不是"没跑"：计入分母（False），并在日志里留原因。
                logger.error(
                    "eval.v3.generate_failed",
                    topic=topic,
                    position=position,
                    error=type(exc).__name__,
                )
                passed.append(False)
                continue
            report = state.get("self_check_report")
            ok = report is not None and is_passed(report)
            passed.append(ok)
            logger.info(
                "eval.v3.generated",
                topic=topic,
                position=position,
                attempts=state.get("generation_attempts"),
                is_passed=ok,
            )
    return passed


async def _export_labels(*, count: int, output: Path) -> int:
    """把最近的提交导出为人工标注 CSV。`human_score` 留空，等人填。"""
    async with open_engine() as engine, open_session(engine) as session:
        rows = await repositories.list_recent_attempts(session, limit=count)
        await repositories.commit(session)

        if not rows:
            print("没有可导出的提交；先在前端做几道题。", file=sys.stderr)
            return _EXIT_MISSING_DEPENDENCY

        exported: list[dict[str, Any]] = []
        async with open_checkpointer() as saver:
            for row in rows:
                snapshot = await saver.aget_tuple({"configurable": {"thread_id": row.exercise_id}})
                prompt = _prompt_from_snapshot(snapshot)
                if prompt is None:
                    logger.warning("eval.v3.export_missing_spec", exercise_id=row.exercise_id)
                    continue
                exported.append(
                    {
                        "sample_id": row.id,
                        "prompt": prompt,
                        "submitted_code": row.submitted_code,
                        "test_summary": f"score={row.score}; passed={row.is_passed}",
                        "model_score": "" if row.score is None else str(row.score),
                        "human_score": "",
                    }
                )

    if not exported:
        print("提交对应的题目检查点已不存在，无法导出。", file=sys.stderr)
        return _EXIT_MISSING_DEPENDENCY

    output.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：人工标注大多在 Excel 里做，少了 BOM 中文会乱码。
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_EXPORT_HEADER)
        writer.writeheader()
        writer.writerows(exported)
    print(f"samples={len(exported)} path={output}")
    return 0


def _prompt_from_snapshot(snapshot: Any) -> str | None:
    """从检查点取题目 prompt。`ExerciseSpec` 只存在检查点里（SPEC D2）。"""
    if snapshot is None:
        return None
    checkpoint = getattr(snapshot, "checkpoint", None)
    if not isinstance(checkpoint, Mapping):
        return None
    values = checkpoint.get("channel_values")
    if not isinstance(values, Mapping):
        return None
    exercise = values.get("exercise")
    if isinstance(exercise, ExerciseSpec):
        return exercise.prompt
    if isinstance(exercise, Mapping):
        prompt = exercise.get("prompt")
        return str(prompt) if isinstance(prompt, str) else None
    return None


def _read_agreement(path: Path) -> tuple[list[int], list[int]]:
    """读人工标好的 CSV。`human_score` 为空的行视为"还没标"，不计入分母。"""
    scores: list[int] = []
    human_scores: list[int] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            human = (row.get("human_score") or "").strip()
            if not human:
                continue
            try:
                scores.append(int(float(str(row.get("model_score") or "").strip())))
                human_scores.append(int(float(human)))
            except ValueError as exc:
                raise ValueError(f"{path} 里有无法解析的分数：{row}") from exc
    return scores, human_scores


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pyassist.evals.run_v3_eval",
        description="v3 跑分：夹具自洽率 / 真实生成 / 导出标注 / 一致率",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--self-consistency",
        action="store_true",
        help="夹具参考答案过自身测试（需 Docker）",
    )
    mode.add_argument(
        "--generate",
        action="store_true",
        help="真实生成 + 自校验（需 key + Docker）",
    )
    mode.add_argument(
        "--export-labels",
        type=int,
        metavar="N",
        help="导出最近 N 条提交为空白标注 CSV",
    )
    mode.add_argument(
        "--agreement",
        type=Path,
        metavar="CSV",
        help="计算人工填好的 CSV 的一致率",
    )
    parser.add_argument(
        "--per-topic", type=int, default=5, help="--generate 每个主题生成几道（默认 5）"
    )
    parser.add_argument(
        "--dataset", type=Path, default=EXERCISES_DATASET_PATH, help="--self-consistency 的数据集"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging("info", json_output=False)

    try:
        if args.self_consistency:
            records = _load_records(args.dataset)
            rows = asyncio.run(_run_self_consistency(records, _require_runner()))
            rate = self_consistency_rate([row.passed for row in rows])
            timeouts = sum(1 for row in rows if row.status == "timeout")
            print(f"records={len(rows)} self_consistency={rate:.3f} timeouts={timeouts}")
            return 0

        if args.generate:
            if args.per_topic <= 0:
                raise ValueError("--per-topic 必须大于 0")
            passed = asyncio.run(_run_generate(per_topic=args.per_topic, runner=_require_runner()))
            rate = self_consistency_rate(passed)
            failed = sum(1 for item in passed if not item)
            print(f"records={len(passed)} self_consistency={rate:.3f} failed={failed}")
            return 0

        if args.export_labels is not None:
            if args.export_labels <= 0:
                raise ValueError("--export-labels 必须大于 0")
            output = Path("docs") / "evals" / f"阶段八-标注样本-{date.today().isoformat()}.csv"
            return asyncio.run(_export_labels(count=args.export_labels, output=output))

        if args.agreement is not None:
            if not args.agreement.is_file():
                raise ValueError(f"找不到标注文件：{args.agreement}")
            scores, human_scores = _read_agreement(args.agreement)
            if not scores:
                print("CSV 里没有已填写 human_score 的行。", file=sys.stderr)
                return _EXIT_MISSING_DEPENDENCY
            print(f"samples={len(scores)} agreement={grading_agreement(scores, human_scores):.3f}")
            return 0
    except ConfigError as exc:
        print(f"缺少依赖：{exc}", file=sys.stderr)
        return _EXIT_MISSING_DEPENDENCY
    except ValueError as exc:
        print(f"输入有误：{exc}", file=sys.stderr)
        return _EXIT_MISSING_DEPENDENCY

    return _EXIT_MISSING_DEPENDENCY


if __name__ == "__main__":
    sys.exit(main())
