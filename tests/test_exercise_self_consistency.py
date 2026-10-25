"""夹具自洽率（SPEC §5.18）：参考答案必须能在**沙箱**里通过它自己的测试。

默认 `pytest` 不跑（`-m 'not docker'`），因为它需要 Docker daemon 与
`pyassist-sandbox:1` 镜像。刻意**不用** `pytest.skip` 兜底：既然有人手动跑了
`-m docker`，环境没准备好就应该红，而不是给一个"全绿"的假象。
"""

from __future__ import annotations

import pytest

from pyassist.agent.tools.grading import compose_test_script, is_passed, parse_test_report
from pyassist.config import EXERCISES_DATASET_PATH
from pyassist.evals.run_v3_eval import _load_records, _spec_of
from pyassist.sandbox.runner import build_sandbox_runner

pytestmark = pytest.mark.docker


async def test_every_reference_solution_passes_its_own_tests() -> None:
    runner = build_sandbox_runner()
    assert runner is not None, "需要 Docker daemon 与 pyassist-sandbox:1 镜像"

    failures: list[str] = []
    records = _load_records(EXERCISES_DATASET_PATH)
    for record in records:
        spec = _spec_of(record)
        tests_source = "\n\n".join(test.source for test in spec.tests)
        result = await runner.run(compose_test_script(spec.reference_solution, tests_source))
        report = parse_test_report(result)
        if not is_passed(report):
            failures.append(
                f"{spec.exercise_id}: status={result.status} "
                f"passed={report.passed}/{report.total} complete={report.is_complete}"
            )

    assert not failures, f"以下夹具的参考答案过不了自身测试：{failures}"
