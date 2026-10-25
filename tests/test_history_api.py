"""`GET /history`（SPEC §5.13）：游标分页稳定、刷新可回放、非法游标 422。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import create_business_tables
from pyassist.api.main import create_app
from pyassist.db import models, repositories
from pyassist.db.session import open_engine, open_session

_BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _seed(database_url: str, *, count: int = 3) -> None:
    async def run() -> None:
        async with open_engine(database_url) as engine, open_session(engine) as session:
            await repositories.ensure_user(session, user_id="local")
            await repositories.create_chat_session(session, session_id="s1", user_id="local")
            await repositories.commit(session)
            for index in range(count):
                session.add(
                    models.ExerciseAttempt(
                        id=f"a{index:03d}",
                        session_id="s1",
                        exercise_id=f"ex_{index}",
                        topic="list",
                        is_passed=index % 2 == 0,
                        score=100 if index % 2 == 0 else 0,
                        submitted_code=f"code-{index}",
                        stdout="ok",
                        stderr=None,
                        duration_ms=index,
                        idempotency_key=f"attempt:{index}",
                        created_at=_BASE + timedelta(seconds=index),
                    )
                )
            await session.commit()

    asyncio.run(run())


@pytest.fixture
def history_client(tmp_database_url: str) -> Iterator[TestClient]:
    create_business_tables(tmp_database_url)
    _seed(tmp_database_url)
    app = create_app(database_url=tmp_database_url, rate_limit_enabled=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_history_returns_page_envelope_with_total(history_client: TestClient) -> None:
    response = history_client.get("/history", params={"session_id": "s1", "limit": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert data["next_cursor"]
    assert [item["attempt_id"] for item in data["items"]] == ["a002", "a001"]
    item = data["items"][0]
    assert set(item) == {
        "attempt_id",
        "exercise_id",
        "topic",
        "is_passed",
        "score",
        "submitted_code",
        "stdout",
        "stderr",
        "duration_ms",
        "created_at",
    }


def test_history_cursor_continues_without_duplicates(history_client: TestClient) -> None:
    first = history_client.get("/history", params={"session_id": "s1", "limit": 2}).json()
    second = history_client.get(
        "/history",
        params={
            "session_id": "s1",
            "limit": 2,
            "cursor": first["data"]["next_cursor"],
        },
    ).json()

    assert [item["attempt_id"] for item in second["data"]["items"]] == ["a000"]
    assert second["data"]["next_cursor"] is None
    assert second["data"]["total"] == 3


def test_history_filters_by_user(tmp_database_url: str) -> None:
    create_business_tables(tmp_database_url)
    _seed(tmp_database_url)
    app = create_app(database_url=tmp_database_url, rate_limit_enabled=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/history", params={"user_id": "nobody"})

    body = response.json()
    assert body["data"]["items"] == []
    assert body["data"]["total"] == 0


def test_history_rejects_malformed_cursor(history_client: TestClient) -> None:
    response = history_client.get("/history", params={"cursor": "not-a-cursor"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
