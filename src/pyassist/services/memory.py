"""用户画像的读 / 写 / 渲染（SPEC §5.10 / D6）。

持久化一律经 LangGraph `Store`（`agent/store.py`），不建第二张表、不调模型：
`update_profile` 是纯计数，输入相同就得到相同的画像。记忆是增强，不是主路径 ——
Store 的失败只记日志，绝不让一次提交因画像写不进去而变成 500。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from langgraph.store.base import BaseStore

from pyassist.agent.state import AttemptRecord, MistakeRecord
from pyassist.config import (
    PROFILE_NAMESPACE,
    PROFILE_RECENT_EXERCISE_LIMIT,
    PROFILE_SUMMARY_KEY,
)
from pyassist.logging import get_logger

logger = get_logger(__name__)

_MAX_BLOCK_CHARS = 800


@dataclass(frozen=True, slots=True)
class TopicStat:
    """单个主题的累计数字。字段可加，渲染顺序按主题名排序。"""

    attempts: int
    passed: int
    unresolved_mistakes: int


@dataclass(frozen=True, slots=True)
class Profile:
    user_id: str
    topics: dict[str, TopicStat]
    unresolved_mistakes: int
    recent_exercise_ids: tuple[str, ...]
    updated_at: str


def _namespace(user_id: str) -> tuple[str, str]:
    return (PROFILE_NAMESPACE, user_id)


async def load_profile(store: BaseStore, user_id: str) -> Profile | None:
    """读画像。没有 / 形状不认识 → `None`（不猜、不修补）。"""
    item = await store.aget(_namespace(user_id), PROFILE_SUMMARY_KEY)
    if item is None or not isinstance(item.value, dict):
        return None
    value = item.value
    try:
        raw_topics = value["topics"]
        topics = {
            str(topic): TopicStat(
                attempts=int(stat["attempts"]),
                passed=int(stat["passed"]),
                unresolved_mistakes=int(stat["unresolved_mistakes"]),
            )
            for topic, stat in dict(raw_topics).items()
        }
        return Profile(
            user_id=str(value["user_id"]),
            topics=topics,
            unresolved_mistakes=int(value["unresolved_mistakes"]),
            recent_exercise_ids=tuple(str(item) for item in value["recent_exercise_ids"]),
            updated_at=str(value["updated_at"]),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("memory.invalid_profile", user_id=user_id)
        return None


async def update_profile(
    store: BaseStore,
    *,
    user_id: str,
    mistakes: Sequence[MistakeRecord],
    attempts: Sequence[AttemptRecord],
) -> Profile:
    """完全确定性地重算画像并写回。**不调模型**（D6）。"""
    topics: dict[str, TopicStat] = {}

    def stat(topic: str) -> TopicStat:
        return topics.setdefault(topic, TopicStat(attempts=0, passed=0, unresolved_mistakes=0))

    for attempt in attempts:
        current = stat(attempt.topic)
        topics[attempt.topic] = TopicStat(
            attempts=current.attempts + 1,
            passed=current.passed + (1 if attempt.is_passed else 0),
            unresolved_mistakes=current.unresolved_mistakes,
        )

    unresolved = 0
    for mistake in mistakes:
        current = stat(mistake.topic)
        unresolved_here = 0 if mistake.is_resolved else 1
        unresolved += unresolved_here
        topics[mistake.topic] = TopicStat(
            attempts=current.attempts,
            passed=current.passed,
            unresolved_mistakes=current.unresolved_mistakes + unresolved_here,
        )

    recent: list[str] = []
    for attempt in sorted(attempts, key=lambda item: item.created_at, reverse=True):
        if attempt.exercise_id and attempt.exercise_id not in recent:
            recent.append(attempt.exercise_id)
        if len(recent) >= PROFILE_RECENT_EXERCISE_LIMIT:
            break

    profile = Profile(
        user_id=user_id,
        topics=topics,
        unresolved_mistakes=unresolved,
        recent_exercise_ids=tuple(recent),
        updated_at=datetime.now(UTC).isoformat(),
    )
    await store.aput(_namespace(user_id), PROFILE_SUMMARY_KEY, _to_value(profile))
    return profile


def render_profile_block(profile: Profile | None) -> str:
    """固定格式的多行文本，≤ 800 字符。空画像返回 `""`（不塞空壳）。"""
    if profile is None:
        return ""
    lines = ["学习者画像："]
    for topic in sorted(profile.topics):
        stat = profile.topics[topic]
        lines.append(
            f"- {topic}：练习 {stat.attempts} 次，通过 {stat.passed} 次，"
            f"未解决错题 {stat.unresolved_mistakes} 道"
        )
    lines.append(f"未解决错题合计：{profile.unresolved_mistakes} 道")
    if profile.recent_exercise_ids:
        lines.append(f"最近练习：{', '.join(profile.recent_exercise_ids)}")
    return "\n".join(lines)[:_MAX_BLOCK_CHARS]


def _to_value(profile: Profile) -> dict[str, object]:
    """Store 只收 JSON 友好的 dict；dataclass 在这里一次性展开。"""
    return {
        "user_id": profile.user_id,
        "topics": {
            topic: {
                "attempts": stat.attempts,
                "passed": stat.passed,
                "unresolved_mistakes": stat.unresolved_mistakes,
            }
            for topic, stat in profile.topics.items()
        },
        "unresolved_mistakes": profile.unresolved_mistakes,
        "recent_exercise_ids": list(profile.recent_exercise_ids),
        "updated_at": profile.updated_at,
    }


__all__ = [
    "Profile",
    "TopicStat",
    "load_profile",
    "render_profile_block",
    "update_profile",
]
