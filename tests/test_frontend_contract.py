"""前端契约（SPEC D11 / 守护 #38 / #44）。

三件事必须同时成立，否则"前端只走 HTTP"就只是口号：

1. `frontend/**` 的 AST 里没有 `pyassist` 导入（守护 #38）；
2. `api_client.py` 里的每个路径字符串都真实存在于 `app.openapi()`（守护 #44）；
3. SSE 解析是纯函数，坏数据、心跳、多行 data 都有确定行为。

客户端用 `httpx.MockTransport` 打桩，不碰网络。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from pyassist.api.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "frontend"
API_CLIENT_PATH = FRONTEND_DIR / "api_client.py"
APP_PATH = FRONTEND_DIR / "streamlit_app.py"


_MODULE_NAME = "frontend_api_client"


def _load_api_client() -> Any:
    """按路径加载 `frontend/api_client.py`（`frontend/` 不是包，也没有 `__init__.py`）。

    **必须先登记进 `sys.modules`**：`dataclass(slots=True)` 会通过
    `sys.modules[cls.__module__]` 回查注解，未登记的模块会直接 `AttributeError`。
    """
    cached = sys.modules.get(_MODULE_NAME)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, API_CLIENT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _frontend_sources() -> Iterator[Path]:
    yield from sorted(FRONTEND_DIR.glob("*.py"))


def test_frontend_never_imports_pyassist() -> None:
    """守护 #38：前端只经 HTTP 说话。"""
    offenders: list[str] = []
    for path in _frontend_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}: {alias.name}"
                    for alias in node.names
                    if alias.name == "pyassist" or alias.name.startswith("pyassist.")
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and (node.module == "pyassist" or node.module.startswith("pyassist."))
            ):
                offenders.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Call) and _is_dynamic_import(node):
                offenders.append(f"{path.name}: 动态导入")
    assert not offenders, f"前端不得 import pyassist：{offenders}"


def _is_dynamic_import(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "__import__"
    if isinstance(func, ast.Attribute):
        return func.attr == "import_module"
    return False


def test_api_client_paths_are_in_openapi() -> None:
    """守护 #44：路径写错一个字母，这里就红 —— 不靠人眼比对。"""
    module = _load_api_client()
    app = create_app(
        database_url="sqlite+aiosqlite:///:memory:",
        rate_limit_enabled=False,
    )
    openapi_paths = set(app.openapi()["paths"])

    assert set(module.ALL_PATHS) <= openapi_paths
    for path in module.ALL_PATHS:
        assert path.startswith("/")


def test_every_path_literal_in_client_is_in_openapi() -> None:
    """比 `ALL_PATHS` 更严：散落在函数体里的字面量也不能是野路径。"""
    module = _load_api_client()
    app = create_app(
        database_url="sqlite+aiosqlite:///:memory:",
        rate_limit_enabled=False,
    )
    openapi_paths = set(app.openapi()["paths"])

    literals = {
        node.value
        for node in ast.walk(ast.parse(API_CLIENT_PATH.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("/")
        # `rstrip("/")` 之类的单字符常量不是路由。
        and node.value != "/"
    }

    assert literals
    assert literals <= openapi_paths
    assert set(module.ALL_PATHS) == literals


def test_streamlit_app_is_syntactically_valid() -> None:
    compile(APP_PATH.read_text(encoding="utf-8"), str(APP_PATH), "exec")


def test_parse_sse_handles_tokens_heartbeats_and_done() -> None:
    module = _load_api_client()
    lines = [
        ": heartbeat",
        "event: token",
        'data: {"text": "你好"}',
        "",
        "event: citations",
        'data: {"citations": [{"index": 1, "url": "https://example.com"}]}',
        "",
        "event: done",
        'data: {"request_id": "req_1"}',
        "",
    ]

    events = list(module.parse_sse(lines))

    assert [event.type for event in events] == ["token", "citations", "done"]
    assert events[0].payload == {"text": "你好"}
    assert events[1].payload["citations"][0]["index"] == 1
    assert events[2].payload["request_id"] == "req_1"


def test_parse_sse_flushes_trailing_event_without_blank_line() -> None:
    module = _load_api_client()

    events = list(module.parse_sse(["event: token", 'data: {"text": "尾"}']))

    assert len(events) == 1
    assert events[0].type == "token"
    assert events[0].payload["text"] == "尾"


def test_parse_sse_joins_multiline_data() -> None:
    module = _load_api_client()

    events = list(module.parse_sse(["event: token", "data: {", 'data: "text": "x"}', ""]))

    assert events[0].payload == {"text": "x"}


def test_parse_sse_rejects_malformed_json() -> None:
    module = _load_api_client()

    with pytest.raises(module.ApiError) as excinfo:
        list(module.parse_sse(["event: token", "data: not-json", ""]))

    assert excinfo.value.code == "BAD_STREAM"


def _client(module: Any, handler: Any) -> Any:
    return module.ApiClient("http://testserver", transport=httpx.MockTransport(handler))


def test_client_unwraps_envelope_data() -> None:
    module = _load_api_client()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/stats"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "data": {"total_attempts": 3},
                "error": None,
                "meta": {},
            },
        )

    data = _client(module, handler).stats()

    assert data == {"total_attempts": 3}


def test_client_converts_failure_envelope_to_api_error() -> None:
    module = _load_api_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "data": None,
                "error": {"code": "RATE_LIMITED", "message": "配额用尽"},
                "meta": {},
            },
        )

    with pytest.raises(module.ApiError) as excinfo:
        _client(module, handler).recommend("sid")

    assert (excinfo.value.code, excinfo.value.status) == ("RATE_LIMITED", 429)


def test_client_streams_chat_tokens() -> None:
    module = _load_api_client()

    body = (
        "event: token\ndata: " + json.dumps({"text": "你"}, ensure_ascii=False) + "\n\n"
        "event: token\ndata: " + json.dumps({"text": "好"}, ensure_ascii=False) + "\n\n"
        "event: done\ndata: {}\n\n"
        "event: token\ndata: " + json.dumps({"text": "不应出现"}) + "\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat"
        return httpx.Response(200, content=body.encode("utf-8"))

    events = list(_client(module, handler).chat_stream("hi", "sid"))

    assert [event.type for event in events] == ["token", "token", "done"]
    assert (
        "".join(str(event.payload.get("text", "")) for event in events if event.type == "token")
        == "你好"
    )


def test_client_stream_failure_reads_envelope_first() -> None:
    module = _load_api_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "ok": False,
                "data": None,
                "error": {"code": "CONFIG_ERROR", "message": "问答图不可用"},
                "meta": {},
            },
        )

    with pytest.raises(module.ApiError) as excinfo:
        list(_client(module, handler).chat_stream("hi", "sid"))

    assert excinfo.value.code == "CONFIG_ERROR"


def test_client_reports_unreachable_backend_without_leaking_transport() -> None:
    module = _load_api_client()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(module.ApiError) as excinfo:
        _client(module, handler).health()

    assert excinfo.value.code == "CONNECTION_ERROR"
    assert "http://testserver" in excinfo.value.message
