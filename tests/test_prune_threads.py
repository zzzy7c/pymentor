"""`scripts/prune_threads.py`：默认 dry-run，`--apply` 才删，且只删过期 thread。

脚本不是包的一部分，用 `spec_from_file_location` 加载；它只碰检查点表，
测试因此直接建两张最小的 sqlite 表（`checkpoints` / `writes`），不起 LangGraph。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prune_threads.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("prune_threads", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses 在 exec 期间要靠 sys.modules[cls.__module__] 解析注解，先登记。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_SCHEMA = """
CREATE TABLE checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint BLOB,
    metadata BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    value BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
"""


_SERDE = JsonPlusSerializer()


def _insert_checkpoint(
    connection: sqlite3.Connection, thread_id: str, checkpoint_id: str, seen: datetime
) -> None:
    # 真实 LangGraph 1.x 写的是 (type='msgpack', BLOB)，不是 JSON 文本。
    kind, blob = _SERDE.dumps_typed({"ts": seen.isoformat()})
    connection.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_id, type, checkpoint) VALUES (?, ?, ?, ?)",
        (thread_id, checkpoint_id, kind, blob),
    )


def _seed(path: Path) -> None:
    now = datetime.now(UTC)
    rows = {
        "old": now - timedelta(days=90),
        "fresh": now - timedelta(days=1),
        # 同一 thread 有两条：只要最新一条在保留期内就不该删。
        "mixed": now - timedelta(days=90),
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)
        for thread_id, seen in rows.items():
            _insert_checkpoint(connection, thread_id, f"cp-{thread_id}", seen)
            connection.execute(
                "INSERT INTO writes (thread_id, checkpoint_id, task_id, idx, channel) "
                "VALUES (?, ?, 't', 0, 'messages')",
                (thread_id, f"cp-{thread_id}"),
            )
        _insert_checkpoint(connection, "mixed", "cp-mixed-new", now)


def _threads(path: Path, table: str) -> set[str]:
    # 表名只来自本文件的字面量；写成两条语句是为了不让 S608 依赖"人肉确认"。
    if table == "checkpoints":
        query = "SELECT DISTINCT thread_id FROM checkpoints"
    elif table == "writes":
        query = "SELECT DISTINCT thread_id FROM writes"
    else:
        raise AssertionError(f"未知表：{table}")
    with sqlite3.connect(path) as connection:
        return {row[0] for row in connection.execute(query)}


def test_dry_run_reports_but_does_not_delete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "app.db"
    _seed(database)
    module = _load_script()

    exit_code = asyncio.run(
        module.main(
            ["--days", "30", "--database-url", f"sqlite+aiosqlite:///{database.as_posix()}"]
        )
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "threads=3 expired=1" in output
    assert "未删除任何数据" in output
    assert _threads(database, "checkpoints") == {"old", "fresh", "mixed"}


def test_apply_deletes_only_expired_threads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "app.db"
    _seed(database)
    module = _load_script()

    exit_code = asyncio.run(
        module.main(
            [
                "--days",
                "30",
                "--apply",
                "--database-url",
                f"sqlite+aiosqlite:///{database.as_posix()}",
            ]
        )
    )

    assert exit_code == 0
    assert "deleted_threads=1" in capsys.readouterr().out
    assert _threads(database, "checkpoints") == {"fresh", "mixed"}
    assert _threads(database, "writes") == {"fresh", "mixed"}


def test_missing_checkpoint_table_is_zero_not_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "empty.db"
    sqlite3.connect(database).close()
    module = _load_script()

    exit_code = asyncio.run(
        module.main(["--database-url", f"sqlite+aiosqlite:///{database.as_posix()}"])
    )

    assert exit_code == 0
    assert "库里没有检查点表" in capsys.readouterr().out
