"""引擎与 Session（SPEC §2）。

**本文件不含任何方言字样。** 方言相关的连接参数由 `config.build_engine_kwargs()`
以数据形式给出，本文件只装配 —— 于是"不得出现方言分支"从约定变成可断言的
事实（`tests/test_contract_guardrails.py`）。

引擎**不做模块级单例**：`create_async_engine` 本身是惰性的，但单例在测试里要
额外重置；需要长生命周期时由调用方持有（阶段四的 FastAPI `lifespan`）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pyassist.config import build_engine_kwargs, settings


def create_engine(database_url: str | None = None) -> AsyncEngine:
    """建一个异步引擎。不传 URL 则用 `settings.database_url`。

    传参是给测试留的缝（照 `llm.client.get_chat_model(api_key=...)` 的做法）：
    **构造引擎这个动作在物理上只有这一处**，测试不必自己 new 一个。
    """
    url = database_url or settings.database_url
    return create_async_engine(url, **build_engine_kwargs(url))


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """`expire_on_commit=False`：提交后还要读对象属性是常态（阶段四/六），
    默认的过期会在 async 场景里抛 `MissingGreenlet`。"""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def open_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """一次业务操作一个 Session，退出即关闭。阶段四的依赖注入会包一层它。"""
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session


@asynccontextmanager
async def open_engine(database_url: str | None = None) -> AsyncIterator[AsyncEngine]:
    """建一个引擎并在退出时 dispose。**FastAPI lifespan 专用**。

    存在的理由：契约守护 #11 把引擎工厂的调用点钉在 db/session.py 一处，
    于是"谁持有引擎的生命周期"这件事也必须在这里回答 —— api/main.py 不能自己建。
    名字刻意不叫 `create_engine`（那是上面工厂的名字），但**它在同一个文件里**，
    "引擎只有一个家"依然成立。本文件不得出现方言字样（守护 #12），故此处不提后端名。
    """
    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        await engine.dispose()
