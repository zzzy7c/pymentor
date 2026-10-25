"""§3.2 的异常树 ↔ HTTP 状态码：**穷尽**映射（SPEC 阶段四 §4.9）。

每条用例断言 (status_code, ok)。三个 200 单独断言 —— 它们最容易被误改成 500。
"""

from __future__ import annotations

import pytest

from pyassist.api.errors import _MAPPING, _resolve
from pyassist.errors import (
    AppError,
    ConfigError,
    CorpusMissing,
    ErrorCode,
    ExerciseNotFound,
    LLMBadResponse,
    LLMError,
    LLMInsufficientBalance,
    LLMModelNotFound,
    LLMRateLimited,
    LLMTimeout,
    QuotaExceeded,
    RetrievalEmpty,
    RetrievalError,
    SandboxError,
    SandboxRejected,
    SandboxTimeout,
    StoreError,
    ValidationError,
)

CASES = [
    (ConfigError("x"), 500, False),
    (ValidationError("x"), 422, False),
    (RetrievalEmpty(), 200, True),  # ← 不是 500，也不是 ok=False 的 200
    (CorpusMissing("x"), 503, False),
    (LLMTimeout("x"), 504, False),
    (LLMRateLimited("x", retry_after=3), 429, False),
    (LLMInsufficientBalance("x"), 503, False),
    (LLMModelNotFound("x"), 500, False),
    (LLMBadResponse("x"), 502, False),
    (SandboxTimeout(), 200, True),
    (SandboxRejected(), 200, True),
    (StoreError("x"), 500, False),
    # 阶段八：两者都复用既有码（SPEC D10），但语义不同，必须分开断言。
    (ExerciseNotFound("ex_1"), 404, False),
    (QuotaExceeded(retry_after=60, limit=500_000, used=500_000), 429, False),
    (AppError("x"), 500, False),
]


@pytest.mark.parametrize(("exc", "status", "ok"), CASES, ids=lambda c: type(c).__name__)
def test_exception_maps_to_status(exc: AppError, status: int, ok: bool) -> None:
    resolved = _resolve(exc)
    assert (resolved.status, resolved.ok) == (status, ok)


def test_every_leaf_exception_is_in_the_mapping_table() -> None:
    """新增一个叶子异常却忘了登记 → 立刻红。这是映射表不腐烂的保证。"""
    intermediate = {AppError, RetrievalError, LLMError, SandboxError}

    def leaves(klass: type[AppError]) -> set[type[AppError]]:
        children = set(klass.__subclasses__())
        return {k for k in children if not k.__subclasses__()} | set().union(
            *(leaves(k) for k in children if k.__subclasses__())
        )

    missing = {k.__name__ for k in leaves(AppError) if k not in _MAPPING}
    assert not missing, f"这些叶子异常未登记到 _MAPPING：{sorted(missing)}"
    assert intermediate.isdisjoint({k for k in _MAPPING if k is not AppError})


def test_three_business_results_are_200_not_500() -> None:
    """显式重复断言，防止有人"顺手统一成 5xx"。"""
    for exc in (RetrievalEmpty(), SandboxTimeout(), SandboxRejected()):
        assert _resolve(exc).status == 200
        assert _resolve(exc).ok is True


def test_error_code_values_are_upper_snake() -> None:
    for code in ErrorCode:
        assert code.value == code.name


def test_error_code_covers_transport_codes() -> None:
    """ADR 0002 决定 1：四个传输层码必须在枚举里，否则 404/405/429/500 无合规码可用。"""
    assert {
        "NOT_FOUND",
        "METHOD_NOT_ALLOWED",
        "RATE_LIMITED",
        "INTERNAL_ERROR",
    } <= {code.value for code in ErrorCode}
