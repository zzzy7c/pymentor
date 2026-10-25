"""★ 唯一的配额检查与账本写入出口（SPEC §5.11 / D8）。

三件事只在这里做：判断今天还能不能花、把一次请求的用量记进账本、把客户端给的
`session_id` 归一到 `sessions.id` 的 32 字符。任何 router / service 都不得自己
读账本或拼 `append_token_usage`（守护 #43）。

**软上限**：并发请求可能同时通过检查，账本随后各记一笔 —— 这是 D8 接受的代价，
换来的是不维护第二份计数器。唯一真相始终是 `token_ledger` 表。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.config import DEFAULT_DAILY_TOKEN_LIMIT, MAX_SESSION_ID_CHARS
from pyassist.db.repositories import (
    append_token_usage,
    commit,
    create_chat_session,
    ensure_user,
    sum_tokens_since,
)
from pyassist.errors import QuotaExceeded
from pyassist.llm.tracing import UsageSnapshot
from pyassist.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """给 `/stats` 与日志看的配额快照。`limit=None` 表示熔断已关闭。"""

    limit: int | None
    used_today: int
    is_exhausted: bool


def resolve_limit(limit: int | None) -> int | None:
    """`None` → 默认上限；`<= 0` → 关闭（返回 None）。"""
    resolved = DEFAULT_DAILY_TOKEN_LIMIT if limit is None else limit
    return resolved if resolved > 0 else None


def utc_day_start(moment: datetime | None = None) -> datetime:
    now = moment or datetime.now(UTC)
    return datetime.combine(now.astimezone(UTC).date(), time.min, tzinfo=UTC)


def seconds_until_utc_midnight(moment: datetime | None = None) -> int:
    now = moment or datetime.now(UTC)
    tomorrow = utc_day_start(now) + timedelta(days=1)
    return max(1, int((tomorrow - now.astimezone(UTC)).total_seconds()))


async def assert_daily_quota(session: AsyncSession, *, user_id: str, limit: int | None) -> None:
    """超限抛 `QuotaExceeded`（429 + Retry-After）。关闭熔断时什么都不查。"""
    resolved = resolve_limit(limit)
    if resolved is None:
        return
    prompt_tokens, completion_tokens, _cached, _cost = await sum_tokens_since(
        session, user_id=user_id, since=utc_day_start()
    )
    used = prompt_tokens + completion_tokens
    if used >= resolved:
        logger.warning("usage.quota_exceeded", user_id=user_id, limit=resolved, used=used)
        raise QuotaExceeded(
            retry_after=seconds_until_utc_midnight(),
            limit=resolved,
            used=used,
        )


async def quota_status(session: AsyncSession, *, user_id: str, limit: int | None) -> QuotaStatus:
    resolved = resolve_limit(limit)
    prompt_tokens, completion_tokens, _cached, _cost = await sum_tokens_since(
        session, user_id=user_id, since=utc_day_start()
    )
    used = prompt_tokens + completion_tokens
    return QuotaStatus(
        limit=resolved,
        used_today=used,
        is_exhausted=resolved is not None and used >= resolved,
    )


async def record_usage(
    session: AsyncSession,
    *,
    request_id: str,
    session_id: str,
    model_id: str,
    usage: UsageSnapshot,
) -> bool:
    """记一次请求的用量。键 `req:{request_id}`：重放不会把成本记两遍。"""
    return await append_token_usage(
        session,
        idempotency_key=f"req:{request_id}",
        session_id=db_session_id(session_id),
        model_id=model_id,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_tokens=usage.cached_tokens,
        cost_usd=usage.cost_usd,
        is_off_peak=usage.is_off_peak,
    )


async def record_request_usage(
    session: AsyncSession,
    *,
    request_id: str,
    raw_session_id: str,
    user_id: str,
    model_id: str,
    usage: UsageSnapshot,
) -> bool:
    """路由的记账入口：建父行 → 写账本 → 提交（S6/S7）。

    `token_ledger.session_id` 是 `sessions.id` 的外键，匿名请求也必须先有会话行；
    先提交一次再把 CHECK 点写库隔开，避免业务事务占着 SQLite 写锁（同一原因见
    `services/exercise.py` 的提交注释）。
    """
    await ensure_user(session, user_id=user_id)
    await create_chat_session(session, session_id=db_session_id(raw_session_id), user_id=user_id)
    await commit(session)
    recorded = await record_usage(
        session,
        request_id=request_id,
        session_id=raw_session_id,
        model_id=model_id,
        usage=usage,
    )
    await commit(session)
    return recorded


def db_session_id(raw: str) -> str:
    """D9：`sessions.id` 是 32 字符，契约允许 64。**绝不为这件事改表结构。**

    合法（非空且 ≤32）原样用；否则 `"s" + sha256(raw)[:31]`。纯函数、确定性 ——
    同一个客户端 id 每次映射到同一行。
    """
    candidate = raw.strip()
    if candidate and len(candidate) <= MAX_SESSION_ID_CHARS:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    return "s" + digest[: MAX_SESSION_ID_CHARS - 1]


__all__ = [
    "QuotaStatus",
    "assert_daily_quota",
    "db_session_id",
    "quota_status",
    "record_request_usage",
    "record_usage",
    "resolve_limit",
    "seconds_until_utc_midnight",
    "utc_day_start",
]
