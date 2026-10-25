"""v3 HTTP 契约（SPEC §5.12 / §5.13）：信封、不泄露测试、404、配额。

模型与沙箱都是替身；数据库是临时 SQLite。这里只看**线上形状**——
节点的业务判断已经由 `test_graph_exercise.py` 钉住。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from fakes.llm import ScriptedChatModel
from fakes.sandbox import FakeSandboxRunner, result_for
from pyassist.agent.tools.grading import ExerciseDraft, GradeDraft
from pyassist.agent.tools.grading import TestCaseDraft as DraftTest
from pyassist.api.main import create_app
from pyassist.config import TEST_RESULT_MARKER, settings
from pyassist.db import models, repositories
from pyassist.db.session import open_engine, open_session
from pyassist.llm.tracing import UsageSnapshot
from pyassist.rag.retriever import RetrievedChunk
from pyassist.services.usage import record_usage


def _create_tables(database_url: str) -> None:
    """生产建表走迁移；测试在**启动 app 之前**把同一份 metadata 建出来。"""

    async def run() -> None:
        async with open_engine(database_url) as engine, engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

    asyncio.run(run())


def _seed_today_usage(database_url: str, tokens: int) -> None:
    """给默认用户记一笔当日用量，让配额闸门在下一次请求处触发。"""

    async def run() -> None:
        async with open_engine(database_url) as engine, open_session(engine) as session:
            await repositories.ensure_user(session, user_id="local")
            await repositories.create_chat_session(session, session_id="seed", user_id="local")
            await repositories.commit(session)
            await record_usage(
                session,
                request_id="seed-request",
                session_id="seed",
                model_id="deepseek-v4-flash",
                usage=UsageSnapshot(
                    prompt_tokens=tokens,
                    completion_tokens=0,
                    cached_tokens=0,
                    cost_usd=Decimal(0),
                    is_off_peak=True,
                ),
            )
            await repositories.commit(session)

    asyncio.run(run())


def _draft() -> ExerciseDraft:
    return ExerciseDraft(
        title="列表求和",
        prompt="实现 add",
        starter_code="def add(left, right):\n    ...\n",
        reference_solution="def add(left, right):\n    return left + right\n",
        tests=[
            DraftTest(
                name="test_add",
                source="def test_add(self):\n    self.assertEqual(add(1, 2), 3)",
            )
        ],
    )


def _stdout(*, passed: int, failed: int = 0) -> str:
    records: list[dict[str, Any]] = [
        {"name": f"test_p{index}", "status": "passed", "message": "", "duration_ms": 1}
        for index in range(passed)
    ]
    records.extend(
        {"name": f"test_f{index}", "status": "failed", "message": "boom", "duration_ms": 1}
        for index in range(failed)
    )
    return TEST_RESULT_MARKER + json.dumps(
        {"total": passed + failed, "records": records, "duration_ms": 2}
    )


async def _search(query: str, top_k: int) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id="lists#append",
            text="list.append 在末尾追加一个元素。",
            doc_url="https://docs.python.org/3.12/tutorial/datastructures.html",
            source_url="https://docs.python.org/3.12/tutorial/datastructures.html#lists",
            title="Data Structures",
            section="More on Lists",
            score=0.9,
        )
    ]


@pytest.fixture
def exercise_app(
    tmp_database_url: str,
    *,
    drafts: list[ExerciseDraft] | None = None,
    grades: list[GradeDraft] | None = None,
    results: list[Any] | None = None,
) -> Iterator[TestClient]:
    """默认一题、一次提交的脚本；需要更多轮时用例自己传参。"""
    _create_tables(tmp_database_url)
    generation = ScriptedChatModel(structured_script=list(drafts or [_draft()]))
    grading = ScriptedChatModel(structured_script=list(grades or [GradeDraft(summary="失败")]))
    runner = FakeSandboxRunner(
        list(
            results
            or [
                result_for("", stdout=_stdout(passed=1)),  # 生成自校验
                result_for("", stdout=_stdout(passed=1)),  # 一次提交
            ]
        )
    )
    app = create_app(
        database_url=tmp_database_url,
        rate_limit_enabled=False,
        exercise_model=generation,
        grading_model=grading,
        sandbox_runner=runner,
        search=_search,
        store=InMemoryStore(),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_create_exercise_returns_envelope_without_tests(exercise_app: TestClient) -> None:
    response = exercise_app.post("/exercises", json={"topic": "list"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["meta"]["request_id"]
    assert body["meta"]["usage"]["prompt_tokens"] == 40
    data = body["data"]
    assert set(data) == {"exercise_id", "topic", "title", "prompt", "starter_code"}
    assert data["exercise_id"].startswith("ex_")
    assert data["topic"] == "list"
    # D2 / 守护 #39：测试与参考答案绝不下发
    assert "tests" not in data
    assert "reference_solution" not in data


def test_create_exercise_rejects_unknown_topic(exercise_app: TestClient) -> None:
    response = exercise_app.post("/exercises", json={"topic": "numpy"})

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_submit_unknown_exercise_returns_404_envelope(exercise_app: TestClient) -> None:
    response = exercise_app.post("/exercises/ex_missing/submit", json={"code": "x = 1"})

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["meta"]["usage"]["prompt_tokens"] == 0


def test_submit_returns_report_feedback_and_citations(
    tmp_database_url: str,
) -> None:
    _create_tables(tmp_database_url)
    generation = ScriptedChatModel(structured_script=[_draft()])
    grading = ScriptedChatModel(
        structured_script=[GradeDraft(summary="有一条用例失败", doc_queries=["list append"])]
    )
    runner = FakeSandboxRunner(
        [
            result_for("", stdout=_stdout(passed=1)),  # 自校验
            result_for("", stdout=_stdout(passed=0, failed=1), stderr="boom"),  # 提交
        ]
    )
    app = create_app(
        database_url=tmp_database_url,
        rate_limit_enabled=False,
        exercise_model=generation,
        grading_model=grading,
        sandbox_runner=runner,
        search=_search,
        store=InMemoryStore(),
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        created = client.post("/exercises", json={"topic": "list"}).json()
        exercise_id = created["data"]["exercise_id"]
        response = client.post(
            f"/exercises/{exercise_id}/submit",
            json={"code": "def add(a, b):\n    return a\n", "session_id": "sess-api"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["exercise_id"] == exercise_id
    assert data["status"] == "failed"
    assert data["is_passed"] is False
    assert data["score"] == 0
    assert data["tests"][0]["status"] == "failed"
    assert data["stderr"] == "boom"
    assert data["summary"] == "有一条用例失败"
    assert data["citations"][0]["url"].endswith("#lists")
    assert body["meta"]["usage"]["prompt_tokens"] == 40  # 批改那一轮


def test_exhausted_quota_returns_429_before_calling_the_model(
    tmp_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D8：超限时 429 + Retry-After，且**不再调模型**（脚本为空，一调就炸）。"""
    create_app_database = tmp_database_url
    _create_tables(create_app_database)
    _seed_today_usage(create_app_database, tokens=100)
    monkeypatch.setattr(settings, "daily_token_limit", 100)

    generation = ScriptedChatModel()  # 没有脚本：任何调用都会 AssertionError
    runner = FakeSandboxRunner()
    app = create_app(
        database_url=create_app_database,
        rate_limit_enabled=False,
        exercise_model=generation,
        grading_model=generation,
        sandbox_runner=runner,
        store=InMemoryStore(),
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/exercises", json={"topic": "list"})

    assert response.status_code == 429
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["details"]["limit"] == 100
    assert int(response.headers["Retry-After"]) >= 1
    assert generation.structured_requests == []
    assert runner.calls == []


def test_submit_does_not_expose_hidden_tests(exercise_app: TestClient) -> None:
    created = exercise_app.post("/exercises", json={"topic": "list"}).json()
    response = exercise_app.post(
        f"/exercises/{created['data']['exercise_id']}/submit",
        json={"code": "def add(a, b):\n    return a + b\n"},
    )

    body = response.json()
    assert response.status_code == 200
    text = response.text
    # 题目自带的测试源码（函数体）没有出现在响应里
    assert "assertEqual(add(1, 2), 3)" not in text
    assert "reference_solution" not in body["data"]
