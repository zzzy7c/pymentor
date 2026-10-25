"""应用工厂。

**唯一**构造 FastAPI 的地方（守护 #18）。本阶段不 include 任何业务 router —— /health 用
APIRouter 定义在本文件里，作为"路由怎么写"的样板与守护的锚点。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, Request, Response
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from slowapi import Limiter
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from pyassist.agent.checkpointer import open_checkpointer
from pyassist.agent.graphs.diagnose import DiagnoseGraph, build_diagnose_graph
from pyassist.agent.graphs.exercise import ExerciseGraphs, build_exercise_graph
from pyassist.agent.graphs.progress import ProgressGraph, build_progress_graph
from pyassist.agent.graphs.qa import QAGraph, build_qa_graph
from pyassist.agent.store import open_store
from pyassist.agent.tools.docs import Fetcher, Searcher
from pyassist.api.deps import RequestContext, build_meta
from pyassist.api.errors import register_exception_handlers
from pyassist.api.routers.chat import router as chat_router
from pyassist.api.routers.diagnose import router as diagnose_router
from pyassist.api.routers.exercises import router as exercises_router
from pyassist.api.routers.history import router as history_router
from pyassist.api.routers.knowledge import router as knowledge_router
from pyassist.api.routers.progress import router as progress_router
from pyassist.api.routers.stats import router as stats_router
from pyassist.api.schemas import API_VERSION, Envelope, HealthData, envelope_success
from pyassist.config import settings
from pyassist.db.repositories import ping
from pyassist.db.session import open_engine, open_session
from pyassist.errors import ConfigError
from pyassist.llm.client import check_models, get_last_model_check
from pyassist.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
)
from pyassist.sandbox.runner import SandboxRunner, build_sandbox_runner

logger = get_logger(__name__)

# 限流数值：模块常量而非 Settings —— 它不是部署差异，是接口策略（待定项 6）。
DEFAULT_RATE_LIMIT = "120/minute"

_health_router = APIRouter()


@_health_router.get("/health", response_model=Envelope[HealthData])
async def health(request: Request) -> Envelope[HealthData]:
    """健康检查。**永远返回 200**，用 data.status 表达降级（ADR 0001：模型名漂移是告警）。

    数据库不可用时**不是 500** —— 否则编排器会把"上游模型漂移"当成"服务挂了"。
    """
    database_ok = False
    try:
        async with open_session(request.app.state.engine) as session:
            database_ok = await ping(session)
    except Exception:
        # 探活的全部意义就是"连不上也要给出信封"：这里捕获大类是**有意的**，
        # 且函数体是"置降级 + 打点"，不是 pass（守护 #19 拦的是后者）。
        logger.warning("health.db_unreachable")

    check = get_last_model_check()
    model_ok = None if check is None else check.is_ok
    # 沙箱不可用**不影响 v1**：只把整体状态降级，`/chat` 与 `/knowledge` 照常可用。
    # 真正的 500 留给 `/diagnose` 自己（`get_sandbox` → ConfigError）。
    sandbox_ok = getattr(request.app.state, "sandbox", None) is not None
    is_ok = database_ok and model_ok is not False and sandbox_ok
    data = HealthData(
        status="ok" if is_ok else "degraded",
        api_version=API_VERSION,
        database_ok=database_ok,
        model_check_ok=model_ok,
        checked_at=datetime.now(UTC).isoformat(),
        sandbox_ok=sandbox_ok,
    )
    return envelope_success(data, meta=build_meta(request))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程级资源的唯一开合点。

    顺序（SPEC D11）：`configure_logging` → `open_engine` → `open_checkpointer` →
    `build_qa_graph` → `yield` → **逆序关闭**（`async with` 天然保证）。

    `open_checkpointer` 必须在 lifespan 内：`AsyncSqliteSaver.__init__` 要 running loop。
    图在这里**只编译一次**：每次请求现编会让 guardrail/预检各拿到一套依赖（D11）。
    """
    configure_logging(settings.log_level, json_output=settings.log_json)
    async with open_engine(app.state.database_url) as engine:
        app.state.engine = engine
        async with open_checkpointer(database_url=app.state.database_url) as saver:
            app.state.checkpointer = saver
            # Store 与检查点指向**同一个库**（§2）：存储构造点只有一个。
            # 测试可注入内存 Store；生产一律走 `agent/store.py`。
            store_context = (
                _null_store_context(app.state.injected_store)
                if app.state.injected_store is not None
                else open_store(database_url=app.state.database_url)
            )
            async with store_context as store:
                app.state.store = store
                # 沙箱在两张图**之前**建：诊断图编译时就要拿到 runner（缺了直接 ConfigError）。
                # 注入的 fake 优先 —— 测试绝不碰 docker。探测失败只返回 None，不中断启动。
                app.state.sandbox = app.state.sandbox_runner or build_sandbox_runner()
                # 没有 API key 时**不崩**：`/health` 仍要可用（ADR 0001 的同一取舍）。
                app.state.qa_graph = _build_graph_if_possible(app, saver)
                app.state.diagnose_graph = _build_diagnose_graph_if_possible(app, saver)
                app.state.exercise_graphs = _build_exercise_graphs_if_possible(app, saver)
                app.state.progress_graph = _build_progress_graph_if_possible(app, saver)
                logger.info(
                    "api.startup",
                    api_version=API_VERSION,
                    qa_graph=app.state.qa_graph is not None,
                    diagnose_graph=app.state.diagnose_graph is not None,
                    exercise_graphs=app.state.exercise_graphs is not None,
                    progress_graph=app.state.progress_graph is not None,
                    sandbox=app.state.sandbox is not None,
                )
                check_task = (
                    asyncio.create_task(_verify_models_background())
                    if app.state.verify_models
                    else None
                )
                try:
                    yield
                finally:
                    if check_task is not None and not check_task.done():
                        check_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await check_task
    logger.info("api.shutdown")


