"""★ 唯一 SQL 出口（SPEC §2）。

`services/*` / 图节点 / 阶段四的路由**一律不写 SQL**，只调这里的函数。
例外只有 `migrations/` —— 迁移本来就是 DDL，不算业务读写。

幂等的权威是表上的 `UNIQUE` 约束（`idempotency_key`），不是应用层的 `if`：
恢复会重跑被中断的节点（§2），"先查再插"在并发下仍会撞，只有约束能真正兜住。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.db.models import ChatSession, ExerciseAttempt, Mistake, TokenLedger, User
from pyassist.logging import get_logger

logger = get_logger(__name__)

# 三张带 `idempotency_key` 的表。写入路径共用 `_record_once` 的同一套去重与
# SAVEPOINT 逻辑 —— 这就是"幂等只有一个实现"的落点。
type _IdempotentRow = ExerciseAttempt | Mistake | TokenLedger


async def commit(session: AsyncSession) -> None:
    """提交请求级事务。service 不直接碰 `session.commit()`：事务边界也归本文件。"""
    await session.commit()


async def ensure_user(
    session: AsyncSession, *, user_id: str, display_name: str | None = None
) -> User:
    """幂等建档。`user_id` 由调用方给出（将来从鉴权来），不用随机值。"""
    existing = await session.get(User, user_id)
    if existing is not None:
        return existing
    user = User(id=user_id, display_name=display_name)
    session.add(user)
    await session.flush()
    return user


async def create_chat_session(
    session: AsyncSession, *, session_id: str, user_id: str
) -> ChatSession:
    """建会话。`session_id` 同时就是 LangGraph 的 `thread_id`（见 models.py）。"""
    existing = await session.get(ChatSession, session_id)
    if existing is not None:
        return existing
    chat_session = ChatSession(id=session_id, user_id=user_id)
    session.add(chat_session)
    await session.flush()
    return chat_session


async def record_attempt(
    session: AsyncSession,
    *,
    idempotency_key: str,
    session_id: str,
    exercise_id: str,
    topic: str,
    submitted_code: str,
    is_passed: bool,
    score: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    duration_ms: int | None = None,
) -> bool:
    """落一条练习提交。**已被同一个 `idempotency_key` 记过则返回 `False`，不重复落。**

    返回 `bool` 而不是 `ExerciseAttempt`：调用方（节点）需要知道"这次跑是新的还是重跑"，
    但不需要拿到行对象。阶段八要读记录时用 `list_attempts`，不从这里返回。
    """
    return await _record_once(
        session,
        ExerciseAttempt(
            idempotency_key=idempotency_key,
            session_id=session_id,
            exercise_id=exercise_id,
            topic=topic,
            submitted_code=submitted_code,
            is_passed=is_passed,
            score=score,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
        ),
    )


async def record_mistake(
    session: AsyncSession,
    *,
    idempotency_key: str,
    user_id: str,
    topic: str,
    exercise_id: str | None = None,
    error_signature: str | None = None,
) -> bool:
    """落一条错题（幂等，语义同 `record_attempt`）。"""
    return await _record_once(
        session,
        Mistake(
            idempotency_key=idempotency_key,
            user_id=user_id,
            topic=topic,
            exercise_id=exercise_id,
            error_signature=error_signature,
        ),
    )


async def append_token_usage(
    session: AsyncSession,
    *,
    idempotency_key: str,
    session_id: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    cost_usd: Decimal,
    is_off_peak: bool,
) -> bool:
    """追加一条 Token 账本（幂等）。

    **本阶段不接任何请求路径**：阶段四的信封与阶段八的 `/stats` 才调它。
    字段名与 `llm.tracing.UsageSnapshot` 一一对应，调用方直接按属性名传。
    """
    return await _record_once(
        session,
        TokenLedger(
            idempotency_key=idempotency_key,
            session_id=session_id,
            model_id=model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            cost_usd=cost_usd,
            is_off_peak=is_off_peak,
        ),
    )


async def ping(session: AsyncSession) -> bool:
    """探活：能跑通一条最小查询即视为存储可用。

    裸 SQL 的唯一出口是本文件（守护 #13），所以 /health 的探活语句落在这里。
    捕获 `SQLAlchemyError` 而非 `Exception`：探活失败是可预期的运维状态，不是缺陷；
    且捕获面要窄，`BaseException`（如 `CancelledError`）必须继续上抛。
    """
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("db.ping.failed")
        return False
    return True


async def list_attempts(
    session: AsyncSession, *, session_id: str, limit: int = 50
) -> Sequence[ExerciseAttempt]:
    """`GET /history` 用（阶段八）。只有一处 `select()`，不散落在 service 里。"""
    result = await session.scalars(
        select(ExerciseAttempt)
        .where(ExerciseAttempt.session_id == session_id)
        .order_by(ExerciseAttempt.created_at.desc())
        .limit(limit)
    )
    return result.all()


async def list_recent_attempts(
    session: AsyncSession, *, limit: int = 30
) -> Sequence[ExerciseAttempt]:
    """跨会话取最近提交（`run_v3_eval --export-labels` 用）。

    排序与 `list_attempts_page` 一致：`(created_at DESC, id DESC)`，同一时刻的两次提交
    也不会抖。这里**不做用户过滤** —— 导出标注样本是单用户工具，调用方自己决定范围。
    """
    result = await session.scalars(
        select(ExerciseAttempt)
        .order_by(ExerciseAttempt.created_at.desc(), ExerciseAttempt.id.desc())
        .limit(limit)
    )
    return result.all()


async def count_attempts(session: AsyncSession) -> int:
    """测试与阶段八的统计用。"""
    return await session.scalar(select(func.count()).select_from(ExerciseAttempt)) or 0


async def _record_once(session: AsyncSession, instance: _IdempotentRow) -> bool:
    """插入一行带去重键的记录；键已存在（或被并发抢先）则返回 `False`。

    `SAVEPOINT`（`begin_nested`）：唯一约束冲突**只回滚这一条插入**，不牵连调用方
    在同一事务里已经做过的别的写入。换成 `session.rollback()` 会把整个事务一起丢掉
    —— 而"重复写入"在恢复重跑时是**常态**，不是异常路径。
    """
    if await _already_recorded(session, type(instance), instance.idempotency_key):
        return False
    try:
        async with session.begin_nested():
            session.add(instance)
            await session.flush()
    except IntegrityError:
        # 并发下另一个任务抢先落了同一把键 —— 这正是唯一约束存在的意义。
        # 捕获的是具体异常（§3.2「捕获链条不捕获大类」），不是 except Exception。
        logger.info(
            "db.duplicate",
            table=type(instance).__tablename__,
            idempotency_key=instance.idempotency_key,
        )
        return False
    return True


async def _already_recorded(
    session: AsyncSession,
    model: type[_IdempotentRow],
    idempotency_key: str,
) -> bool:
    """键是否已存在。**只是快路径优化，权威是表上的唯一约束。**"""
    found = await session.scalar(select(model.id).where(model.idempotency_key == idempotency_key))
    return found is not None


# ---------- 阶段八：v3 / v4（SPEC §5.14）----------


@dataclass(frozen=True, slots=True)
class TopicMistakeCount:
    """按主题聚合的错题数。`/stats` 的唯一列表口径（不返回裸 dict）。"""

    topic: str
    count: int


@dataclass(frozen=True, slots=True)
class StatsAggregate:
    """`/stats` 需要的全部聚合值。**frozen dataclass，不是 ORM 行**（§5.14）。

    服务层与路由都只读这个对象，不拼 SQL、不碰 `Row` 的元组下标。
    """

    today_prompt_tokens: int
    today_completion_tokens: int
    today_cached_tokens: int
    today_cost_usd: Decimal
    total_attempts: int
    passed_attempts: int
    mistake_total: int
    unresolved_mistakes: int
    mistakes_by_topic: list[TopicMistakeCount]


async def list_mistakes(
    session: AsyncSession,
    *,
    user_id: str,
    only_unresolved: bool = True,
    limit: int = 50,
) -> Sequence[Mistake]:
    """该用户的错题，按时间倒序。v4 推荐的候选集**只从这里来**（SPEC D7）。"""
    statement = select(Mistake).where(Mistake.user_id == user_id)
    if only_unresolved:
        statement = statement.where(Mistake.is_resolved.is_(False))
    statement = statement.order_by(Mistake.created_at.desc(), Mistake.id.desc()).limit(limit)
    result = await session.scalars(statement)
    return result.all()


async def mark_mistake_resolved(session: AsyncSession, *, user_id: str, exercise_id: str) -> bool:
    """把某用户某道题的错题标记为已解决。返回是否真的翻转过一行。"""
    result = await session.execute(
        update(Mistake)
        .where(
            Mistake.user_id == user_id,
            Mistake.exercise_id == exercise_id,
            Mistake.is_resolved.is_(False),
        )
        .values(is_resolved=True)
    )
    # execute() 的静态返回类型是 Result，DML 实际拿到的是 CursorResult（有 rowcount）。
    return bool(cast(CursorResult[Any], result).rowcount)


async def list_attempts_page(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
    cursor: tuple[datetime, str] | None = None,
) -> tuple[Sequence[ExerciseAttempt], bool]:
    """游标分页（SPEC §5.13）。返回 `(本页, 是否还有下一页)`。

    排序是 `(created_at DESC, id DESC)` —— 同一时刻的两次提交靠 id 兜底，
    否则翻页会重复或漏行。`user_id` 经 `sessions` 关联；
    **两个筛选条件同时给时以 `session_id` 为准**（§5.14）。
    多取一条用来判断"还有下一页"，它不会出现在返回值里。
    """
    statement = select(ExerciseAttempt)
    if session_id is not None:
        statement = statement.where(ExerciseAttempt.session_id == session_id)
    elif user_id is not None:
        statement = statement.join(ChatSession, ChatSession.id == ExerciseAttempt.session_id).where(
            ChatSession.user_id == user_id
        )

    if cursor is not None:
        created_at, row_id = cursor
        statement = statement.where(
            or_(
                ExerciseAttempt.created_at < created_at,
                and_(
                    ExerciseAttempt.created_at == created_at,
                    ExerciseAttempt.id < row_id,
                ),
            )
        )

    statement = statement.order_by(
        ExerciseAttempt.created_at.desc(), ExerciseAttempt.id.desc()
    ).limit(limit + 1)
    result = await session.scalars(statement)
    rows = list(result.all())
    has_more = len(rows) > limit
    return rows[:limit], has_more


async def count_attempts_for(
    session: AsyncSession, *, user_id: str | None, session_id: str | None
) -> int:
    """`Page.total` 的口径：筛选条件下的**总数**，不是本页条数。"""
    statement = select(func.count()).select_from(ExerciseAttempt)
    if session_id is not None:
        statement = statement.where(ExerciseAttempt.session_id == session_id)
    elif user_id is not None:
        statement = statement.join(ChatSession, ChatSession.id == ExerciseAttempt.session_id).where(
            ChatSession.user_id == user_id
        )
    return int(await session.scalar(statement) or 0)


async def count_attempts_by_topic(session: AsyncSession, *, user_id: str) -> dict[str, int]:
    """该用户各主题的练习次数（v4 空候选时挑"最不熟的主题"用，SPEC §5.9）。

    与 `/stats` 的 `mistakes_by_topic` 不是同一件事：那边数错题，这里数尝试次数。
    """
    statement = (
        select(ExerciseAttempt.topic, func.count(ExerciseAttempt.id))
        .join(ChatSession, ChatSession.id == ExerciseAttempt.session_id)
        .where(ChatSession.user_id == user_id)
        .group_by(ExerciseAttempt.topic)
    )
    rows = (await session.execute(statement)).all()
    return {str(row[0]): int(row[1]) for row in rows}


async def sum_tokens_since(
    session: AsyncSession, *, user_id: str, since: datetime
) -> tuple[int, int, int, Decimal]:
    """该用户在 `since` 之后的 `(prompt, completion, cached, cost)`。

    配额与 `/stats` 读**同一张表**（SPEC S13）—— 不维护第二份计数器。
    """
    statement = (
        select(
            func.coalesce(func.sum(TokenLedger.prompt_tokens), 0),
            func.coalesce(func.sum(TokenLedger.completion_tokens), 0),
            func.coalesce(func.sum(TokenLedger.cached_tokens), 0),
            func.coalesce(func.sum(TokenLedger.cost_usd), 0),
        )
        .join(ChatSession, ChatSession.id == TokenLedger.session_id)
        .where(ChatSession.user_id == user_id, TokenLedger.created_at >= since)
    )
    row = (await session.execute(statement)).one()
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), _as_decimal(row[3])


async def get_stats(session: AsyncSession, *, user_id: str, since: datetime) -> StatsAggregate:
    """`/stats` 的全部聚合。一次读三张表，不调模型、不写任何东西。"""
    prompt_tokens, completion_tokens, cached_tokens, cost_usd = await sum_tokens_since(
        session, user_id=user_id, since=since
    )

    attempts_statement = (
        select(
            func.count(ExerciseAttempt.id),
            func.coalesce(func.sum(case((ExerciseAttempt.is_passed.is_(True), 1), else_=0)), 0),
        )
        .join(ChatSession, ChatSession.id == ExerciseAttempt.session_id)
        .where(ChatSession.user_id == user_id)
    )
    attempts_row = (await session.execute(attempts_statement)).one()
    total_attempts = int(attempts_row[0] or 0)
    passed_attempts = int(attempts_row[1] or 0)

    mistake_statement = select(
        func.count(Mistake.id),
        func.coalesce(func.sum(case((Mistake.is_resolved.is_(False), 1), else_=0)), 0),
    ).where(Mistake.user_id == user_id)
    mistake_row = (await session.execute(mistake_statement)).one()
    mistake_total = int(mistake_row[0] or 0)
    unresolved = int(mistake_row[1] or 0)

    topic_statement = (
        select(Mistake.topic, func.count(Mistake.id))
        .where(Mistake.user_id == user_id)
        .group_by(Mistake.topic)
        .order_by(Mistake.topic)
    )
    topic_rows = (await session.execute(topic_statement)).all()

    return StatsAggregate(
        today_prompt_tokens=prompt_tokens,
        today_completion_tokens=completion_tokens,
        today_cached_tokens=cached_tokens,
        today_cost_usd=cost_usd,
        total_attempts=total_attempts,
        passed_attempts=passed_attempts,
        mistake_total=mistake_total,
        unresolved_mistakes=unresolved,
        mistakes_by_topic=[
            TopicMistakeCount(topic=str(row[0]), count=int(row[1])) for row in topic_rows
        ],
    )


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    return Decimal(0)
