"""信封守护（SPEC 阶段四 §4.8）。

契约冻结的可执行形式：字段集合一变即红；成功、失败、未知路由、未捕获异常**四条出口**
都必须装配成同一个信封形状。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pyassist.api.main import create_app
from pyassist.api.schemas import API_VERSION, Envelope, ErrorDetail, Meta, Page, Usage
from pyassist.errors import RetrievalEmpty

SNAPSHOT = Path(__file__).parent / "snapshots" / "envelope.json"


def test_api_version_is_declared() -> None:
    assert API_VERSION == "v1"


def test_usage_serializes_whole_numbers() -> None:
    """pydantic 2.13.5 默认把 Decimal 序列化成字符串，必须被 field_serializer 修掉。"""
    dumped = Usage(cost_usd=Decimal("0.0004")).model_dump(mode="json")
    assert dumped["cost_usd"] == 0.0004
    assert isinstance(dumped["cost_usd"], float)


def test_meta_always_carries_usage() -> None:
    """§3.1：usage 永远在位；缺就填零值，不省略（示例与规则现已一致）。"""
    meta = Meta(request_id="req_x", latency_ms=1)
    assert meta.usage.prompt_tokens == 0
    assert set(meta.model_dump()) == {"request_id", "latency_ms", "usage"}


def test_contract_snapshot_fields_unchanged() -> None:
    """★ 契约冻结的可执行形式：字段集合一变即红。只比字段名，不比取值。"""
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = {
        "API_VERSION": API_VERSION,
        "Envelope": sorted(Envelope.model_fields),
        "Meta": sorted(Meta.model_fields),
        "Usage": sorted(Usage.model_fields),
        "ErrorDetail": sorted(ErrorDetail.model_fields),
        "Page": sorted(Page.model_fields),
    }
    for key, value in actual.items():
        assert value == frozen[key], f"{key} 的字段集合已变 —— 这是破坏性变更（§4）"


def test_health_returns_compliant_envelope(client: TestClient) -> None:
    response = client.get("/health")
    body = response.json()
    assert set(body) == {"ok", "data", "error", "meta"}
    assert body["ok"] is True
    assert set(body["meta"]) == {"request_id", "latency_ms", "usage"}
    assert response.headers["X-Request-ID"].startswith("req_")


def test_unknown_route_yields_envelope(client: TestClient) -> None:
    """404 也必须是完整信封，且带 meta —— 由错误处理器接管，不是默认 {"detail": ...}。"""
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"ok", "data", "error", "meta"}
    assert body["ok"] is False
    assert body["error"]["code"] == "NOT_FOUND"
    # 失败出口也自己盖 X-Request-ID（M3：中间件盖不上）。
    assert response.headers["X-Request-ID"]


def test_unhandled_exception_yields_envelope(tmp_database_url: str) -> None:
    """兜底 500 也是信封。**同步 def** + raise_server_exceptions=False 是必需的（M1）。

    路由在客户端启动**之前**加上：Starlette 的中间件栈在首个请求时构建并缓存，
    启动后再加路由可能不被采用。为此单造一个 app（测试代码，不违反任何守护）。
    """

    def boom() -> None:
        raise RuntimeError("test-only failure")

    app = create_app(database_url=tmp_database_url, rate_limit_enabled=False)
    app.add_api_route("/_boom", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/_boom")
    assert response.status_code == 500
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["message"] == "服务内部错误"  # 不回显 str(exc)
    assert set(body["meta"]) == {"request_id", "latency_ms", "usage"}


def test_business_result_200_uses_success_envelope(tmp_database_url: str) -> None:
    """§3.2 的三条 200 走**成功**信封：ok=True、data 承载实验结果、error 为 null。

    这是对映射表 ok 标志的端到端断言 —— `_resolve` 单测只验表，不验信封形状。
    """

    def empty() -> None:
        raise RetrievalEmpty(query="asyncio.gather 异常处理")

    app = create_app(database_url=tmp_database_url, rate_limit_enabled=False)
    app.add_api_route("/_empty", empty)
    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/_empty")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["data"]["status"] == "empty"
    assert body["data"]["query"] == "asyncio.gather 异常处理"


def test_rate_limited_end_to_end(tmp_database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """R2 / M6 的集成断言：限流 429 也必须是**我们的信封**，不是 slowapi 的 `{"error": ...}`。

    若处理器被误写成 `async def`，slowapi 会静默换上它自己的默认处理器 —— 这条会立刻红。
    把默认限额收紧到 1/分钟（monkeypatch 模块常量，不碰 limiter 私有状态）。

    这条同时钉住 `create_app` 的**路由挂载方式**：fastapi 0.142 下若用 `include_router`，
    slowapi 找不到 `.endpoint`，默认限流会静默失效 —— 那时本用例会退化成 200/200 而变红。
    """
    monkeypatch.setattr("pyassist.api.main.DEFAULT_RATE_LIMIT", "1/minute")
    app = create_app(database_url=tmp_database_url, rate_limit_enabled=True)
    with TestClient(app, raise_server_exceptions=False) as c:
        first = c.get("/health")
        second = c.get("/health")
        # 挂载方式必须让 /health 进 OpenAPI，否则路由虽可达却成了"影子端点"。
        assert "/health" in c.get("/openapi.json").json()["paths"]
    assert first.status_code == 200
    assert second.status_code == 429
    body = second.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["message"] == "请求过于频繁，请稍后重试"
    assert second.headers["X-Request-ID"]  # 外层中间件给限流响应也盖上了 id
    assert second.headers["Retry-After"] == "60"
