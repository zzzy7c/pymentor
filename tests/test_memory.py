"""阶段五记忆（SPEC §5.10 / D6）：画像纯计数、确定性渲染、只碰一个 Store。

Store 用上游的 `InMemoryStore`（`langgraph.store.memory`）—— 它是**同一个
`BaseStore` 接口**的内存实现，不是我们自造的替身。这里要钉住的是"画像由哪些
数字算出来"，而不是存储后端的行为。
"""

from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from pyassist.agent.state import AttemptRecord, MistakeRecord
from pyassist.config import PROFILE_NAMESPACE, PROFILE_SUMMARY_KEY
from pyassist.services.memory import (
    load_profile,
    render_profile_block,
    update_profile,
)


def _attempt(
    *, exercise_id: str, topic: str, passed: bool, created_at: str = "2026-10-01T00:00:00+00:00"
) -> AttemptRecord:
    return AttemptRecord(
        exercise_id=exercise_id,
        topic=topic,
        is_passed=passed,
        score=100 if passed else 0,
        created_at=created_at,
    )


def _mistake(*, exercise_id: str, topic: str, resolved: bool = False) -> MistakeRecord:
    return MistakeRecord(
        id=f"m-{exercise_id}",
        exercise_id=exercise_id,
        topic=topic,
        error_signature="boom",
        is_resolved=resolved,
        created_at="2026-10-01T00:00:00+00:00",
    )


async def test_update_profile_counts_attempts_and_unresolved_mistakes() -> None:
    store = InMemoryStore()

    profile = await update_profile(
        store,
        user_id="u1",
        attempts=[
            _attempt(exercise_id="ex_1", topic="list", passed=False),
            _attempt(exercise_id="ex_2", topic="list", passed=True),
            _attempt(exercise_id="ex_3", topic="dict", passed=True),
        ],
        mistakes=[
            _mistake(exercise_id="ex_1", topic="list"),
            _mistake(exercise_id="ex_3", topic="dict", resolved=True),
        ],
    )

    assert profile.topics["list"].attempts == 2
    assert profile.topics["list"].passed == 1
    assert profile.topics["list"].unresolved_mistakes == 1
    assert profile.topics["dict"].unresolved_mistakes == 0
    assert profile.unresolved_mistakes == 1
    assert profile.recent_exercise_ids == ("ex_1", "ex_2", "ex_3")


async def test_profile_is_readable_back_from_the_same_namespace() -> None:
    store = InMemoryStore()
    written = await update_profile(
        store,
        user_id="u1",
        attempts=[_attempt(exercise_id="ex_1", topic="async", passed=False)],
        mistakes=[_mistake(exercise_id="ex_1", topic="async")],
    )

    loaded = await load_profile(store, "u1")

    assert loaded is not None
    assert loaded.user_id == "u1"
    assert loaded.topics == written.topics
    assert loaded.unresolved_mistakes == written.unresolved_mistakes
    assert loaded.recent_exercise_ids == written.recent_exercise_ids


async def test_load_profile_returns_none_for_missing_user() -> None:
    store = InMemoryStore()

    assert await load_profile(store, "nobody") is None


async def test_load_profile_rejects_unknown_shape_without_guessing() -> None:
    """存进去的东西形状不认识时返回 `None`，不修补、不半读。"""
    store = InMemoryStore()
    await store.aput((PROFILE_NAMESPACE, "u1"), PROFILE_SUMMARY_KEY, {"topics": "oops"})

    assert await load_profile(store, "u1") is None


async def test_users_are_isolated_in_store_namespace() -> None:
    store = InMemoryStore()
    await update_profile(
        store,
        user_id="u1",
        attempts=[_attempt(exercise_id="ex_1", topic="list", passed=True)],
        mistakes=[],
    )

    assert await load_profile(store, "u2") is None


def test_render_profile_block_is_deterministic_and_sorted() -> None:
    from pyassist.services.memory import Profile, TopicStat

    profile = Profile(
        user_id="u1",
        topics={
            "list": TopicStat(attempts=2, passed=1, unresolved_mistakes=1),
            "async": TopicStat(attempts=1, passed=0, unresolved_mistakes=1),
        },
        unresolved_mistakes=2,
        recent_exercise_ids=("ex_2", "ex_1"),
        updated_at="2026-10-01T00:00:00+00:00",
    )

    first = render_profile_block(profile)
    second = render_profile_block(profile)

    assert first == second
    # 主题按名字排序：async 在 list 之前
    assert first.index("async") < first.index("list")
    assert "未解决错题合计：2 道" in first
    assert len(first) <= 800


def test_render_profile_block_is_empty_for_no_profile() -> None:
    assert render_profile_block(None) == ""


def test_render_profile_block_truncates_to_800_chars() -> None:
    from pyassist.services.memory import Profile, TopicStat

    topics = {
        f"topic-{index:03d}": TopicStat(attempts=index, passed=0, unresolved_mistakes=index)
        for index in range(200)
    }
    profile = Profile(
        user_id="u1",
        topics=topics,
        unresolved_mistakes=1,
        recent_exercise_ids=(),
        updated_at="2026-10-01T00:00:00+00:00",
    )

    assert len(render_profile_block(profile)) == 800


async def test_update_profile_does_not_open_a_second_store() -> None:
    """D6：画像只写 `agent/store.py` 注入的 Store，不建第二个库。

    用 spy 包住唯一那个 Store，断言调用次数 —— 而不是去翻文件系统。
    """
    calls: list[tuple[str, str]] = []

    class SpyStore(InMemoryStore):
        async def aput(self, namespace, key, value, *args, **kwargs) -> None:
            calls.append((namespace[0], key))
            await super().aput(namespace, key, value, *args, **kwargs)

    store = SpyStore()

    await update_profile(
        store,
        user_id="u1",
        attempts=[_attempt(exercise_id="ex_1", topic="list", passed=True)],
        mistakes=[],
    )

    assert calls == [(PROFILE_NAMESPACE, PROFILE_SUMMARY_KEY)]


@pytest.mark.parametrize("user_id", ["local", "u1"])
async def test_profile_roundtrip_for_multiple_user_ids(user_id: str) -> None:
    store = InMemoryStore()
    await update_profile(store, user_id=user_id, attempts=[], mistakes=[])

    profile = await load_profile(store, user_id)

    assert profile is not None
    assert profile.topics == {}
    assert profile.unresolved_mistakes == 0
