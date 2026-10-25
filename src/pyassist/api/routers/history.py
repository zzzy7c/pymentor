"""`GET /history`（SPEC §5.13）：游标分页，刷新后仍能回放。

**不挂 `require_quota`**：只读业务表，不调模型（D8）。`cursor` 是不透明字符串
`"{created_at.isoformat()}|{id}"`，排序 `(created_at DESC, id DESC)` —— 同一时刻
靠 id 兜底，否则翻页会重复或漏行。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.api.deps import build_meta, get_session
from pyassist.api.schemas import Envelope, HistoryItem, Page, envelope_success
from pyassist.config import (
    DEFAULT_USER_ID,
    HISTORY_PAGE_SIZE,
    MAX_HISTORY_PAGE_SIZE,
)
from pyassist.db.models import ExerciseAttempt
from pyassist.db.repositories import count_attempts_for, list_attempts_page
from pyassist.errors import ValidationError

router = APIRouter()


@router.get("/history", response_model=Envelope[Page[HistoryItem]])
async def history(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    session_id: str | None = Query(default=None, max_length=64),
    user_id: str = Query(default=DEFAULT_USER_ID, max_length=64),
    limit: int = Query(default=HISTORY_PAGE_SIZE, ge=1, le=MAX_HISTORY_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=128),
) -> Envelope[Page[HistoryItem]]:
    parsed = _parse_cursor(cursor)
    rows, has_more = await list_attempts_page(
        session,
        user_id=user_id,
        session_id=session_id,
        limit=limit,
        cursor=parsed,
    )
    total = await count_attempts_for(session, user_id=user_id, session_id=session_id)
    items = [_to_item(row) for row in rows]
    next_cursor = _encode_cursor(rows[-1]) if has_more and rows else None
    return envelope_success(
        Page[HistoryItem](items=items, total=total, next_cursor=next_cursor),
        meta=build_meta(request),
    )


def _to_item(row: ExerciseAttempt) -> HistoryItem:
    return HistoryItem(
        attempt_id=str(row.id),
        exercise_id=row.exercise_id,
        topic=str(row.topic),
        is_passed=bool(row.is_passed),
        score=row.score,
        submitted_code=row.submitted_code,
        stdout=row.stdout,
        stderr=row.stderr,
        duration_ms=row.duration_ms,
        created_at=row.created_at.isoformat(),
    )


def _encode_cursor(row: ExerciseAttempt) -> str:
    return f"{row.created_at.isoformat()}|{row.id}"


def _parse_cursor(raw: str | None) -> tuple[datetime, str] | None:
    """非法 cursor → 422 真信封。**不静默退回第一页**：那会让客户端无限循环。"""
    if raw is None:
        return None
    timestamp, separator, row_id = raw.rpartition("|")
    if not separator or not row_id:
        raise ValidationError("cursor 不合法", details={"cursor": raw})
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValidationError("cursor 不合法", details={"cursor": raw}) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed, row_id


__all__ = ["router"]
