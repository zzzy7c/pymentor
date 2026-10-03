"""记账守护（SPEC 阶段二 §4.7）：命中与未命中分别计价；错峰边界。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from pyassist.config import MODEL_FLASH, MODEL_PRO
from pyassist.llm.tracing import is_off_peak, price_usage


def test_cache_hit_and_miss_are_priced_separately() -> None:
    """命中与未命中必须分别计价：合并成一个 prompt_tokens 会让报表严重失真（§3.1）。"""
    cost = price_usage(
        MODEL_FLASH,
        cached_tokens=1_000_000,
        uncached_input_tokens=1_000_000,
        output_tokens=1_000_000,
        is_off_peak=True,
    )
    assert cost == Decimal("0.753")  # 0.003 + 0.15 + 0.60


def test_peak_price_is_double_off_peak() -> None:
    off_peak = price_usage(
        MODEL_PRO,
        cached_tokens=0,
        uncached_input_tokens=1_000_000,
        output_tokens=0,
        is_off_peak=True,
    )
    peak = price_usage(
        MODEL_PRO,
        cached_tokens=0,
        uncached_input_tokens=1_000_000,
        output_tokens=0,
        is_off_peak=False,
    )
    assert peak == off_peak * 2


@pytest.mark.parametrize(
    ("moment", "expected_off_peak"),
    [
        (datetime(2026, 10, 5, 0, 59, tzinfo=UTC), True),  # 周一，窗口前
        (datetime(2026, 10, 5, 1, 0, tzinfo=UTC), False),  # 周一，峰值起点（含）
        (datetime(2026, 10, 5, 4, 0, tzinfo=UTC), True),  # 周一，峰值终点（不含）
        (datetime(2026, 10, 5, 6, 0, tzinfo=UTC), False),  # 周一，第二个窗口起点
        (datetime(2026, 10, 5, 10, 0, tzinfo=UTC), True),  # 周一，第二个窗口终点
        (datetime(2026, 10, 10, 2, 0, tzinfo=UTC), True),  # 周六，全时段半价
    ],
)
def test_is_off_peak_boundaries(moment: datetime, expected_off_peak: bool) -> None:
    assert is_off_peak(moment) is expected_off_peak


def test_is_off_peak_rejects_naive_datetime() -> None:
    """无时区输入直接报错：用本地时区算价格是"看起来对但全错"的典型（§4.7）。"""
    with pytest.raises(ValueError, match="时区"):
        is_off_peak(datetime(2026, 10, 5, 2, 0))


def test_unknown_model_id_is_rejected() -> None:
    """模型名只允许写在 config.py：表外模型必须报错，不能静默按 0 计价。"""
    with pytest.raises(RuntimeError, match="PRICE_TABLE"):
        price_usage(
            "some-model-that-is-not-in-the-table",
            cached_tokens=0,
            uncached_input_tokens=1,
            output_tokens=1,
            is_off_peak=True,
        )
