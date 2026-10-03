"""节点幂等守护（SPEC §2）：恢复会重跑被中断的节点，副作用必须带去重键。

图和 State 都定义在**本测试文件内** —— 阶段三不建任何产品图（那是阶段六的
`agent/graphs/*.py` + `agent/state.py`）。这是测试夹具，不是"第二套图状态 schema"。
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from pyassist.agent.checkpointer import open_checkpointer
from pyassist.db.models import Base
from pyassist.db.repositories import (
    count_attempts,
    create_chat_session,
    ensure_user,
    record_attempt,
)
from pyassist.db.session import create_engine, create_session_factory

THREAD_ID = "thread-idempotency-1"
DEDUP_KEY = f"{THREAD_ID}:probe:0"


class _ProbeState(TypedDict):
    passes: int


def _build_graph(session_factory: object, *, fail_first: bool) -> tuple[StateGraph, dict[str, int]]:
    """一个会"第一次失败、第二次成功"的单节点图。

    用 `RetryPolicy` 迫使**同一个节点在同一个 thread 下重跑** —— 这正是 §2 说的
    "恢复会重跑被中断的节点"的最小可复现形态。
    """
    calls = {"n": 0}

    async def probe(state: _ProbeState) -> _ProbeState:
        calls["n"] += 1
        async with session_factory() as session:  # type: ignore[operator]
            await record_attempt(
                session,
                idempotency_key=DEDUP_KEY,
                session_id=THREAD_ID,
                exercise_id="ex-1",
                topic="列表",
                submitted_code="pass",
                is_passed=True,
            )
            await session.commit()
        if fail_first and calls["n"] == 1:
            raise RuntimeError("第一次执行故意失败，迫使同节点重跑")
        return {"passes": state["passes"] + 1}

    builder = StateGraph(_ProbeState)
    builder.add_node(
        "probe",
        probe,
        # 三个坑（SPEC §1.6）：默认 retry_on 不重试 RuntimeError；默认会真 sleep。
        retry_policy=RetryPolicy(
            max_attempts=2, retry_on=RuntimeError, initial_interval=0.0, jitter=False
        ),
    )
    builder.add_edge(START, "probe")
    builder.add_edge("probe", END)
    return builder, calls


async def test_node_rerun_keeps_business_rows_at_one(tmp_path: Path) -> None:
    """同一 thread 下节点跑了两次，业务表仍只有一行（去重键生效）。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    async with session_factory() as session:
        await ensure_user(session, user_id="u1")
        await create_chat_session(session, session_id=THREAD_ID, user_id="u1")
        await session.commit()

    async with open_checkpointer(database_url=url) as saver:
        builder, calls = _build_graph(session_factory, fail_first=True)
        graph = builder.compile(checkpointer=saver)
        # §2：恢复只传 config，不重传 state。
        await graph.ainvoke({"passes": 0}, {"configurable": {"thread_id": THREAD_ID}})

    assert calls["n"] == 2, "节点必须真的重跑过，否则这个测试是空转"

    async with session_factory() as session:
        assert await count_attempts(session) == 1
    await engine.dispose()
