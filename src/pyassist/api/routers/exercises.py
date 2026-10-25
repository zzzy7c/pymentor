"""v3 练习的路由（SPEC §5.13）：出题与提交。

本文件**只做协议翻译**：服务层的 dataclass → 契约模型。业务判断在
`services/exercise.py`，错误 → 状态码只在 `api/errors.py`。

`require_quota` 挂在 router 上：两个端点都会调用模型，新端点自动继承（D8）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.api.deps import (
    build_meta,
    get_exercise_graphs,
    get_request_context,
    get_session,
    get_store,
    require_quota,
)
from pyassist.api.schemas import (
    CitationData,
    Envelope,
    ExerciseData,
    ExerciseRequest,
    FeedbackItem,
    SubmissionData,
    SubmitRequest,
    TestOutcomeData,
    envelope_success,
    usage_to_schema,
)
from pyassist.config import DEFAULT_USER_ID
from pyassist.services.exercise import SubmissionResult
from pyassist.services.exercise import create_exercise as create_exercise_service
from pyassist.services.exercise import submit_exercise as submit_exercise_service
from pyassist.services.usage import record_request_usage

router = APIRouter(dependencies=[Depends(require_quota)])


@router.post("/exercises", response_model=Envelope[ExerciseData])
async def create_exercise_endpoint(
    request: Request,
    body: ExerciseRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[ExerciseData]:
    """按主题出一题，并已在沙箱里对参考答案自校验通过。"""
    graphs = get_exercise_graphs(request)
    ctx = get_request_context(request)
    # exercise_id 就是生成图的 thread_id（D2）：题目身份不另立一套。
    exercise_id = f"ex_{uuid.uuid4().hex}"
    result = await create_exercise_service(
        topic=body.topic,
        graph=graphs.generation,
        exercise_id=exercise_id,
        request_id=ctx.request_id,
    )
    await record_request_usage(
        session,
        request_id=ctx.request_id,
        raw_session_id=body.session_id or ctx.request_id,
        user_id=DEFAULT_USER_ID,
        model_id=graphs.model_id,
        usage=result.usage,
    )
    exercise = result.exercise
    data = ExerciseData(
        exercise_id=exercise.exercise_id,
        topic=exercise.topic,
        title=exercise.title,
        prompt=exercise.prompt,
        starter_code=exercise.starter_code,
    )
    return envelope_success(data, meta=build_meta(request, usage=usage_to_schema(result.usage)))


@router.post("/exercises/{exercise_id}/submit", response_model=Envelope[SubmissionData])
async def submit_exercise_endpoint(
    request: Request,
    exercise_id: str,
    body: SubmitRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[SubmissionData]:
    """跑用户代码的测试并批改。通过与否**只来自沙箱报告**（D5）。"""
    graphs = get_exercise_graphs(request)
    ctx = get_request_context(request)
    result = await submit_exercise_service(
        exercise_id=exercise_id,
        code=body.code,
        graphs=graphs,
        session=session,
        user_id=DEFAULT_USER_ID,
        session_id=body.session_id or ctx.request_id,
        request_id=ctx.request_id,
        store=get_store(request),
    )
    await record_request_usage(
        session,
        request_id=ctx.request_id,
        raw_session_id=body.session_id or ctx.request_id,
        user_id=DEFAULT_USER_ID,
        model_id=graphs.grading_model_id,
        usage=result.usage,
    )
    return envelope_success(
        _to_submission_data(result),
        meta=build_meta(request, usage=usage_to_schema(result.usage)),
    )


def _to_submission_data(result: SubmissionResult) -> SubmissionData:
    return SubmissionData(
        exercise_id=result.exercise_id,
        status="passed" if result.is_passed else "failed",
        is_passed=result.is_passed,
        score=result.score,
        tests=[
            TestOutcomeData(
                name=outcome.name,
                status=outcome.status,
                message=outcome.message,
                duration_ms=outcome.duration_ms,
            )
            for outcome in result.report.outcomes
        ],
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
        summary=result.summary,
        feedback=[
            FeedbackItem(where=issue.where, why=issue.why, hint=issue.hint)
            for issue in result.issues
        ],
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
