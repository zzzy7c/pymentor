"""v3 评估集与指标（SPEC §5.18）。**离线**：不跑沙箱、不调模型。

数据集是回归基线。这里钉住两件事：

1. 字段集合**恰好**是约定的七个 —— 多一个字段可能就是把隐藏测试或参考答案
   顺手塞给了客户端；
2. 指标函数的边界（空输入、容忍度边界、长度不等）—— 算错的分比没分更危险。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyassist.agent.state import ExerciseSpec
from pyassist.config import EXERCISE_TOPICS, EXERCISES_DATASET_PATH
from pyassist.evals.metrics import grading_agreement, self_consistency_rate
from pyassist.evals.run_v3_eval import _load_records, _spec_of

_EXPECTED_FIELDS = {
    "id",
    "topic",
    "title",
    "prompt",
    "starter_code",
    "reference_solution",
    "tests",
}


def _raw_records() -> list[dict[str, object]]:
    lines = EXERCISES_DATASET_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_dataset_has_exactly_eight_records_two_per_topic() -> None:
    records = _raw_records()

    assert len(records) == 8
    for topic in EXERCISE_TOPICS:
        assert sum(1 for record in records if record["topic"] == topic) == 2


def test_dataset_fields_are_exactly_the_frozen_set() -> None:
    for record in _raw_records():
        assert set(record) == _EXPECTED_FIELDS


def test_dataset_ids_are_unique() -> None:
    ids = [record["id"] for record in _raw_records()]

    assert len(ids) == len(set(ids))


def test_every_record_maps_to_a_valid_exercise_spec() -> None:
    for record in _load_records(EXERCISES_DATASET_PATH):
        spec = _spec_of(record)

        assert isinstance(spec, ExerciseSpec)
        assert spec.tests
        assert all(test.name.startswith("test_") for test in spec.tests)
        assert spec.exercise_id == record["id"]


def test_every_test_source_defines_its_declared_method() -> None:
    for record in _load_records(EXERCISES_DATASET_PATH):
        for test in record["tests"]:
            expected = f"def {test['name']}(self):"
            assert test["source"].startswith(expected)


def test_load_records_rejects_extra_fields(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    record = dict(_raw_records()[0])
    record["reference_tests"] = "泄露"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="字段集合"):
        _load_records(path)


def test_self_consistency_rate_handles_empty_and_partial() -> None:
    assert self_consistency_rate([]) == 0.0
    assert self_consistency_rate([True, True]) == 1.0
    assert self_consistency_rate([True, False, False, False]) == 0.25


def test_grading_agreement_counts_tolerance_boundary_as_agreement() -> None:
    assert grading_agreement([80, 50], [80, 40]) == 1.0
    assert grading_agreement([80, 50], [81, 39]) == 0.5
    assert grading_agreement([0], [11], tolerance=10) == 0.0


def test_grading_agreement_uses_all_samples_as_denominator() -> None:
    assert grading_agreement([100, 0, 0], [100, 50, 50]) == pytest.approx(1 / 3)


def test_grading_agreement_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError, match="等长"):
        grading_agreement([100], [100, 90])


def test_grading_agreement_handles_empty() -> None:
    assert grading_agreement([], []) == 0.0
