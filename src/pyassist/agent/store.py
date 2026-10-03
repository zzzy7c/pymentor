"""★ 唯一长期记忆 Store 构造点（SPEC §2）。

长期记忆与用户画像走 LangGraph `Store`，**不走 `db/repositories.py`，也不另开
第二个向量库**。`services/memory.py`（阶段八）只负责"从对话里抽取偏好"这类加工，
持久化一律经这里。

`index=None`：不配 embeddings。向量模型选型是阶段五的待定项，本阶段**不替它拍板**；
语义检索所需的 index 配置是阶段八记忆功能的缝。

注意：`langgraph.store.sqlite.base` 有**模块级**的 `import sqlite_vec`，
所以本模块的 import 硬依赖 `sqlite-vec`（已由 `langgraph-checkpoint-sqlite` 声明）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, cast

from langgraph.store.base import BaseStore
from langgraph.store.sqlite.aio import AsyncSqliteStore
from sqlalchemy.engine import make_url

from pyassist.config import resolve_sqlite_path, settings, to_psycopg_conninfo
from pyassist.logging import get_logger

logger = get_logger(__name__)


class _HasSetup(Protocol):
    """`setup()` 存在于每个具体 Store 类上，但不在 `BaseStore` 基类上。"""

    async def setup(self) -> None: ...


@asynccontextmanager
async def open_store(*, database_url: str | None = None) -> AsyncIterator[BaseStore]:
    """打开唯一的长期记忆 Store。与检查点指向**同一个库**（§2）。"""
    resolved = database_url or settings.database_url
    backend = make_url(resolved).get_backend_name()

    if backend == "sqlite":
        path = resolve_sqlite_path(resolved)
        async with AsyncSqliteStore.from_conn_string(str(path), index=None) as store:
            logger.info("store.opened", backend="sqlite", db_path=str(path))
            yield store
        return

    if backend == "postgresql":
        # 函数内 import，理由同 `agent/checkpointer.py`（psycopg → libpq）。
        from langgraph.store.postgres.aio import AsyncPostgresStore

        async with AsyncPostgresStore.from_conn_string(
            to_psycopg_conninfo(resolved), index=None
        ) as store:
            logger.info("store.opened", backend="postgresql")
            yield store
        return

    raise ValueError(f"不支持的数据库后端：{backend!r}（§2 只允许 SQLite / PostgreSQL）")


async def setup_store(*, database_url: str | None = None) -> None:
    """建 Store 的表。**只给迁移脚本调用**。幂等，语义同 `setup_checkpointer`。"""
    async with open_store(database_url=database_url) as store:
        await cast("_HasSetup", store).setup()
