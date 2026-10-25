"""`POST /recommend`（SPEC §5.13 / D7）：信封、无错题时的确定性推荐、id 校验。

模型与检索都是替身；数据库是临时 SQLite。这里只看**线上形状**，
图的控制流已由 `test_progress_graph.py` 钉住。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore

from conftest import create_business_tables
from fakes.llm import ScriptedChatModel
from pyassist.agent.tools.progress import NextStepDraft
from pyassist.api.main import create_app
from pyassist.db import repositories
from pyassist.db.session import open_engine, open_session
from pyassist.rag.retriever import RetrievedChunk

_DOC_URL = "https://docs.python.org/3.12/tutorial/datastructures.html"


def _chunks() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id="c0",
            text="列表是可变序列。",
            doc_url=_DOC_URL,
            source_url=f"{_DOC_URL}#lists",
            title="Data Structures",
            section="More on Lists",
            score=0.9,
        )
    ]


async def _search(query: str, top_k: int) -> list[RetrievedChunk]:
    return _chunks()


async def _fetch(url: str) -> str:
    return "<html></html>"


async def _preflight() -> None:
    return None


def _seed(
    database_url: str,
    *,
    with_mistake: bool,
    dict_attempts: int = 0,
) -> None:
    async def run() -> None:
        async with open_engine(database_url) as engine, open_session(engine) as session:
            await repositories.ensure_user(session, user_id="local")
            await repositories.create_chat_session(session, session_id="s1", user_id="local")
            await repositories.commit(session)
            if with_mistake:
                await repositories.record_mistake(
                    session,
                    idempotency_key="mistake:local:ex_1",
                    user_id="local",
                    topic="list",
                    exercise_id="ex_1",
                )
            for index in range(dict_attempts):
                await repositories.record_attempt(
                    session,
                    idempotency_key=f"attempt:{index}",
                    session_id="s1",
                    exercise_id=f"ex_d{index}",
                    topic="dict",
                    submitted_code="code",
                    is_passed=False,
                    score=0,
                )
            await repositories.commit(session)

    asyncio.run(run())


def _client(database_url: str, model: ScriptedChatModel) -> TestClient:
    app = create_app(
        database_url=database_url,
        rate_limit_enabled=False,
        progress_model=model,
        search=_search,
        fetch=_fetch,
        preflight=_preflight,
        store=InMemoryStore(),
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(tmp_database_url: str) -> Iterator[TestClient]:
    create_business_tables(tmp_database_url)
    _seed(tmp_database_url, with_mistake=True)
    with _client(tmp_database_url, _draft_model()) as client:
        yield client


def _draft_model() -> ScriptedChatModel:
    return ScriptedChatModel(
        structured_script=[
            NextStepDraft(
                topic="list",
                reason="你在列表上还没通过",
                exercise_ids=["ex_1"],
                next_steps=["重做 ex_1", "阅读列表文档"],
            )
        ]
    )


def test_recommend_returns_envelope_with_verified_refs(client: TestClient) -> None:
    response = client.post("/recommend", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    data = body["data"]
    assert set(data) == {
        "topic",
        "reason",
        "exercise_ids",
        "next_steps",
        "citations",
    }
    assert data["topic"] == "list"
    assert data["exercise_ids"] == ["ex_1"]
    assert "- 你在 ex_1（list）上未通过" in data["reason"]
    assert data["next_steps"] == ["重做 ex_1", "阅读列表文档"]
    assert data["citations"][0]["url"] == f"{_DOC_URL}#lists"
    assert data["citations"][0]["index"] == 1
    assert body["meta"]["usage"]["prompt_tokens"] == 40


def test_recommend_without_mistakes_is_deterministic_and_model_free(
    tmp_database_url: str,
) -> None:
    create_business_tables(tmp_database_url)
    _seed(tmp_database_url, with_mistake=False, dict_attempts=3)
    model = ScriptedChatModel()  # 空脚本：真被调用会 AssertionError

    with _client(tmp_database_url, model) as client:
        body = client.post("/recommend", json={}).json()

    data = body["data"]
    assert data["topic"] == "list"  # dict 已练 3 次，另外三个都是 0；按固定顺序取 list
    assert data["reason"] == "你还没有未解决的错题"
    assert data["exercise_ids"] == []
    assert data["citations"] == []
    assert data["next_steps"]
    assert model.structured_requests == []
    assert body["meta"]["usage"]["prompt_tokens"] == 0


def test_recommend_strips_ids_the_model_made_up(tmp_database_url: str) -> None:
    create_business_tables(tmp_database_url)
    _seed(tmp_database_url, with_mistake=True)
    model = ScriptedChatModel(
        structured_script=[
            NextStepDraft(
                topic="list",
                reason="先练列表",
                exercise_ids=["ex_1", "ex_ghost"],
                next_steps=["重做 ex_1"],
            )
        ]
    )

    with _client(tmp_database_url, model) as client:
        data = client.post("/recommend", json={}).json()["data"]

    assert data["exercise_ids"] == ["ex_1"]
    assert "ex_ghost" not in data["reason"]
