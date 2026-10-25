"""`POST /diagnose` —— v2 错误诊断（非流式，SPEC D8）。

本文件**只做协议翻译**：`DiagnoseResult` → `Envelope[DiagnosisData]`。
业务判断在 `services/diagnose.py`，错误 → 状态码在 `api/errors.py`（§2 单点所有权）。

空检索不在这里特判：`RetrievalEmpty` 由 `api/errors.py` 映射成
200 + `ok=true` + `data={"status": "empty", "query": ...}`（§3.2）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.api.deps import (
    build_meta,
    get_diagnose_graph,
    get_request_context,
    get_sandbox,
    get_session,
    require_quota,
)
from pyassist.api.schemas import (
    CitationData,
    DiagnoseRequest,
    DiagnosisData,
    Envelope,
    SandboxData,
    envelope_success,
    usage_to_schema,
)
from pyassist.config import DEFAULT_USER_ID
from pyassist.services.diagnose import DiagnoseResult, run_diagnosis
from pyassist.services.usage import record_request_usage

router = APIRouter(dependencies=[Depends(require_quota)])


@router.post("/diagnose", response_model=Envelope[DiagnosisData])
async def diagnose(
    request: Request,
    body: DiagnoseRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[DiagnosisData]:
    """贴报错 → 检索 → 解释 → 修复 → 沙箱验证。"""
    dg = get_diagnose_graph(request)
    get_sandbox(request)  # 沙箱不可用 → ConfigError → 500（快速失败，别等模型花完钱）
    await dg.preflight()  # 与 qa 同一套廉价预检
    request_id = get_request_context(request).request_id
    thread_id = _thread_id(request, body)
    result = await run_diagnosis(
        error_text=body.error_text,
        code=body.code,
        graph=dg.graph,
        thread_id=thread_id,
        request_id=request_id,
    )
    await record_request_usage(
        session,
        request_id=request_id,
        raw_session_id=thread_id,
        user_id=DEFAULT_USER_ID,
        model_id=dg.model_id,
        usage=result.usage,
    )
    return envelope_success(
        _to_data(result), meta=build_meta(request, usage=usage_to_schema(result.usage))
    )


def _thread_id(request: Request, body: DiagnoseRequest) -> str:
    """与 `knowledge.py` 逐字同构：`session_id` 缺省用 `request_id`。

    不在 router 之间共享这个函数：共享会让它搬进 `services/`，
    而 `services/` import fastapi 是分层倒挂（§5.12）。
    """
    ctx = get_request_context(request)
    return body.session_id or ctx.request_id


def _to_data(result: DiagnoseResult) -> DiagnosisData:
    """`DiagnoseResult` → 响应契约。引用按原序，编号就是 `Citation.index`。"""
    return DiagnosisData(
        status=result.status,
        error_type=result.error_type,
        explanation=result.explanation,
        fixed_code=result.fixed_code,
        change_summary=result.change_summary,
        verification=_verification(result),
        citations=[
            CitationData(
                index=citation.index,
                title=citation.title,
                section=citation.section,
                url=citation.url,
                snippet=citation.snippet,
            )
            for citation in result.citations
        ],
    )


def _verification(result: DiagnoseResult) -> SandboxData | None:
    sandbox = result.verification
    if sandbox is None:
        return None
    return SandboxData(
        status=sandbox.status,
        exit_code=sandbox.exit_code,
        stdout=sandbox.stdout,
        stderr=sandbox.stderr,
        duration_ms=sandbox.duration_ms,
        is_truncated=sandbox.is_truncated,
    )
