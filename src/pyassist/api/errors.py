"""★ 唯一异常 → HTTP 映射点（AGENTS.md §4：在别处 raise HTTPException 会让错误格式分叉）。

全项目**只有这个文件**：注册异常处理器、构造 JSONResponse、把异常翻译成状态码。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from pyassist.api.schemas import Envelope, ErrorDetail, Meta, envelope_failure, envelope_success
from pyassist.errors import (
    AppError,
    ConfigError,
    CorpusMissing,
    ErrorCode,
    ExerciseNotFound,
    LLMBadResponse,
    LLMInsufficientBalance,
    LLMModelNotFound,
    LLMRateLimited,
    LLMTimeout,
    QuotaExceeded,
    RetrievalEmpty,
    SandboxRejected,
    SandboxTimeout,
    StoreError,
    ValidationError,
)
from pyassist.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Mapping:
    """一条映射：状态码 + 信封的 ok 标志。

    ok 必须进映射表而不是写死 False —— 因为 §3.2 有三个异常映射到 **200 且 ok=True**。
    """

    status: int
    ok: bool


# §3.2 的异常树 → HTTP。**必须覆盖每一个叶子类**（由 test_error_mapping.py 穷尽断言）。
# 键是异常类，不是枚举 —— 中转基类不必逐行写，AppError 那一行兜住它们。
_MAPPING: dict[type[AppError], _Mapping] = {
    ConfigError: _Mapping(500, False),
    ValidationError: _Mapping(422, False),
    RetrievalEmpty: _Mapping(200, True),  # ← 正常业务结果，不是 500
    CorpusMissing: _Mapping(503, False),
    LLMTimeout: _Mapping(504, False),
    LLMRateLimited: _Mapping(429, False),
    LLMInsufficientBalance: _Mapping(503, False),
    LLMModelNotFound: _Mapping(500, False),
    LLMBadResponse: _Mapping(502, False),
    SandboxTimeout: _Mapping(200, True),  # ← 同上
    SandboxRejected: _Mapping(200, True),  # ← 同上
    StoreError: _Mapping(500, False),
    # 阶段八：复用既有码，不新增枚举值（SPEC D10 / ADR 0003）。
    ExerciseNotFound: _Mapping(404, False),
    QuotaExceeded: _Mapping(429, False),
    # 兜底：AppError 自身与三个中转基类。子类未登记时沿 MRO 落到这里。
    AppError: _Mapping(500, False),
}


def _resolve(exc: AppError) -> _Mapping:
    """沿 MRO 找第一条命中：这样阶段六若给 LLMTimeout 加子类，也不必回头改映射表。"""
    for klass in type(exc).__mro__:
        mapping = _MAPPING.get(klass)
        if mapping is not None:
            return mapping
    return _Mapping(500, False)  # 理论上不可达：AppError 已在表里


def _request_id_of(request: Request) -> str:
    """防御性读取：处理器可能在任何中间件之外被调用。"""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else "unknown"


def _latency_ms_of(request: Request) -> int:
    started = getattr(request.state, "started_at", None)
    if not isinstance(started, float):
        return 0
    return int((time.perf_counter() - started) * 1000)


def _meta_of(request: Request) -> Meta:
    """零值 usage：契约要求 meta.usage 永远在位，失败时也不例外（§3.1，示例已同步）。"""
    return Meta(request_id=_request_id_of(request), latency_ms=_latency_ms_of(request))


def _json(
    status: int,
    envelope: Envelope[Any],
    request: Request,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """所有出口的**唯一**构造点。

    为什么不用 JSONResponse(content=envelope)：pydantic 模型不是 JSON 可序列化对象。
    必须 model_dump(mode="json") —— 且必须是 mode="json"，否则 Decimal 不走 field_serializer。

    为什么要自己盖 X-Request-ID：500 由 ServerErrorMiddleware 生成，它在用户中间件**之外**，
    中间件盖不上头（实测 M3）。所以每个出口自己盖。
    """
    out_headers = {"X-Request-ID": _request_id_of(request)}
    if headers:
        out_headers.update(headers)
    return JSONResponse(
        status_code=status,
        content=envelope.model_dump(mode="json"),
        headers=out_headers,
    )


def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """§3.2 异常树的总出口。同步 def：见要点 4。"""
    mapping = _resolve(exc)
    meta = _meta_of(request)

    if mapping.ok:
        # 映射到 200 的三个异常：正常业务结果，data 由异常自带的载荷派生。
        payload = exc.data if exc.data is not None else {"status": "ok"}
        envelope: Envelope[Any] = envelope_success(payload, meta=meta)
    else:
        envelope = envelope_failure(
            ErrorDetail(code=exc.code, message=exc.message, details=exc.details),
            meta=meta,
        )

    logger.warning(
        "api.error",
        request_id=meta.request_id,  # 显式传：兜底时 contextvars 已被清理（M5）
        error_code=exc.code.value,
        status_code=mapping.status,
        error_type=type(exc).__name__,
        # 不记 message 全文：可能含用户代码片段（§4 安全约束）
    )

    headers: dict[str, str] = {}
    # 基类上的 `retry_after` 现在承载两类 429：上游限流（LLMRateLimited）与
    # 本地配额（QuotaExceeded）。状态码相同、码不同，见 §3.2。
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return _json(mapping.status, envelope, request, headers or None)


def _handle_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """pydantic 校验失败 → 422。

    实测：它**不再继承** starlette 的 HTTPException，必须单注册（1.4）。
    """
    meta = _meta_of(request)
    envelope = envelope_failure(
        ErrorDetail(
            code=ErrorCode.VALIDATION_ERROR,
            message="请求参数不合法",
            # 只回显 pydantic 的 loc/msg，**不回显 input** —— input 可能是用户代码。
            details={
                "errors": [
                    {
                        "loc": list(e.get("loc", [])),
                        "msg": e.get("msg", ""),
                        "type": e.get("type", ""),
                    }
                    for e in exc.errors()
                ]
            },
        ),
        meta=meta,
    )
    logger.warning(
        "api.error",
        request_id=meta.request_id,
        error_code=ErrorCode.VALIDATION_ERROR.value,
        status_code=422,
    )
    return _json(422, envelope, request)


def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """starlette 自产的 404 / 405 也要落进信封。

    这两条是**框架**在路由匹配阶段抛的，不经过我们的代码 —— 不接管就会漏出 {"detail": ...}。
    """
    code = ErrorCode.METHOD_NOT_ALLOWED if exc.status_code == 405 else ErrorCode.NOT_FOUND
    meta = _meta_of(request)
    envelope = envelope_failure(
        ErrorDetail(code=code, message=str(exc.detail), details=None), meta=meta
    )
    logger.warning(
        "api.error", request_id=meta.request_id, error_code=code.value, status_code=exc.status_code
    )
    return _json(exc.status_code, envelope, request)


def _handle_rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """slowapi 限流 → 429。

    **必须写成同步 def**：slowapi 的 sync_check_limits 会丢弃 async 处理器、换上它自己返回
    {"error": "Rate limit exceeded: ..."} 的默认处理器（实测 M6）。写成 async 会静默绕过
    整条信封链路。`exc` 未使用是有意的 —— 处理器只产出固定文案。
    """
    meta = _meta_of(request)
    envelope = envelope_failure(
        ErrorDetail(code=ErrorCode.RATE_LIMITED, message="请求过于频繁，请稍后重试", details=None),
        meta=meta,
    )
    logger.warning(
        "api.error",
        request_id=meta.request_id,
        error_code=ErrorCode.RATE_LIMITED.value,
        status_code=429,
    )
    return _json(429, envelope, request, {"Retry-After": "60"})


def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """兜底：**唯一**一处捕获 Exception。

    §3.1 要求任何失败都带完整信封，而 FastAPI 默认 500 返回 {"detail": ...}。边界写死在这里：
      · 必须 structlog 打点并带 request_id（显式传，M5）；
      · **不外泄** traceback / 异常 message / 密钥（message 用固定文案）；
      · **不吞异常语义** —— 这不是 `except Exception: pass`（守护 #19 会检查这一点）。
    """
    meta = _meta_of(request)
    envelope = envelope_failure(
        ErrorDetail(code=ErrorCode.INTERNAL_ERROR, message="服务内部错误", details=None), meta=meta
    )
    logger.error(
        "api.unhandled_error",
        request_id=meta.request_id,
        error_type=type(exc).__name__,
        status_code=500,
    )
    return _json(500, envelope, request)


def register_exception_handlers(app: FastAPI) -> None:
    """**唯一**的处理器注册点。main.py 只调用这一行。

    注册面必须完整：少注册 RequestValidationError 就漏 422、
    少注册 StarletteHTTPException 就漏 404/405。

    处理器把异常参数收窄到具体类型（可读性好、类型安全），而 starlette 的 `ExceptionHandler`
    签名要求第二个参数是 `Exception` —— 参数逆变，故此处逐个 `type: ignore[arg-type]`。
    运行期由 starlette 按 __mro__ 精确派发，收窄是安全的。
    """
    app.add_exception_handler(AppError, _handle_app_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(RateLimitExceeded, _handle_rate_limited)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unexpected)
