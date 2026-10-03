"""0001 · 建业务表（SPEC §2）。**没有 `messages` 表。**

LangGraph 的 `checkpoints` / `writes` / store 表**不在这里** —— 它们由
`migrations/env.py` 在迁移事务提交之后调 `checkpointer.setup()` / `store.setup()`
建（幂等）。理由见 env.py 的模块 docstring（SQLite 写锁）。

Revision ID: 0001_init
Revises:
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None

# 删表顺序：子表在前，父表在后（SQLite 默认不校验外键，但顺序保持可读）。
_BUSINESS_TABLES = ("token_ledger", "mistakes", "exercise_attempts", "sessions", "users")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("display_name", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(32), primary_key=True),  # == LangGraph thread_id
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "exercise_attempts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("exercise_id", sa.String(64), nullable=False),
        sa.Column("topic", sa.String(32), nullable=False),
        sa.Column("is_passed", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("submitted_code", sa.Text(), nullable=False),
        sa.Column("stdout", sa.Text(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),  # 幂等的权威
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_exercise_attempts_session_id", "exercise_attempts", ["session_id"])
    op.create_index("ix_exercise_attempts_exercise_id", "exercise_attempts", ["exercise_id"])
    op.create_index(
        "ix_exercise_attempts_idempotency_key",
        "exercise_attempts",
        ["idempotency_key"],
        unique=True,
    )

    op.create_table(
        "mistakes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("exercise_id", sa.String(64), nullable=True),
        sa.Column("topic", sa.String(32), nullable=False),
        sa.Column("error_signature", sa.String(256), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mistakes_user_id", "mistakes", ["user_id"])
    op.create_index("ix_mistakes_exercise_id", "mistakes", ["exercise_id"])
    op.create_index("ix_mistakes_idempotency_key", "mistakes", ["idempotency_key"], unique=True)

    op.create_table(
        "token_ledger",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column("is_off_peak", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_token_ledger_session_id", "token_ledger", ["session_id"])
    op.create_index(
        "ix_token_ledger_idempotency_key", "token_ledger", ["idempotency_key"], unique=True
    )


def downgrade() -> None:
    for table in _BUSINESS_TABLES:
        op.drop_table(table)
