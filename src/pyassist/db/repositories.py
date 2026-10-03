"""★ 唯一 SQL 出口（SPEC §2）。

`services/*` / 图节点 / 阶段四的路由**一律不写 SQL**，只调这里的函数。
例外只有 `migrations/` —— 迁移本来就是 DDL，不算业务读写。

幂等的权威是表上的 `UNIQUE` 约束（`idempotency_key`），不是应用层的 `if`：
恢复会重跑被中断的节点（§2），"先查再插"在并发下仍会撞，只有约束能真正兜住。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.db.models import ChatSession, ExerciseAttempt, Mistake, TokenLedger, User
from pyassist.logging import get_logger

logger = get_logger(__name__)

# 三张带 `idempotency_key` 的表。写入路径共用 `_record_once` 的同一套去重与
# SAVEPOINT 逻辑 —— 这就是"幂等只有一个实现"的落点。
type _IdempotentRow = ExerciseAttempt | Mistake | TokenLedger


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
