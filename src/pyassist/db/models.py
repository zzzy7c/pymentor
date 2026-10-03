"""SQLAlchemy ORM（SPEC §2）。**没有 `messages` 表。**

对话历史的唯一真相是 LangGraph 检查点（`agent/checkpointer.py`）；业务表只存
"人的行为结果"——用户、会话、练习提交、错题、Token 账本。在这里再存一份消息，
就制造了两份会互相矛盾的对话历史，正是 §2 要防的分叉。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有业务表的基类。`migrations/env.py` 的 `target_metadata` 用它。"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    display_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ChatSession(Base):
    """会话。**`id` 就是 LangGraph 的 `thread_id`** —— 不另立一套身份。

    §2 说续跑只要 `config={"configurable": {"thread_id": <会话 id>}}`；
    若这里再造一个 `thread_id` 列，同一件事就有两个 id，早晚不一致。
    类名叫 `ChatSession` 是为了不和 SQLAlchemy 的 `Session` 混淆。
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ExerciseAttempt(Base):
    """v3 的练习提交（§5.4）。"""

    __tablename__ = "exercise_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    exercise_id: Mapped[str] = mapped_column(String(64), index=True)
    topic: Mapped[str] = mapped_column(String(32))
    is_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[int | None] = mapped_column(Integer, default=None)
    # 用户代码落库是**产品数据**（`GET /history` 要回放），
    # 与 §3.2「日志不记完整用户代码」不冲突 —— 那条约束的是 structlog，不是数据库。
    submitted_code: Mapped[str] = mapped_column(Text)
    stdout: Mapped[str | None] = mapped_column(Text, default=None)
    stderr: Mapped[str | None] = mapped_column(Text, default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    # 幂等键：节点重跑时同一个键只落一行（§2）。权威见 repositories.py。
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Mistake(Base):
    """错题（§5.4 / §5.5）。v4 的推荐**必须引用实际做错的题目 id**，所以要有 `exercise_id`。"""

    __tablename__ = "mistakes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    exercise_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    topic: Mapped[str] = mapped_column(String(32))
    error_signature: Mapped[str | None] = mapped_column(String(256), default=None)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TokenLedger(Base):
    """Token 账本。**列名照抄 `llm.tracing.UsageSnapshot` = §3.1 信封 `meta.usage`。**

    阶段二留下的缝：`UsageSnapshot` 已经在算这些数，阶段四的信封直接序列化它，
    阶段八的 `/stats` 与熔断读这张表 —— 三处字段名一致，不需要任何映射层。
    """

    __tablename__ = "token_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    model_id: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Decimal 落库：单价来自 config.PRICE_TABLE（也是 Decimal）。用 Float 会产生精度假象。
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    is_off_peak: Mapped[bool] = mapped_column(Boolean, default=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