async def _verify_models_background() -> None:
    """启动时校验模型名，**不阻塞启动**（SPEC §9 第 5 条）。

    `check_models()` 是同步网络调用（10s 超时）；放进线程再套 15s 外层超时，
    一次上游抖动最多让这个后台任务失败，不会让 uvicorn 迟迟不 listen。
    结果写进模块级变量，`/health` 读 `get_last_model_check()`。
    """
    try:
        await asyncio.wait_for(asyncio.to_thread(check_models), timeout=15)
    except TimeoutError:
        logger.error("llm.models_check.timeout", timeout_seconds=15)
    except Exception as exc:
        # 后台任务没有调用方能接异常；吞掉会让"为什么 /health 里没有检查结果"变成谜。
        logger.error("llm.models_check.failed", error=type(exc).__name__)


def create_app(
    *,
    database_url: str | None = None,
    rate_limit_enabled: bool = True,
    chat_model: BaseChatModel | None = None,
    diagnose_model: BaseChatModel | None = None,
    sandbox_runner: SandboxRunner | None = None,
    search: Searcher | None = None,
    fetch: Fetcher | None = None,
    preflight: Callable[[], Awaitable[None]] | None = None,
    exercise_model: BaseChatModel | None = None,
    grading_model: BaseChatModel | None = None,
    progress_model: BaseChatModel | None = None,
    store: BaseStore | None = None,
    verify_models: bool = False,
) -> FastAPI:
    app = FastAPI(title="pyassist", version=API_VERSION, lifespan=lifespan)
    app.state.database_url = database_url or settings.database_url
    # 注入参数**全部默认 None**，生产路径一行代码都不走测试分支（D11）。
    # 它们原样转交给两张图的构建函数；三个可调用对象必须**成套**给（半套在构图时报错）。
    app.state.chat_model = chat_model
    app.state.diagnose_model = diagnose_model
    app.state.sandbox_runner = sandbox_runner
    app.state.search = search
    app.state.fetch = fetch
    app.state.preflight = preflight
    app.state.exercise_model = exercise_model
    app.state.grading_model = grading_model
    app.state.progress_model = progress_model
    app.state.injected_store = store
    # **默认关闭**：测试里 `create_app()` 会进 lifespan，开着就等于每个测试都打一次
    # 真实 `models.list()`，违反 §5.1"测试不触网"。生产入口显式开（见文件末尾）。
    app.state.verify_models = verify_models

    # 1) 处理器：在**任何请求发生前**注册完。
    register_exception_handlers(app)

    # 2) 限流：slowapi 不接收 app 参数，走 app.state + add_middleware 三步。
    #    它查找的 RateLimitExceeded 处理器就是第 1 步注册的那个（同步的，见 M6）。
    limiter = Limiter(
        key_func=get_remote_address,
        default_limits=[DEFAULT_RATE_LIMIT],
        enabled=rate_limit_enabled,
    )
    app.state.limiter = limiter

    # 3) 中间件 onion：add_middleware 是 insert(0)，**后加的在外层**（M2）。
    #    先加 slowapi（内层），后加 request_id（外层）—— 外层才能给限流的 429 也盖上 id。
    app.add_middleware(SlowAPIMiddleware)
    app.middleware("http")(_request_context_middleware)

    # 4) 挂路由。**这里刻意不用 `app.include_router(...)`**（偏离 SPEC §4.5 草案，
    #    理由见 ADR 0002「施工中发现」）：
    #    fastapi 0.142 把 include_router 的产物包成一个 `_IncludedRouter` 塞进 `app.routes`，
    #    它**没有 `.endpoint` 属性**；而 slowapi 0.1.10 的 `_find_route_handler` 只认
    #    `Match.FULL and hasattr(route, "endpoint")` 的路由 —— 于是它取不到 handler，
    #    `_should_exempt` 返回 True，**默认限流对所有经 include_router 挂载的路由静默失效**。
    #    直接并入路由表是 0.142 之前 include_router 的等价行为：路由可达、进 OpenAPI、
    #    且 slowapi 能看到 `.endpoint`。`test_rate_limited_end_to_end` 把这条钉住。
    #    **阶段六新增业务 router 时必须沿用这个挂载方式**，否则限流会静默失效。
    app.router.routes.extend(_health_router.routes)
    app.router.routes.extend(chat_router.routes)  # ← 阶段六
    app.router.routes.extend(knowledge_router.routes)  # ← 阶段六
    app.router.routes.extend(diagnose_router.routes)  # ← 阶段七（同样不能 include_router）
    app.router.routes.extend(exercises_router.routes)  # ← 阶段八 8A
    app.router.routes.extend(stats_router.routes)  # ← 阶段八 8B
    app.router.routes.extend(history_router.routes)  # ← 阶段八 8B
    app.router.routes.extend(progress_router.routes)  # ← 阶段八 8C
    return app


