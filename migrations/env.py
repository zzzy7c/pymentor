"""Alembic 环境（同步）。

刻意用**同步**引擎：迁移是 CLI 的一次性动作，而 `checkpointer.setup()` /
`store.setup()` 是异步的 —— 只有在**没有运行中事件循环**的同步上下文里，
才能安全地 `asyncio.run(...)`。用 Alembic 的异步模板（`connection.run_sync`）
反而会因为 greenlet 外层已有事件循环而炸。

LangGraph 的地表**不写进 0001_init.py 的 upgrade()**：那里还处在迁移事务内，
此时再开一条连接去建表，会和迁移事务抢同一个 SQLite 文件的写锁。改成在
**事务提交、连接关闭之后**建 —— 顺序见 `run_migrations_online()`。
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from pyassist.agent.checkpointer import setup_checkpointer
from pyassist.agent.store import setup_store
from pyassist.config import settings
from pyassist.db.models import Base

config = context.config
target_metadata = Base.metadata

# LangGraph 自己的表不归 Alembic 管（它们的形状由上游的 setup() 决定，会随
# langgraph 版本变）。没有这个过滤器，`alembic check` 会把它们报成"数据库里多出来的
# 表"并要求 DROP —— 一个会把检查点连同对话历史一起删掉的假告警。
_LANGGRAPH_TABLE_PREFIXES = ("checkpoint", "writes", "store", "vector")


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """只把**业务表**纳入 Alembic 的视野。"""
    return not (
        type_ == "table" and name is not None and name.startswith(_LANGGRAPH_TABLE_PREFIXES)
    )


# 迁移用同步驱动。这个映射只属于迁移流程 —— §2 的"不得出现方言分支"针对的是
# db/session.py（业务路径），迁移从来就是一次性的 DDL 工具。
_SYNC_DRIVERS = {
    "sqlite+aiosqlite": "sqlite+pysqlite",
    "postgresql+psycopg": "postgresql+psycopg",  # psycopg3 同步异步同名驱动
}


def _sync_url() -> str:
    url = make_url(settings.database_url)
    return url.set(drivername=_SYNC_DRIVERS.get(url.drivername, url.drivername)).render_as_string(
        hide_password=False
    )


def _setup_langgraph_storage() -> None:
    """建检查点与 Store 的表。幂等，重复跑安全（§2「setup 只需在建库时跑一次」）。"""

    async def _run() -> None:
        await setup_checkpointer()
        await setup_store()

    asyncio.run(_run())


def run_migrations_offline() -> None:
    """只生成 SQL（`alembic upgrade --sql`）：不建 LangGraph 的表，也不连库。"""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_sync_url())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_object=include_object,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
    # 到这里迁移事务已提交、连接已关闭 —— 再建 LangGraph 的表，不抢写锁。
    _setup_langgraph_storage()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
