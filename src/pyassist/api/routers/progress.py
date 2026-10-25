"""`POST /recommend`（SPEC §5.13 / D7）：按错题推荐下一步。

本文件只做协议翻译。候选集、校验、引用都在服务层与图里；
`require_quota` 挂在 router 上（会调模型，D8）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.api.deps import (
    build_meta,
    get_progress_graph,
    get_request_context,
    get_session,
    get_store,
    require_quota,
)
from pyassist.api.schemas import (
    CitationData,
    Envelope,
    RecommendationData,
    RecommendRequest,
    envelope_success,
    usage_to_schema,
)
from pyassist.config import DEFAULT_USER_ID
from pyassist.services.progress import Recommendation, recommend_for_user
from pyassist.services.usage import record_request_usage

router = APIRouter(dependencies=[Depends(require_quota)])


@router.post("/recommend", response_model=Envelope[RecommendationData])
async def recommend(
    request: Request,
    body: RecommendRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[RecommendationData]:
    """读该用户的未解决错题 → 结构化推荐 → 校验引用 id → 生成文档链接。"""
    graph = get_progress_graph(request)
    # 与 qa/diagnose 同一套廉价预检：索引不可用时在路由层就失败，
    # 不会让模型先花完钱再在图里撞 `CorpusMissing`。
    await graph.preflight()
    ctx = get_request_context(request)
    result = await recommend_for_user(
        user_id=DEFAULT_USER_ID,
        graph=graph,
        store=get_store(request),
        session=session,
        request_id=ctx.request_id,
    )
    await record_request_usage(
        session,
        request_id=ctx.request_id,
        raw_session_id=body.session_id or ctx.request_id,
        user_id=DEFAULT_USER_ID,
        model_id=graph.model_id,
        usage=result.usage,
    )
    return envelope_success(
        _to_data(result), meta=build_meta(request, usage=usage_to_schema(result.usage))
    )


def _to_data(result: Recommendation) -> RecommendationData:
    return RecommendationData(
        topic=result.topic,
        reason=result.reason,
        exercise_ids=result.exercise_ids,
        next_steps=result.next_steps,
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
