"""阶段三的边界守护（SPEC §2）。

三条断言围绕同一件事：**对话历史只有一个家，业务记录不重复存对话**。
其中"检查点目标 == 业务表目标 == data/app.db"是**程序化**的 —— 不是读常量比对，
而是开一条真实连接，问数据库自己"你是哪个文件"。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from sqlalchemy.engine import make_url

import pyassist
from pyassist.agent.checkpointer import open_checkpointer, setup_checkpointer
from pyassist.agent.store import setup_store
from pyassist.config import DB_PATH, settings
from pyassist.db import models
from pyassist.db.session import create_engine


def _same_file(reported: str, expected: Path) -> bool:
    """同步的路径比较助手：`ASYNC240` 不希望 async 函数体内出现阻塞式 pathlib 调用。"""
    return Path(reported).resolve() == expected.resolve()


def test_settings_url_points_at_the_single_db_path() -> None:
    """URL 与 `DB_PATH` 是同一个文件 —— 路径常量只有一个。"""
    url = make_url(settings.database_url)
    assert url.get_backend_name() == "sqlite"  # 开发库
    assert Path(url.database or "").resolve() == DB_PATH.resolve()


def test_engine_resolves_to_the_same_file() -> None:
    """`db/session.py` 的引擎指向同一个文件。"""
    engine = create_engine()
    assert Path(make_url(engine.url).database or "").resolve() == DB_PATH.resolve()


async def test_live_checkpointer_connection_is_the_same_file() -> None:
    """**关键一步**：问活的检查点连接"你的 main 库是哪个文件"。

    这是"程序化断言，不是靠约定"的落点 —— 常量比对只能证明"我们写了同一个字符串"，
    证明不了连接真的开在那。
    """
    async with open_checkpointer() as saver:
        # 测试不参与 mypy --strict（只管 src/pyassist）；这里刻意伸手到 saver.conn，
        # 因为 AsyncSqliteSaver 的公开属性就是它。
        cursor = await saver.conn.execute(
            "SELECT file FROM pragma_database_list WHERE name = 'main'"
        )
        row = await cursor.fetchone()
        await cursor.close()
    assert row is not None
    assert _same_file(row[0], DB_PATH)


async def test_business_and_checkpoint_tables_live_in_one_file(tmp_path: Path) -> None:
    """业务表、检查点表、Store 表三者都在**同一个文件**里（ROADMAP 的"迁移后检查表结构"）。

    用 tmp 库而不是真实 `data/app.db`，避免测试互相污染、也不 require 迁移先跑过。
    """
    url = f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    await setup_checkpointer(database_url=url)
    await setup_store(database_url=url)

    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
        names = {row[0] for row in rows}
    await engine.dispose()

    assert {"users", "sessions", "exercise_attempts", "mistakes", "token_ledger"} <= names
    assert {"checkpoints", "writes"} <= names
    assert "store" in names  # AsyncSqliteStore.setup() 建的 KV 表


def test_models_declare_no_message_shaped_table() -> None:
    """§2：不在业务表里再存一份对话消息。

    不只查表名 —— 也查形状：没有任何表同时具备 role/content/tool_calls 这三列。
    """
    tables = models.Base.metadata.tables
    assert "messages" not in tables
    for table in tables.values():
        columns = set(table.columns.keys())
        assert not {"role", "content", "tool_calls"} <= columns, table.name


def test_memory_saver_is_not_reachable_from_pyassist() -> None:
    """§2：`MemorySaver` 重启即丢，从 `src/pyassist` 里 import 不到（ROADMAP 的原文要求）。"""
    for module_info in pkgutil.walk_packages(pyassist.__path__, prefix="pyassist."):
        module = importlib.import_module(module_info.name)
        assert not hasattr(module, "MemorySaver"), module_info.name
