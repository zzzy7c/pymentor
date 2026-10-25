#!/usr/bin/env python
"""删除过期 LangGraph 检查点 thread（SPEC §5.16 待定项 11）。

**默认只统计、不删除。** 加 `--apply` 才真的删。

    uv run python scripts/prune_threads.py --days 30
    uv run python scripts/prune_threads.py --days 30 --apply

为什么是手动脚本而不是自动清理：检查点与业务表（提交、错题）之间**没有外键**，
"哪些 thread 可以删"需要对业务语义负责（例如还在被错题引用的 exercise_id）。
自动清理一旦删错，历史回放就永久缺一段；手动脚本至少留了一次确认。

只动 LangGraph 的检查点表，不碰业务表，也不碰长期记忆 Store。

这里直接写 SQL 是**刻意**的：检查点表由 LangGraph 的 saver 拥有，`db/repositories.py`
从不碰它们；为了这个脚本在 repositories 里开一个"删任意 thread"的入口，反而会把
业务层与框架 schema 绑在一起。表的读写仍然只经 saver 之外的这一处运维脚本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from pyassist.config import settings
from pyassist.db.session import open_engine

# 两套 schema：SQLite 是 `writes`，PostgreSQL 是 `checkpoint_writes` + `checkpoint_blobs`。
# 表名是常量，但**写成整条语句**而不是运行时拼表名：S608 无法证明拼接安全，
# 而"让静态检查闭嘴"的正确方式消掉拼接，不是加 noqa。
_DELETE_STATEMENTS: dict[str, str] = {
    "writes": "DELETE FROM writes WHERE thread_id = :thread_id",
    "checkpoint_writes": "DELETE FROM checkpoint_writes WHERE thread_id = :thread_id",
    "checkpoint_blobs": "DELETE FROM checkpoint_blobs WHERE thread_id = :thread_id",
    "checkpoints": "DELETE FROM checkpoints WHERE thread_id = :thread_id",
}


@dataclass(frozen=True, slots=True)
class _Thread:
    thread_id: str
    last_seen: datetime


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


_SERDE = JsonPlusSerializer()
"""检查点列不是纯 JSON：实测 LangGraph 1.x 写的是 msgpack（`type='msgpack'`），
按 `json.loads` 解析会得到 `threads=0`，脚本看起来"没数据"却什么都不删。
必须用写入方自己的序列化器解码。"""


def _checkpoint_value(kind: Any, raw: Any) -> dict[str, Any] | None:
    """解码 `checkpoints.checkpoint`。

    SQLite 给 BLOB + `type='msgpack'`；PostgreSQL 的 JSONB 可能已经反序列化成 dict。
    旧的 TEXT/JSON 行保留一条回退路径 —— 升级前后的库都可能被这个脚本碰到。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(kind, str) and isinstance(raw, (bytes, bytearray, memoryview)):
        decoded = _SERDE.loads_typed((kind, bytes(raw)))
        return decoded if isinstance(decoded, dict) else None
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def _table_names(connection: AsyncConnection) -> set[str]:
    return await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))


async def _load_threads(connection: AsyncConnection) -> list[_Thread]:
    rows = (
        await connection.execute(text("SELECT thread_id, type, checkpoint FROM checkpoints"))
    ).all()
    latest: dict[str, datetime] = {}
    for thread_id, kind, raw in rows:
        if not isinstance(thread_id, str):
            continue
        checkpoint = _checkpoint_value(kind, raw)
        if checkpoint is None:
            continue
        seen = _parse_timestamp(checkpoint.get("ts"))
        if seen is None:
            continue
        current = latest.get(thread_id)
        if current is None or seen > current:
            latest[thread_id] = seen
    return [_Thread(thread_id, last_seen) for thread_id, last_seen in latest.items()]


def _expired(threads: Iterable[_Thread], *, days: int, now: datetime) -> list[_Thread]:
    cutoff = now - timedelta(days=days)
    return [thread for thread in threads if thread.last_seen < cutoff]


async def _delete(connection: AsyncConnection, thread_ids: list[str], tables: set[str]) -> None:
    statements = [statement for table, statement in _DELETE_STATEMENTS.items() if table in tables]
    for thread_id in thread_ids:
        for statement in statements:
            await connection.execute(text(statement), {"thread_id": thread_id})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/prune_threads.py",
        description="删除超过 N 天没有更新的 LangGraph thread（默认 dry-run）",
    )
    parser.add_argument("--days", type=int, default=30, help="保留天数（默认 30）")
    parser.add_argument("--apply", action="store_true", help="真的删除；不加只统计")
    parser.add_argument(
        "--database-url",
        default=None,
        help="默认用 settings.database_url；检查点与业务库必须同一个库",
    )
    return parser


async def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.days < 0:
        print("--days 不能为负数", file=sys.stderr)
        return 2

    database_url = args.database_url or settings.database_url
    async with open_engine(database_url) as engine, engine.connect() as connection:
        tables = await _table_names(connection)
        if "checkpoints" not in tables:
            print("threads=0 expired=0 （库里没有检查点表）")
            return 0

        threads = await _load_threads(connection)
        expired = _expired(threads, days=args.days, now=datetime.now(UTC))
        print(f"threads={len(threads)} expired={len(expired)} days={args.days}")
        if not expired or not args.apply:
            if expired and not args.apply:
                print("未删除任何数据；确认后加 --apply。")
            return 0

        await _delete(connection, [thread.thread_id for thread in expired], tables)
        await connection.commit()
        print(f"deleted_threads={len(expired)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
