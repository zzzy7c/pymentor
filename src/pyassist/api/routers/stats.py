"""`GET /stats`（SPEC §5.13）：只读账本与业务表，**不调模型**。

因此这个 router **不挂 `require_quota`**（D8）。`meta.usage` 是零值 ——
统计接口自己不消耗 Token。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.api.deps import build_meta, get_session
from pyassist.api.schemas import Envelope, StatsData, TopicCount, envelope_success
from pyassist.config import DEFAULT_USER_ID, settings
from pyassist.db.repositories import get_stats
from pyassist.services.usage import quota_status, utc_day_start

router = APIRouter()


@router.get("/stats", response_model=Envelope[StatsData])
async def stats(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_id: str = Query(default=DEFAULT_USER_ID, max_length=64),
) -> Envelope[StatsData]:
    aggregate = await get_stats(session, user_id=user_id, since=utc_day_start())
    quota = await quota_status(session, user_id=user_id, limit=settings.daily_token_limit)
    pass_rate = (
        round(aggregate.passed_attempts / aggregate.total_attempts, 4)
        if aggregate.total_attempts
        else 0.0
    )
    remaining = None if quota.limit is None else max(0, quota.limit - quota.used_today)
    data = StatsData(
        today_prompt_tokens=aggregate.today_prompt_tokens,
        today_completion_tokens=aggregate.today_completion_tokens,
        today_cached_tokens=aggregate.today_cached_tokens,
        today_cost_usd=aggregate.today_cost_usd,
        total_attempts=aggregate.total_attempts,
        passed_attempts=aggregate.passed_attempts,
        pass_rate=pass_rate,
        mistake_total=aggregate.mistake_total,
        unresolved_mistakes=aggregate.unresolved_mistakes,
        mistakes_by_topic=[
            TopicCount(topic=item.topic, count=item.count) for item in aggregate.mistakes_by_topic
        ],
        quota_limit=quota.limit,
        quota_used=quota.used_today,
        quota_remaining=remaining,
        is_exhausted=quota.is_exhausted,
    )
    return envelope_success(data, meta=build_meta(request))
