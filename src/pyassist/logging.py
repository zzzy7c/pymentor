"""唯一 structlog 初始化（§2 / §3.5）。其他模块只调用 `get_logger()`，不得自行 configure。

命名说明：本模块名与标准库 `logging` 同名，但 **不会遮蔽标准库**。
Python 3 默认绝对导入，包内（乃至 `src/pyassist/` 之外）`import logging` 取到的都是标准库
—— 本模块只有在显式 `from pyassist.logging import ...` / `import pyassist.logging` 时才被选中。
为避免读者困惑，本文件内部导入标准库时一律显式别名 `stdlib_logging`。

设计（SPEC §4 T5）：
- `structlog.configure(...)` 全局只在此处调用一次。
- 输出渲染器二选一：`JSONRenderer`（生产）/ `ConsoleRenderer`（本地），由 `settings.log_json` 决定。
- `request_id` / `session_id` / `user_id` 经 `structlog.contextvars` 绑定 —— contextvars 对 async
  任务天然隔离，阶段四的中间件才能"一次调用串起来"。
- stdlib 桥接：`ProcessorFormatter` 挂到 root logger，让 uvicorn / sqlalchemy 的日志走同一渲染器。
  这条现在不做，阶段四再补就得动 uvicorn 的启动配置。
"""

from __future__ import annotations

import logging as stdlib_logging
from typing import Any, cast

import structlog
from structlog.stdlib import BoundLogger
from structlog.typing import Processor

__all__ = [
    "bind_request_context",
    "clear_request_context",
    "configure_logging",
    "get_logger",
]

# 与渲染器无关的公共处理器：contextvars 绑定、级别、时间戳、异常格式化。
# 注意顺序：merge_contextvars 必须在最前，否则绑定字段会被后续处理器丢掉。
_SHARED_PROCESSORS: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """初始化 structlog 与 stdlib 桥接。幂等：可重复调用，后一次覆盖前一次。"""
    # ConsoleRenderer 在 structlog 26 里位于 structlog.dev（不再是 structlog.processors）。
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    # structlog 侧：以 wrap_for_formatter 收尾，真正的渲染交给下面的 ProcessorFormatter。
    # 这样 structlog 与被 stdlib 记录的日志（uvicorn / sqlalchemy）共用同一个渲染器。
    structlog.configure(
        processors=[*_SHARED_PROCESSORS, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = stdlib_logging.StreamHandler()
    handler.setFormatter(formatter)

    # 重复调用时清掉旧 handler，避免同一行日志被打两次。
    root_logger = stdlib_logging.getLogger()
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
    root_logger.addHandler(handler)
    root_logger.setLevel(level.upper())


def get_logger(name: str | None = None) -> BoundLogger:
    """取一个绑定后的 logger。`cast` 收窄 `structlog.get_logger()` 的 `Any` 返回，
    否则 mypy --strict 的 `warn_return_any` 会在这里报错。"""
    return cast("BoundLogger", structlog.get_logger(name))


def bind_request_context(
    *,
    request_id: str,
    session_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """把一次调用的上下文字段绑进 contextvars。None 的字段不绑定，避免日志里堆 null。"""
    context: dict[str, Any] = {"request_id": request_id}
    if session_id is not None:
        context["session_id"] = session_id
    if user_id is not None:
        context["user_id"] = user_id
    structlog.contextvars.bind_contextvars(**context)


def clear_request_context() -> None:
    """清空当前上下文的绑定。异步任务结束时必须调用，防止串号。"""
    structlog.contextvars.clear_contextvars()
