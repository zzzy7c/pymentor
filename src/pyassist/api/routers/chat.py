"""`POST /chat`（SSE）—— **唯一** LangGraph 事件 → SSE 映射点（AGENTS.md §2）。

图里已经筛过事件（`stream_mode=["custom","updates"]`，只转发 `custom`），
所以本文件**不做任何业务过滤**，只做"事件 dict → 线上字节"。

三条线上格式硬规则（都由 `tests/test_sse.py` 逐字节断言）：

1. **不发 `retry:`** —— 我们不希望 `EventSource` 自动重连（重连会重跑整张图，等于重复计费）；
2. **`error` 事件之后仍然发 `done`** —— 没有 `done` 的流会被误读成断线；
3. **分隔符是 `\\n\\n`，不是库默认的 `\\r\\n\\r\\n`**（`sep="\\n"`）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Any, Final, cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.event import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from pyassist.agent.state import QaEvent
from pyassist.api.deps import get_qa_graph, get_request_context, get_session, require_quota
from pyassist.api.schemas import ChatRequest
from pyassist.config import DEFAULT_USER_ID, SSE_HEARTBEAT_SECONDS
from pyassist.llm.tracing import UsageSnapshot
from pyassist.logging import get_logger
from pyassist.services.qa import stream_answer
from pyassist.services.usage import record_request_usage

logger = get_logger(__name__)

router = APIRouter(dependencies=[Depends(require_quota)])

# 事件的线上字段白名单（SPEC §5.9）。**加字段必须登记** —— 这是刻意让
# "给事件加字段"不会悄悄改线上格式：`_to_sse` 是查表，未登记的新事件类型会 KeyError。
_PAYLOAD_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "token": ("text",),
    "citations": ("citations",),
    "tool": ("phase", "name"),
    "error": ("code", "message", "details"),
    "done": ("request_id", "usage"),
}


@router.post("/chat")
async def chat(
    request: Request,
    body: ChatRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EventSourceResponse:
    """流式问答。开流**前**做廉价预检，把索引类故障变成真信封（SPEC D6）。"""
    request_id = get_request_context(request).request_id
    thread_id = _thread_id(request, body)
    qa = get_qa_graph(request)
    # 预检必须用图节点的**同一组**依赖（QAGraph.preflight，与 search/fetch 同源），
    # 路由自己调 assert_index_ready() 就等于"预检一套依赖、检索另一套"（D11）。
    await qa.preflight()

    async def on_usage(usage: UsageSnapshot) -> None:
        """`done` 之前记一次账（D8 / S8）。写库失败只降级为"这次没记账"。"""
        try:
            await record_request_usage(
                session,
                request_id=request_id,
                raw_session_id=thread_id,
                user_id=DEFAULT_USER_ID,
                model_id=qa.model_id,
                usage=usage,
            )
        except SQLAlchemyError:
            logger.exception("usage.record_failed", request_id=request_id)

    events = stream_answer(
        body.question,
        graph=qa.graph,
        thread_id=thread_id,
        request_id=request_id,
        on_usage=on_usage,
    )
    return EventSourceResponse(
        _sse_stream(
            events,
            queue=asyncio.Queue(maxsize=64),
            heartbeat_seconds=SSE_HEARTBEAT_SECONDS,
        ),
        ping=0,  # 库自带心跳是 `ping - <utc now>`，既不是契约字面量又不可断言（L10）
        sep="\n",  # 库默认 \r\n，与 §2.4 的字节不一致（L11）
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _thread_id(request: Request, body: ChatRequest) -> str:
    """本阶段"一次请求一条 thread"：不带 session_id 时用 request_id。

    这样不带 session 的调用天然不串台，也不需要额外建会话表（SPEC §10.4）。
    """
    ctx = get_request_context(request)
    return body.session_id or ctx.request_id


async def _sse_stream(
    events: AsyncIterator[QaEvent],
    queue: asyncio.Queue[QaEvent],
    heartbeat_seconds: float,
) -> AsyncIterator[ServerSentEvent]:
    """图在独立 task 里跑，本生成器只负责"搬事件 + 发心跳"（SPEC D5）。

    **不用 `None` 哨兵**（L20 实测）：生产者结束时队列往往正好是满的，
    哨兵的 `await queue.put(None)` 会在取消之后永久阻塞，`gather` 于是永不返回。
    改成与生产者的**完成**竞速（`task.done()`）之后，这个死锁在结构上不存在。
    """
    task = asyncio.create_task(_pump(events, queue))
    getter: asyncio.Task[QaEvent] | None = None
    try:
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {getter, task},
                timeout=heartbeat_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done:
                yield _to_sse(getter.result())
                getter = None
            elif task in done:
                _log_pump_crash(task)
                # 排干尾部：`_pump` 可能写完最后几个事件就结束，只 break 会丢掉 `done`
                while not queue.empty():
                    yield _to_sse(queue.get_nowait())
                break
            else:
                getter.cancel()
                getter = None
                yield ServerSentEvent(comment="heartbeat", sep="\n")
    finally:
        # 先收悬空的 `queue.get()`：漏掉它，`aclose()` 会一直等（L20 实测）。
        if getter is not None:
            getter.cancel()
        task.cancel()  # 客户端断开也要停图（否则账单照付）
        await asyncio.gather(task, return_exceptions=True)


async def _pump(events: AsyncIterator[QaEvent], queue: asyncio.Queue[QaEvent]) -> None:
    """把服务层的事件搬进队列。**不发结束哨兵** —— 生产者的结束由 task 状态表达。"""
    try:
        async for event in events:
            await queue.put(event)  # 队列满 → 背压；取消 → CancelledError 自然上抛
    finally:
        # 客户端断开时把上游生成器**显式**关掉。只 cancel 本 task 是不够的：
        # CancelledError 只会从 `queue.put` 冒出来，`async for` 背后的生成器
        # （`stream_answer` → `graph.astream`）会留在挂起状态 —— 图可能继续跑，
        # 而用户早就走了（"用户了事、账单照付"）。`aclose()` 把 GeneratorExit
        # 送进生成器，LangGraph 才会在同一个 tick 里收尾。
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()  # `AsyncIterator` 协议外的可选能力（真实对象都是生成器）


def _to_sse(event: QaEvent) -> ServerSentEvent:
    """事件 → SSE。**唯一**的映射点：按 `type` 挑白名单字段，不整包塞进 data。"""
    kind = event["type"]
    # TypedDict 的键必须是字面量，而这里是**查表**拼字段 —— 显式收窄成 Mapping。
    # 这不是绕过类型检查：白名单本身就是 `Final`，字段集合在 import 期就定死了。
    raw = cast("Mapping[str, Any]", event)
    payload = {key: raw[key] for key in _PAYLOAD_KEYS[kind] if key in raw}
    return ServerSentEvent(event=kind, data=json.dumps(payload, ensure_ascii=False), sep="\n")


def _log_pump_crash(task: asyncio.Task[None]) -> None:
    """生产者异常只进日志：流已开，补发事件会打乱状态机（SPEC §5.9）。"""
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error("qa.pump_crashed", exc_info=exc)
