"""v2 错误诊断的**唯一业务层**（SPEC §5.10）。只做业务，不 import fastapi。

`run_diagnosis` 是 `ainvoke` + 从检查点读终态：**不是**流式路径。
`/diagnose` 是非流式信封接口（D8）；用户代码的执行结果就是最终产物，
边跑边吐没有意义，反而让 200/422 的状态码在流开始后无法回退。

`AppError` **直接冒泡** —— 错误 → 状态码只有 `api/errors.py` 一个翻译点。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from pyassist.llm.tracing import UsageSnapshot
from pyassist.logging import get_logger
from pyassist.rag.citations import Citation
from pyassist.sandbox.runner import SandboxResult

logger = get_logger(__name__)

DiagnoseStatus = Literal["ok", "error", "timeout", "rejected", "unverified"]

_ZERO_USAGE = UsageSnapshot(
    prompt_tokens=0,
    completion_tokens=0,
    cached_tokens=0,
    cost_usd=Decimal(0),
    is_off_peak=True,
)


@dataclass(frozen=True, slots=True)
class DiagnoseResult:
    """非流式路径的返回值。`status="unverified"` = 没有任何沙箱结果。"""

    status: DiagnoseStatus
    error_type: str | None
    explanation: str
    fixed_code: str | None
    change_summary: list[str]
    verification: SandboxResult | None
    citations: list[Citation]
    usage: UsageSnapshot


async def run_diagnosis(
    *,
    error_text: str,
    code: str | None,
    graph: CompiledStateGraph[Any, Any, Any, Any],
    thread_id: str,
    request_id: str,
) -> DiagnoseResult:
    """跑图并从检查点取终态（AGENTS.md §2：检查点是对话状态的唯一真相）。

    `request_id` 只进日志，不进图状态 —— 它每次请求都不同，进了状态就是
    一个**随请求变化的检查点字段**，对恢复与缓存都没有好处。
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    started = time.perf_counter()
    await graph.ainvoke({"error_text": error_text, "broken_code": code}, config)
    snapshot = await graph.aget_state(config)
    values = dict(snapshot.values)

    verification = values.get("last_verification")
    if not isinstance(verification, SandboxResult):
        verification = None
    resolved_status: DiagnoseStatus = "unverified" if verification is None else verification.status

    citations = values.get("citations")
    explanation = values.get("explanation")
    fixed_code = values.get("fixed_code")
    error_type = values.get("error_type")
    change_summary = values.get("change_summary")
    usage = values.get("usage")

    result = DiagnoseResult(
        status=resolved_status,
        error_type=error_type if isinstance(error_type, str) else None,
        explanation=explanation if isinstance(explanation, str) else "",
        fixed_code=fixed_code if isinstance(fixed_code, str) else None,
        change_summary=(
            [item for item in change_summary if isinstance(item, str)]
            if isinstance(change_summary, list)
            else []
        ),
        verification=verification,
        citations=(
            [item for item in citations if isinstance(item, Citation)]
            if isinstance(citations, list)
            else []
        ),
        usage=usage if isinstance(usage, UsageSnapshot) else _ZERO_USAGE,
    )
    logger.info(
        "diagnose.done",
        request_id=request_id,
        thread_id=thread_id,
        status=result.status,
        repair_attempts=_as_int(values.get("repair_attempts")),
        citations=len(result.citations),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
