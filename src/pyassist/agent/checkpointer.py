"""★ 唯一检查点构造点（SPEC §2）。

开发：`AsyncSqliteSaver` → `data/app.db`（与业务表**同一个文件**）。
生产：`AsyncPostgresSaver` → 同一个 PG 实例。**禁止 `MemorySaver`**（重启即丢）。

为什么是异步类而不是 ROADMAP 字面写的 `SqliteSaver`：
同步的 `SqliteSaver` 会在 async 路由里阻塞事件循环，而 §3.4 要求 DB 路径 async。
两类的表结构完全相同，只是接口。详见 SPEC §5「与 ROADMAP 验收的差异」第 1 条。

为什么是 `@asynccontextmanager` 而不是模块级单例：
`AsyncSqliteSaver.__init__` 会调 `asyncio.get_running_loop()`，import 期构造直接抛。
构造点只能在**运行中的事件循环内**进入 —— 阶段四的 FastAPI `lifespan` 是它唯一的
长生命周期宿主。

Postgres 类是**函数内 import**：它们会拉进 psycopg，而 psycopg 的二进制轮子带
libpq。放在模块级，一台没有 libpq 的机器上连 SQLite 路径与全部测试都会 import 失败
—— 一个开发期用不到的后端不该有这个能力。SQLite 类留在模块级（它是开发期的主路径，
早失败更早发现）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy.engine import make_url

from pyassist.config import resolve_sqlite_path, settings, to_psycopg_conninfo
from pyassist.errors import ConfigError
from pyassist.logging import get_logger

logger = get_logger(__name__)


class _HasSetup(Protocol):
    """`setup()` 存在于每个具体检查点类上，但不在 `BaseCheckpointSaver` 基类上。"""

    async def setup(self) -> None: ...


@asynccontextmanager
async def open_checkpointer(
    *, database_url: str | None = None
) -> AsyncIterator[BaseCheckpointSaver[str]]:
    """打开唯一的检查点。不传 URL 则用 `settings.database_url`。

    `database_url` 是给测试留的缝：测试用 `tmp_path`，绝不污染真实的 `data/app.db`。

    **不在这里调 `setup()`** —— §2 明确"不要在请求路径里调"。SQLite 侧上游会在
    首次使用时懒调用（幂等，无害）；PG 侧由迁移（`migrations/env.py`）保证已建表。
    """
    resolved = database_url or settings.database_url
    backend = make_url(resolved).get_backend_name()

    if backend == "sqlite":
        path = resolve_sqlite_path(resolved)
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            logger.info("checkpointer.opened", backend="sqlite", db_path=str(path))
            yield saver
        return

    if backend == "postgresql":
        # **函数内 import**，不是模块级：`langgraph.checkpoint.postgres.aio` 会拉进
        # psycopg / psycopg_pool，而 psycopg 的二进制轮子带 libpq。放在模块级，
        # 一台没有 libpq 的机器上连 SQLite 路径和**全部测试**都会 import 失败 ——
        # 一个开发期根本用不到的后端，不该有能力拖垮开发环境。
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(to_psycopg_conninfo(resolved)) as saver:
            logger.info("checkpointer.opened", backend="postgresql")
            yield saver
        return

    # 服务级配置问题（库后端不认识）→ ConfigError（阶段四 T7 收口）。
    raise ConfigError(f"不支持的数据库后端：{backend!r}（§2 只允许 SQLite / PostgreSQL）")


async def setup_checkpointer(*, database_url: str | None = None) -> None:
    """建检查点表。**只给迁移脚本调用**（§2：不在请求路径里调）。

    幂等（`is_setup` 标志 + `CREATE TABLE IF NOT EXISTS`），所以"迁移跑一次、
    运行时上游再懒调用"不会撞表，不必额外去重。
    """
    async with open_checkpointer(database_url=database_url) as saver:
        await cast("_HasSetup", saver).setup()
