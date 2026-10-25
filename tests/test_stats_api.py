"""`GET /stats`（SPEC §5.13 / D8）：与账本一致、通过率、错题分布、配额。

统计接口**不调模型**：`meta.usage` 是零值，且这里注入一个空脚本模型 ——
真被调用就会炸。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from conftest import create_business_tables
from fakes.llm import ScriptedChatModel
from fakes.sandbox import FakeSandboxRunner
from pyassist.api.main import create_app
from pyassist.config import settings
from pyassist.db import repositories
from pyassist.db.session import open_engine, open_session
from pyassist.llm.tracing import UsageSnapshot
from pyassist.services.usage import record_usage


def _seed(database_url: str) -> None:
    async def run() -> None:
        async with open_engine(database_url) as engine, open_session(engine) as session:
            await repositories.ensure_user(session, user_id="local")
            await repositories.create_chat_session(session, session_id="s1", user_id="local")
            await repositories.commit(session)

            for index, passed in enumerate([True, False, True]):
                await repositories.record_attempt(
                    session,
                    idempotency_key=f"attempt:{index}",
                    session_id="s1",
                    exercise_id=f"ex_{index}",
                    topic="list" if index != 1 else "dict",
                    submitted_code="code",
                    is_passed=passed,
                    score=100 if passed else 0,
                )
            await repositories.record_mistake(
                session,
                idempotency_key="mistake:1",
                user_id="local",
                topic="dict",
                exercise_id="ex_1",
            )
            await repositories.record_mistake(
                session,
                idempotency_key="mistake:2",
                user_id="local",
                topic="async",
                exercise_id="ex_2",
            )
            await repositories.mark_mistake_resolved(session, user_id="local", exercise_id="ex_2")
            await repositories.commit(session)

            await record_usage(
                session,
                request_id="req-1",
                session_id="s1",
                model_id="deepseek-v4-flash",
                usage=UsageSnapshot(
                    prompt_tokens=200,
                    completion_tokens=100,
                    cached_tokens=150,
                    cost_usd=Decimal("0.0012"),
                    is_off_peak=True,
                ),
            )
            await repositories.commit(session)

    asyncio.run(run())


@pytest.fixture
def stats_client(tmp_database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    create_business_tables(tmp_database_url)
    _seed(tmp_database_url)
    monkeypatch.setattr(settings, "daily_token_limit", 1_000)

    model = ScriptedChatModel()
    runner = FakeSandboxRunner()
    app = create_app(
        database_url=tmp_database_url,
        rate_limit_enabled=False,
        exercise_model=model,
        grading_model=model,
        sandbox_runner=runner,
        store=InMemoryStore(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_stats_reports_ledger_attempts_and_mistakes(stats_client: TestClient) -> None:
    response = stats_client.get("/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["today_prompt_tokens"] == 200
    assert data["today_completion_tokens"] == 100
    assert data["today_cached_tokens"] == 150
    assert data["today_cost_usd"] == 0.0012
    assert data["total_attempts"] == 3
    assert data["passed_attempts"] == 2
    assert data["pass_rate"] == 0.6667
    assert data["mistake_total"] == 2
    assert data["unresolved_mistakes"] == 1
    assert data["mistakes_by_topic"] == [
        {"topic": "async", "count": 1},
        {"topic": "dict", "count": 1},
    ]
    assert body["meta"]["usage"]["prompt_tokens"] == 0


def test_stats_reports_quota_window(stats_client: TestClient) -> None:
    data = stats_client.get("/stats").json()["data"]

    assert data["quota_limit"] == 1_000
    assert data["quota_used"] == 300
    assert data["quota_remaining"] == 700
    assert data["is_exhausted"] is False


def test_stats_without_attempts_has_zero_pass_rate(tmp_database_url: str) -> None:
    create_business_tables(tmp_database_url)
    app = create_app(database_url=tmp_database_url, rate_limit_enabled=False)

    with TestClient(app, raise_server_exceptions=False) as client:
        data = client.get("/stats").json()["data"]

    assert data["total_attempts"] == 0
    assert data["pass_rate"] == 0.0
    assert data["mistakes_by_topic"] == []
    assert data["quota_remaining"] is None or isinstance(data["quota_remaining"], int)


def test_stats_does_not_invoke_the_model(stats_client: TestClient) -> None:
    """只读三张表；脚本模型为空，被调用就会 AssertionError。"""
    stats_client.get("/stats")

    assert stats_client.app.state.exercise_model.structured_requests == []
