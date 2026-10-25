"""依赖注入缝。

阶段七的沙箱假实现在这里替换；阶段六的图节点在这里取 session。
本阶段只把"从请求拿到资源"的路径定死，不引入任何业务依赖。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request
from langgraph.store.base import BaseStore
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pyassist.agent.graphs.diagnose import DiagnoseGraph
from pyassist.agent.graphs.exercise import ExerciseGraphs
from pyassist.agent.graphs.progress import ProgressGraph
from pyassist.agent.graphs.qa import QAGraph
from pyassist.api.schemas import Meta, Usage
from pyassist.config import DEFAULT_USER_ID, settings
from pyassist.db import repositories
from pyassist.db.session import open_session
from pyassist.errors import ConfigError
from pyassist.sandbox.runner import SandboxRunner
from pyassist.services.usage import assert_daily_quota


@dataclass(frozen=True, slots=True)
class RequestContext:
    """中间件塞进 request.state 的东西。处理器与路由都从这里取。"""

    request_id: str
    started_at: float

    @property
    def latency_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)


def get_request_context(request: Request) -> RequestContext:
    ctx = getattr(request.state, "context", None)
    if not isinstance(ctx, RequestContext):
        # 未经过中间件（例如直接单测处理器）：给一个自洽的降级，而不是崩。
        return RequestContext(request_id="unknown", started_at=time.perf_counter())
    return ctx


def build_meta(request: Request, *, usage: Usage | None = None) -> Meta:
    """路由构造成功信封用的入口。usage 缺省时填**零值**，不省略字段（§3.1）。"""
    ctx = get_request_context(request)
    return Meta(request_id=ctx.request_id, latency_ms=ctx.latency_ms, usage=usage or Usage())


def get_engine(request: Request) -> AsyncEngine:
    """从 app.state 取引擎。引擎**不在**这里构造（守护 #11 把它钉在 db/session.py）。"""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise RuntimeError("引擎未初始化：lifespan 未运行（TestClient 必须是上下文管理器）")
    return cast("AsyncEngine", engine)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """请求级 AsyncSession。每次请求一个，退出即关。"""
    async with open_session(get_engine(request)) as session:
        yield session


def get_qa_graph(request: Request) -> QAGraph:
    """取 lifespan 编译好的图。**不在这里现编一个**（§2 单点所有权）。

    返回的是 `QAGraph(graph, preflight)` 这一整个对象，不是裸的 `CompiledStateGraph` ——
    预检与图必须共用同一组依赖（`search`/`fetch`/`preflight` 三件套），
    路由拿不到那个依赖就只能猜，而猜就是"生产对、测试错"的来源（SPEC D11）。

    `None` 的唯一来源是"没有 API key，图没编译"：那不是 404 也不是 503，
    而是本服务缺配置 → `ConfigError` → 500 信封（与 `/health` 仍可用并存）。
    """
    qa = getattr(request.app.state, "qa_graph", None)
    if qa is None:
        raise ConfigError("问答图不可用：DEEPSEEK_API_KEY 未配置或图未编译")
    return cast("QAGraph", qa)


def get_sandbox(request: Request) -> SandboxRunner:
    """取 lifespan 建好的沙箱 runner。**缺失即 ConfigError**（与 `get_qa_graph` 同一套写法）。

    `/diagnose` 先调它再调 `get_diagnose_graph`：沙箱不可用时给出一条明确的
    "本机没装 Docker / 镜像缺失"，而不是等模型已经花了钱、走到 `verify` 才报错。
    """
    runner = getattr(request.app.state, "sandbox", None)
    if runner is None:
        raise ConfigError("沙箱不可用：Docker daemon 未启动或沙箱镜像缺失")
    return cast("SandboxRunner", runner)


def get_diagnose_graph(request: Request) -> DiagnoseGraph:
    """取 lifespan 编译好的诊断图。与 `get_qa_graph` 同构：不在这里现编。"""
    dg = getattr(request.app.state, "diagnose_graph", None)
    if dg is None:
        raise ConfigError("诊断图不可用：DEEPSEEK_API_KEY 未配置、沙箱不可用或图未编译")
    return cast("DiagnoseGraph", dg)


def get_exercise_graphs(request: Request) -> ExerciseGraphs:
    """取 lifespan 编译好的练习图（生成 + 批改），**不在这里现编**。"""
    graphs = getattr(request.app.state, "exercise_graphs", None)
    if graphs is None:
        raise ConfigError("练习图不可用：DEEPSEEK_API_KEY 未配置、沙箱不可用或图未编译")
    return cast("ExerciseGraphs", graphs)


def get_progress_graph(request: Request) -> ProgressGraph:
    """取 lifespan 编译好的推荐图。与其它三张图同构：**不在这里现编**。"""
    progress = getattr(request.app.state, "progress_graph", None)
    if progress is None:
        raise ConfigError("推荐图不可用：DEEPSEEK_API_KEY 未配置或图未编译")
    return cast("ProgressGraph", progress)


def get_store(request: Request) -> BaseStore:
    """取 lifespan 打开的长期记忆 Store。缺失即 `ConfigError`（与图同一套写法）。"""
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise ConfigError("长期记忆 Store 不可用：lifespan 未完成或数据库后端不支持")
    return cast("BaseStore", store)


async def require_quota(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> None:
    """router 级配额闸门（D8）：匿名请求也先建档，再查当日账本。

    **软上限**：`assert → 跑模型 → 记账` 不是事务，并发请求可能同时通过检查；
    真正的硬闸门是 slowapi 速率限制。这里不维护第二份计数器。

    建档后立刻提交：检查点走另一条连接写同一个库，业务事务挂着写锁跑图会死等
    `database is locked`（见 `services/exercise.py` 同一处注释）。
    """
    user_id = _user_id(request)
    await repositories.ensure_user(session, user_id=user_id)
    await repositories.commit(session)
    await assert_daily_quota(session, user_id=user_id, limit=settings.daily_token_limit)
    # 释放检查读事务：SQLite 在 rollback-journal 模式下读快照会挡住写提交。
    await repositories.commit(session)


def _user_id(request: Request) -> str:
    """D9：固定用户。将来接鉴权时只改这一处（及 `get_request_context` 的绑定）。"""
    return cast("str", getattr(request.state, "user_id", DEFAULT_USER_ID))