@asynccontextmanager
async def _null_store_context(store: BaseStore) -> AsyncIterator[BaseStore]:
    """把测试注入的 Store 包成 lifespan 可 `async with` 的形状（生命周期归调用方）。

    生产路径**不走这里**：它只会拿到 `agent/store.py::open_store` 打开的那个。
    """
    yield store


def _build_graph_if_possible(app: FastAPI, saver: BaseCheckpointSaver[str]) -> QAGraph | None:
    """编译问答图。**只捕 `ConfigError`**（缺 key），其它异常照常上抛。

    捕 `Exception` 会让"图写错了"表现成"问答不可用"，而这是最该在启动时就炸的错。
    """
    try:
        return build_qa_graph(
            checkpointer=saver,
            model=app.state.chat_model,
            search=app.state.search,
            fetch=app.state.fetch,
            preflight=app.state.preflight,
        )
    except ConfigError:
        logger.warning("api.qa_graph.unavailable")
        return None


def _build_diagnose_graph_if_possible(
    app: FastAPI, saver: BaseCheckpointSaver[str]
) -> DiagnoseGraph | None:
    """编译诊断图。三种"不可用"都落到 `ConfigError`：缺 key、缺沙箱、依赖半套。

    与 qa 一样**只捕 `ConfigError`**：图本身的编程错误必须在启动时炸出来。
    """
    if app.state.sandbox is None:
        logger.warning("diagnose.unavailable", reason="sandbox")
        return None
    try:
        return build_diagnose_graph(
            checkpointer=saver,
            model=app.state.diagnose_model,
            runner=app.state.sandbox,
            search=app.state.search,
            fetch=app.state.fetch,
            preflight=app.state.preflight,
        )
    except ConfigError:
        logger.warning("diagnose.unavailable", reason="config")
        return None


def _build_exercise_graphs_if_possible(
    app: FastAPI, saver: BaseCheckpointSaver[str]
) -> ExerciseGraphs | None:
    """编译练习图。缺 key / 缺沙箱 / 依赖半套都落到 `ConfigError`。"""
    if app.state.sandbox is None:
        logger.warning("exercise.unavailable", reason="sandbox")
        return None
    try:
        return build_exercise_graph(
            checkpointer=saver,
            model=app.state.exercise_model,
            grading_model=app.state.grading_model,
            runner=app.state.sandbox,
            search=app.state.search,
        )
    except ConfigError:
        logger.warning("exercise.unavailable", reason="config")
        return None


def _build_progress_graph_if_possible(
    app: FastAPI, saver: BaseCheckpointSaver[str]
) -> ProgressGraph | None:
    """编译推荐图。不依赖沙箱，只需要模型与检索三件套。"""
    try:
        return build_progress_graph(
            checkpointer=saver,
            model=app.state.progress_model,
            search=app.state.search,
            fetch=app.state.fetch,
            preflight=app.state.preflight,
        )
    except ConfigError:
        logger.warning("progress.unavailable", reason="config")
        return None


async def _request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """给每个请求发一个 request_id，绑定日志上下文，并在响应头回显。

    进出一律清理 contextvars：并发请求共享进程，残留会串台（M5 的另一面）。
    """
    request_id = _normalize_request_id(request.headers.get("X-Request-ID"))
    started_at = time.perf_counter()
    request.state.request_id = request_id
    request.state.started_at = started_at
    request.state.context = RequestContext(request_id=request_id, started_at=started_at)
    bind_request_context(request_id=request_id)
    try:
        response = await call_next(request)
    finally:
        clear_request_context()
    response.headers["X-Request-ID"] = request_id
    return response


def _normalize_request_id(raw: str | None) -> str:
    """接受上游透传的 id，但**做字符集与长度校验**：它会进日志与响应头。

    只放行 [A-Za-z0-9._-]，上限 64 字符；不合法就重新生成。
    （不校验等于把 header / 日志注入的口子交给调用方。）
    """
    if raw and len(raw) <= 64 and all(c.isalnum() or c in "._-" for c in raw):
        return raw
    return f"req_{uuid.uuid4().hex}"


# 生产入口：`uvicorn pyassist.api.main:app` 走这一行，启动校验在后台跑。
# 测试显式调用 `create_app()`，默认不触网。
app = create_app(verify_models=True)
