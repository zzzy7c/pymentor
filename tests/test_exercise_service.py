"""v3 服务层（SPEC §5.8）：出题、提交、落库、幂等与错题翻转。

图与沙箱都是替身；**数据库是真的**（临时 SQLite + 真检查点）。这里要钉住的正是
"图跑完之后，库里到底多了几行"——那是图测试看不到的部分。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from fakes.llm import ScriptedChatModel
from fakes.sandbox import FakeSandboxRunner, result_for
from pyassist.agent.checkpointer import open_checkpointer
from pyassist.agent.graphs.exercise import ExerciseGraphs, build_exercise_graph
from pyassist.agent.tools.grading import ExerciseDraft, GradeDraft
from pyassist.agent.tools.grading import TestCaseDraft as DraftTest
from pyassist.config import TEST_RESULT_MARKER
from pyassist.db import models, repositories
from pyassist.db.session import open_engine, open_session
from pyassist.errors import ExerciseNotFound
from pyassist.rag.retriever import RetrievedChunk
from pyassist.services.exercise import create_exercise, submit_exercise


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


def _stdout(*, passed: int, failed: int) -> str:
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


async def _no_search(query: str, top_k: int) -> list[RetrievedChunk]:
    raise AssertionError(f"这条用例不该检索：{query!r}")


class _Env:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        graphs: ExerciseGraphs,
        runner: FakeSandboxRunner,
    ) -> None:
        self.engine = engine
        self.graphs = graphs
        self.runner = runner


async def _env(
    tmp_database_url: str,
    *,
    drafts: list[ExerciseDraft],
    grades: list[GradeDraft],
    results: list[Any],
    search: Any = _no_search,
) -> AsyncIterator[_Env]:
    async with open_engine(tmp_database_url) as engine:
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        async with open_checkpointer(database_url=tmp_database_url) as saver:
            model = ScriptedChatModel(structured_script=[*drafts, *grades])
            runner = FakeSandboxRunner(results)
            graphs = build_exercise_graph(
                checkpointer=saver, model=model, runner=runner, search=search
            )
            yield _Env(engine=engine, graphs=graphs, runner=runner)


@pytest.fixture
async def env_factory(tmp_database_url: str):
    opened: list[Any] = []

    async def factory(**kwargs: Any) -> _Env:
        generator = _env(tmp_database_url, **kwargs)
        env = await generator.__anext__()
        opened.append(generator)
        return env

    yield factory

    for generator in opened:
        await generator.aclose()


async def test_create_exercise_returns_spec_with_thread_id_as_id(env_factory) -> None:
    env = await env_factory(
        drafts=[_draft()],
        grades=[],
        results=[result_for("", stdout=_stdout(passed=1, failed=0))],
    )

    result = await create_exercise(
        topic="list",
        graph=env.graphs.generation,
        exercise_id="ex_abc",
        request_id="req-1",
    )

    assert result.exercise.exercise_id == "ex_abc"
    assert result.exercise.topic == "list"
    assert result.usage.prompt_tokens == 40
    assert env.runner.calls  # 自校验真的跑了一次（假的）沙箱


async def test_submit_persists_attempt_and_mistake_once(env_factory) -> None:
    env = await env_factory(
        drafts=[_draft()],
        grades=[
            GradeDraft(summary="有一条用例失败", doc_queries=["list append"]),
            GradeDraft(summary="有一条用例失败", doc_queries=["list append"]),
        ],
        results=[
            result_for("", stdout=_stdout(passed=1, failed=0)),  # 生成自校验
            result_for("", stdout=_stdout(passed=0, failed=1)),  # 第一次提交
            result_for("", stdout=_stdout(passed=0, failed=1)),  # 重放
        ],
        search=_search,
    )
    await create_exercise(
        topic="list", graph=env.graphs.generation, exercise_id="ex_1", request_id="r"
    )

    async with open_session(env.engine) as session:
        first = await submit_exercise(
            exercise_id="ex_1",
            code="def add(a, b):\n    return a\n",
            graphs=env.graphs,
            session=session,
            user_id="local",
            session_id="sess-1",
            request_id="req-1",
        )
        second = await submit_exercise(
            exercise_id="ex_1",
            code="def add(a, b):\n    return a\n",
            graphs=env.graphs,
            session=session,
            user_id="local",
            session_id="sess-1",
            request_id="req-2",
        )

        attempts = await repositories.list_attempts(session, session_id="sess-1")
        mistakes = await repositories.list_mistakes(session, user_id="local", only_unresolved=False)

    assert first.is_passed is False
    assert first.score == 0
    assert first.report.total == 1
    assert second.is_passed is False  # 重放仍返回结果，不报错
    assert len(attempts) == 1  # 同一个 (exercise, code) 只落一行
    assert len(mistakes) == 1
    assert mistakes[0].exercise_id == "ex_1"
    assert mistakes[0].is_resolved is False
    assert first.citations


async def test_later_pass_flips_mistake_to_resolved(env_factory) -> None:
    env = await env_factory(
        drafts=[_draft()],
        grades=[
            GradeDraft(summary="失败", doc_queries=[]),
            GradeDraft(summary="通过", doc_queries=[]),
        ],
        results=[
            result_for("", stdout=_stdout(passed=1, failed=0)),  # 自校验
            result_for("", stdout=_stdout(passed=0, failed=1)),  # 失败提交
            result_for("", stdout=_stdout(passed=1, failed=0)),  # 通过提交
        ],
    )
    await create_exercise(
        topic="list", graph=env.graphs.generation, exercise_id="ex_2", request_id="r"
    )

    async with open_session(env.engine) as session:
        await submit_exercise(
            exercise_id="ex_2",
            code="wrong",
            graphs=env.graphs,
            session=session,
            user_id="local",
            session_id="sess-2",
            request_id="r1",
        )
        passed = await submit_exercise(
            exercise_id="ex_2",
            code="right",
            graphs=env.graphs,
            session=session,
            user_id="local",
            session_id="sess-2",
            request_id="r2",
        )
        mistakes = await repositories.list_mistakes(session, user_id="local", only_unresolved=False)

    assert passed.is_passed is True
    assert passed.score == 100
    assert len(mistakes) == 1
    assert mistakes[0].is_resolved is True


async def test_submit_unknown_exercise_raises_not_found(env_factory) -> None:
    env = await env_factory(drafts=[], grades=[], results=[])

    async with open_session(env.engine) as session:
        with pytest.raises(ExerciseNotFound):
            await submit_exercise(
                exercise_id="ex_missing",
                code="x = 1",
                graphs=env.graphs,
                session=session,
                user_id="local",
                session_id="sess-3",
                request_id="r",
            )


async def test_submit_stores_full_user_code_in_attempt(env_factory) -> None:
    """§5.4 要求 `GET /history` 能回放代码；日志不记代码是另一条约束。"""
    env = await env_factory(
        drafts=[_draft()],
        grades=[GradeDraft(summary="失败", doc_queries=[])],
        results=[
            result_for("", stdout=_stdout(passed=1, failed=0)),
            result_for("", stdout=_stdout(passed=0, failed=1)),
        ],
    )
    await create_exercise(
        topic="list", graph=env.graphs.generation, exercise_id="ex_3", request_id="r"
    )
    code = "def add(a, b):\n    raise ValueError('nope')\n"

    async with open_session(env.engine) as session:
        await submit_exercise(
            exercise_id="ex_3",
            code=code,
            graphs=env.graphs,
            session=session,
            user_id="local",
            session_id="sess-4",
            request_id="r",
        )
        attempt = (
            await session.scalars(
                select(models.ExerciseAttempt).where(models.ExerciseAttempt.exercise_id == "ex_3")
            )
        ).one()

    assert attempt.submitted_code == code
    assert attempt.stdout is not None
    assert attempt.duration_ms == 1
