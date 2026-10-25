"""配额与账本（SPEC §5.11 / D8 / D9）：429、Retry-After、幂等、归一化 session_id。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from pyassist.config import MAX_SESSION_ID_CHARS
from pyassist.db import models, repositories
from pyassist.db.session import open_engine, open_session
from pyassist.errors import QuotaExceeded
from pyassist.llm.tracing import UsageSnapshot
from pyassist.services.usage import (
    assert_daily_quota,
    db_session_id,
    quota_status,
    record_usage,
    resolve_limit,
    seconds_until_utc_midnight,
    utc_day_start,
)


def _usage(prompt: int, completion: int = 0) -> UsageSnapshot:
    return UsageSnapshot(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=0,
        cost_usd=Decimal("0.0001"),
        is_off_peak=True,
    )


@pytest.fixture
async def engine(tmp_database_url: str) -> AsyncIterator[AsyncEngine]:
    async with open_engine(tmp_database_url) as opened:
        async with opened.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        yield opened


async def _seed_session(
    engine: AsyncEngine, *, user_id: str = "u1", session_id: str = "s1"
) -> None:
    async with open_session(engine) as session:
        await repositories.ensure_user(session, user_id=user_id)
        await repositories.create_chat_session(session, session_id=session_id, user_id=user_id)
        await repositories.commit(session)


# ---------- 纯函数 ----------


def test_resolve_limit_maps_none_to_default_and_zero_to_disabled() -> None:
    assert resolve_limit(None) == 500_000
    assert resolve_limit(0) is None
    assert resolve_limit(-1) is None
    assert resolve_limit(123) == 123


def test_utc_day_start_is_midnight_utc() -> None:
    moment = datetime(2026, 10, 22, 15, 30, tzinfo=UTC)

    assert utc_day_start(moment) == datetime(2026, 10, 22, tzinfo=UTC)


def test_seconds_until_utc_midnight_is_never_zero() -> None:
    assert seconds_until_utc_midnight(datetime(2026, 10, 22, 23, 59, tzinfo=UTC)) == 60
    assert seconds_until_utc_midnight(datetime(2026, 10, 22, 23, 59, 59, tzinfo=UTC)) >= 1


def test_db_session_id_keeps_short_ids_verbatim() -> None:
    assert db_session_id("sess-1") == "sess-1"


def test_db_session_id_hashes_overlong_and_empty_ids() -> None:
    long_id = "x" * (MAX_SESSION_ID_CHARS + 1)

    hashed = db_session_id(long_id)

    assert len(hashed) == MAX_SESSION_ID_CHARS
    assert hashed.startswith("s")
    assert hashed != long_id
    assert db_session_id(long_id) == hashed  # 确定性
    assert len(db_session_id("")) == MAX_SESSION_ID_CHARS


# ---------- 配额与账本 ----------


async def test_quota_exceeded_raises_with_retry_after(engine: AsyncEngine) -> None:
    await _seed_session(engine)

    async with open_session(engine) as session:
        await record_usage(
            session,
            request_id="r1",
            session_id="s1",
            model_id="deepseek-v4-flash",
            usage=_usage(prompt=60, completion=40),
        )
        await repositories.commit(session)

        with pytest.raises(QuotaExceeded) as raised:
            await assert_daily_quota(session, user_id="u1", limit=100)

    assert raised.value.details == {"limit": 100, "used": 100}
    assert raised.value.retry_after >= 1


async def test_quota_below_limit_passes(engine: AsyncEngine) -> None:
    await _seed_session(engine)

    async with open_session(engine) as session:
        await record_usage(
            session,
            request_id="r1",
            session_id="s1",
            model_id="deepseek-v4-flash",
            usage=_usage(prompt=10),
        )
        await repositories.commit(session)

        await assert_daily_quota(session, user_id="u1", limit=100)  # 不抛


async def test_disabled_limit_skips_the_check(engine: AsyncEngine) -> None:
    await _seed_session(engine)

    async with open_session(engine) as session:
        await record_usage(
            session,
            request_id="r1",
            session_id="s1",
            model_id="deepseek-v4-flash",
            usage=_usage(prompt=10_000_000),
        )
        await repositories.commit(session)

        await assert_daily_quota(session, user_id="u1", limit=0)


async def test_record_usage_is_idempotent_per_request_id(engine: AsyncEngine) -> None:
    await _seed_session(engine)

    async with open_session(engine) as session:
        first = await record_usage(
            session,
            request_id="req-1",
            session_id="s1",
            model_id="deepseek-v4-flash",
            usage=_usage(prompt=40, completion=20),
        )
        second = await record_usage(
            session,
            request_id="req-1",
            session_id="s1",
            model_id="deepseek-v4-flash",
            usage=_usage(prompt=40, completion=20),
        )
        await repositories.commit(session)
        usage = await repositories.sum_tokens_since(session, user_id="u1", since=utc_day_start())

    assert first is True
    assert second is False
    assert usage[0] == 40
    assert usage[1] == 20


async def test_record_usage_stores_cached_tokens_separately(engine: AsyncEngine) -> None:
    """§3.1：缓存命中必须单独记，否则成本报表会严重偏低。"""
    await _seed_session(engine)

    async with open_session(engine) as session:
        await record_usage(
            session,
            request_id="req-cache",
            session_id="s1",
            model_id="deepseek-v4-flash",
            usage=UsageSnapshot(
                prompt_tokens=1204,
                completion_tokens=388,
                cached_tokens=1024,
                cost_usd=Decimal("0.0004"),
                is_off_peak=True,
            ),
        )
        await repositories.commit(session)
        prompt, completion, cached, cost = await repositories.sum_tokens_since(
            session, user_id="u1", since=utc_day_start()
        )

    assert (prompt, completion, cached) == (1204, 388, 1024)
    assert cost == Decimal("0.0004")


async def test_quota_counts_only_the_given_user(engine: AsyncEngine) -> None:
    await _seed_session(engine, user_id="u1", session_id="s1")
    await _seed_session(engine, user_id="u2", session_id="s2")

    async with open_session(engine) as session:
        await record_usage(
            session,
            request_id="r1",
            session_id="s1",
            model_id="deepseek-v4-flash",
            usage=_usage(prompt=100),
        )
        await record_usage(
            session,
            request_id="r2",
            session_id="s2",
            model_id="deepseek-v4-flash",
            usage=_usage(prompt=5),
        )
        await repositories.commit(session)

        await assert_daily_quota(session, user_id="u2", limit=100)
        with pytest.raises(QuotaExceeded):
            await assert_daily_quota(session, user_id="u1", limit=100)


async def test_quota_status_reports_limit_and_usage(engine: AsyncEngine) -> None:
    await _seed_session(engine)

    async with open_session(engine) as session:
        await record_usage(
            session,
            request_id="r1",
            session_id="s1",
            model_id="deepseek-v4-flash",
            usage=_usage(prompt=30, completion=20),
        )
        await repositories.commit(session)

        status = await quota_status(session, user_id="u1", limit=100)

    assert status.limit == 100
    assert status.used_today == 50
    assert status.is_exhausted is False


async def test_quota_status_marks_exhausted(engine: AsyncEngine) -> None:
    await _seed_session(engine)

    async with open_session(engine) as session:
        await record_usage(
            session,
            request_id="r1",
            session_id="s1",
            model_id="deepseek-v4-flash",
            usage=_usage(prompt=100),
        )
        await repositories.commit(session)

        status = await quota_status(session, user_id="u1", limit=100)

    assert status.is_exhausted is True
