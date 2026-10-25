"""v2 评估集与算术守护（SPEC §5.14 / 判据 13）。

`errors.jsonl` 是错误类型准确率的 ground truth，与 `qa.jsonl` 同级：
**只增不改**（§4）。这里同时钉住两件事：

1. **数据集形状**：恰好 20 条、键集合精确、id 唯一、`expected_error_type` 是标识符；
2. **可解析性**：`parse_traceback` 从每条真实 traceback 里提取出的类型
   必须与 `expected_error_type` 一致 —— 数据集与解析器任何一边漂移，这里先红。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from pyassist.agent.tools.docs import parse_traceback
from pyassist.config import ERRORS_DATASET_PATH, MIN_ERROR_RECORDS
from pyassist.evals.metrics import error_type_accuracy, fix_pass_rate
from pyassist.evals.run_v2_eval import _load_records, _report, _Row

REQUIRED_KEYS = {"id", "topic", "error_text", "broken_code", "expected_error_type"}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _load() -> list[dict[str, Any]]:
    """按行解析 JSONL。**不用 `pytest.skip`**：文件缺失就是失败，不是"跳过"。"""
    path: Path = ERRORS_DATASET_PATH
    assert path.exists(), f"评估集不存在：{path}"

    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - 失败时给出行号才可修
            raise AssertionError(f"{path} 第 {number} 行不是合法 JSON：{exc}") from exc
    return records


@pytest.fixture(scope="module")
def records() -> list[dict[str, Any]]:
    return _load()


# ---------- 数据集形状 ----------


def test_exactly_twenty_records(records: list[dict[str, Any]]) -> None:
    """SPEC §5.14 定死 20 条，且 `MIN_ERROR_RECORDS` 与它同源。"""
    assert len(records) == 20
    assert len(records) >= MIN_ERROR_RECORDS


def test_every_record_has_exactly_the_frozen_schema(records: list[dict[str, Any]]) -> None:
    for record in records:
        assert set(record) == REQUIRED_KEYS, f"{record.get('id')} 的字段是 {sorted(record)}"


def test_ids_are_unique_and_non_empty(records: list[dict[str, Any]]) -> None:
    ids = [record["id"] for record in records]
    assert all(isinstance(item, str) and item.strip() for item in ids)
    assert len(ids) == len(set(ids)), "id 有重复"


def test_text_fields_are_non_empty_strings(records: list[dict[str, Any]]) -> None:
    for record in records:
        for key in ("id", "topic", "error_text", "broken_code", "expected_error_type"):
            value = record[key]
            assert isinstance(value, str), f"{record['id']} 的 {key} 不是字符串"
            assert value.strip(), f"{record['id']} 的 {key} 为空"


def test_expected_types_are_identifiers(records: list[dict[str, Any]]) -> None:
    """`expected_error_type` 必须是裸类型名，不能带模块前缀、括号或消息。"""
    for record in records:
        expected = record["expected_error_type"]
        assert _IDENTIFIER.match(expected), f"{record['id']} 的期望类型不是标识符：{expected}"


def test_expected_types_are_all_distinct(records: list[dict[str, Any]]) -> None:
    """20 条覆盖 20 种类型（SPEC：每类 1–2 条，目标是覆盖而不是堆重复）。"""
    expected = [record["expected_error_type"] for record in records]
    assert len(set(expected)) == 20


def test_error_text_is_a_real_traceback_ending_with_expected_type(
    records: list[dict[str, Any]],
) -> None:
    """原样采集的 traceback 至少要有 `Traceback` 或位置行，且末行是那个异常。

    这不是"格式洁癖"：`parse_traceback` 从末尾往前找异常行，
    一条手写的、只贴了异常名的文本会让准确率虚高。
    """
    for record in records:
        lines = [line for line in record["error_text"].splitlines() if line.strip()]
        assert len(lines) >= 2, f"{record['id']} 的 error_text 太短"
        last = lines[-1]
        actual = last.split(":", 1)[0].rsplit(".", 1)[-1]
        assert actual == record["expected_error_type"], (
            f"{record['id']} 的末行是 {last!r}，与 expected_error_type 不符"
        )


def test_parse_traceback_recovers_every_expected_type(records: list[dict[str, Any]]) -> None:
    """数据集与解析器绑在一起：任何一边改了归一化规则，这条会立刻红。"""
    for record in records:
        signature = parse_traceback(record["error_text"])
        assert signature.exception_type == record["expected_error_type"], (
            f"{record['id']} 解析成 {signature.exception_type}"
        )


def test_broken_code_is_standalone_python(records: list[dict[str, Any]]) -> None:
    """坏代码要能被沙箱单独执行（不是片段拼贴）。语法错误那两条本身就无法编译 ——
    `compile` 失败正是用例的目的，所以这里只断言非空与无 markdown 围栏。"""
    for record in records:
        code = record["broken_code"]
        assert "```" not in code, f"{record['id']} 的 broken_code 带了围栏"
        assert not code.startswith(" "), f"{record['id']} 的 broken_code 有整体缩进"


# ---------- 算术 ----------


def test_error_type_accuracy_counts_exact_matches() -> None:
    assert error_type_accuracy(["ValueError", "TypeError"], ["ValueError", "TypeError"]) == 1.0
    assert error_type_accuracy(["ValueError", "KeyError"], ["ValueError", "TypeError"]) == 0.5


def test_error_type_accuracy_normalizes_only_module_prefix_and_whitespace() -> None:
    assert (
        error_type_accuracy(
            ["json.decoder.JSONDecodeError", "  ValueError  "],
            ["JSONDecodeError", "ValueError"],
        )
        == 1.0
    )
    # 大小写不是格式噪声：`valueerror` 是错的类型名
    assert error_type_accuracy(["valueerror"], ["ValueError"]) == 0.0


def test_error_type_accuracy_counts_none_as_wrong() -> None:
    """没有结论（异常 / 空检索）计错，不从分母里剔除。"""
    assert error_type_accuracy([None, "ValueError"], ["ValueError", "ValueError"]) == 0.5


def test_error_type_accuracy_rejects_length_mismatch_and_empty() -> None:
    with pytest.raises(ValueError, match="必须等长"):
        error_type_accuracy(["ValueError"], ["ValueError", "TypeError"])
    assert error_type_accuracy([], []) == 0.0


def test_fix_pass_rate_only_counts_ok() -> None:
    assert fix_pass_rate(["ok", "error", "timeout", "rejected", None]) == 0.2
    assert fix_pass_rate(["ok", "ok"]) == 1.0
    assert fix_pass_rate([]) == 0.0


def test_report_line_is_stable(capsys: pytest.CaptureFixture[str]) -> None:
    rows = [
        _Row("a", "ValueError", "ValueError", "ok"),
        _Row("b", "TypeError", "ValueError", "timeout"),
        _Row("c", None, "IndexError", None, error="LLMBadResponse: boom"),
    ]
    _report(rows)

    out = capsys.readouterr().out
    assert "records=3" in out
    assert "error_type_accuracy=0.333" in out
    assert "fix_pass_rate=0.333" in out
    assert "timeouts=1" in out
    assert "failed_records=1" in out


def test_load_records_respects_limit() -> None:
    records = _load_records(ERRORS_DATASET_PATH, limit=2)
    assert len(records) == 2
    assert records[0]["id"] == "attribute-01"
