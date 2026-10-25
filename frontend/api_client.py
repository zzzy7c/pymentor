"""前端唯一的后端入口：**只用 HTTP，不 import `pyassist`**（SPEC D11 / 守护 #38）。

两个刻意的选择：

1. **不复制 `api/schemas.py` 的 Pydantic 模型**。前端不是契约的所有者，复制一份
   只会在后端追加字段时制造第二套真相。这里一律收发 `dict`。
2. **SSE 自己解析**（`httpx` + 逐行读）。`st.write_stream` 要的是"逐块 yield 文本"，
   浏览器之外的客户端没有 `EventSource`，而 `requests`/`httpx` 的普通调用会等完整响应
   —— 那样"逐字上屏"就变成了"转圈 30 秒"。

路径字符串集中成常量，`tests/test_frontend_contract.py` 会把它们与 `app.openapi()`
的路径集合逐字比对（守护 #44）；写错一个字母就会红。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

# OpenAPI 里的实际路径。动态段保留 `{exercise_id}` 占位，调用处用 `.format()`。
PATH_HEALTH: Final = "/health"
PATH_CHAT: Final = "/chat"
PATH_KNOWLEDGE: Final = "/knowledge"
PATH_DIAGNOSE: Final = "/diagnose"
PATH_EXERCISES: Final = "/exercises"
PATH_SUBMIT: Final = "/exercises/{exercise_id}/submit"
PATH_HISTORY: Final = "/history"
PATH_STATS: Final = "/stats"
PATH_RECOMMEND: Final = "/recommend"

ALL_PATHS: Final[tuple[str, ...]] = (
    PATH_HEALTH,
    PATH_CHAT,
    PATH_KNOWLEDGE,
    PATH_DIAGNOSE,
    PATH_EXERCISES,
    PATH_SUBMIT,
    PATH_HISTORY,
    PATH_STATS,
    PATH_RECOMMEND,
)

DEFAULT_TIMEOUT_SECONDS: Final = 600.0
"""诊断/练习一次可能跑几分钟（模型 + 沙箱）。超时值跟着后端的最坏耗时不跟 curl。"""


class ApiError(Exception):
    """服务端信封里的 `error`，原样带出来给 UI 展示 `code + message`。

    **不包装 traceback、不打印 body 全文**：错误消息可能含用户输入，日志里不该出现。
    """

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True, slots=True)
class ChatEvent:
    """一条 SSE 事件。`payload` 是 `data:` 那行 JSON 解码后的 dict。"""

    type: str
    payload: dict[str, Any]


class ApiClient:
    """同步客户端。Streamlit 的脚本模型是单线程渲染，异步客户端只会多一层事件循环。"""

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # `transport` 只给测试注入 `httpx.MockTransport`；生产路径永远是 None。
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    # ---------- 生命周期 ----------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------- v1 / v2 ----------
    def health(self) -> dict[str, Any]:
        return self._get(PATH_HEALTH)

    def chat_stream(self, question: str, session_id: str) -> Iterator[ChatEvent]:
        """逐事件产出 `/chat` 的 SSE。收到 `done` 即停。

        非 200 时先把响应体读全（信封在 body 里），再抛 `ApiError` ——
        不能让调用方在 `for` 里收到半截流还以为成功。
        """
        try:
            with self._client.stream(
                "POST", PATH_CHAT, json={"question": question, "session_id": session_id}
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise _error_from(response)
                for event in parse_sse(response.iter_lines()):
                    yield event
                    if event.type == "done":
                        return
        except httpx.HTTPError as exc:
            raise _connection_error(self.base_url) from exc

    def ask(self, question: str, session_id: str) -> dict[str, Any]:
        return self._post(PATH_KNOWLEDGE, {"question": question, "session_id": session_id})

    def diagnose(self, error_text: str, code: str | None, session_id: str) -> dict[str, Any]:
        return self._post(
            PATH_DIAGNOSE,
            {"error_text": error_text, "code": code, "session_id": session_id},
        )

    # ---------- v3 ----------
    def create_exercise(self, topic: str, session_id: str) -> dict[str, Any]:
        return self._post(PATH_EXERCISES, {"topic": topic, "session_id": session_id})

    def submit(self, exercise_id: str, code: str, session_id: str) -> dict[str, Any]:
        path = PATH_SUBMIT.format(exercise_id=exercise_id)
        return self._post(path, {"code": code, "session_id": session_id})

    # ---------- v4 ----------
    def history(
        self, session_id: str, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"session_id": session_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._get(PATH_HISTORY, params=params)

    def stats(self, user_id: str = "local") -> dict[str, Any]:
        return self._get(PATH_STATS, params={"user_id": user_id})

    def recommend(self, session_id: str) -> dict[str, Any]:
        return self._post(PATH_RECOMMEND, {"session_id": session_id})

    # ---------- 内部 ----------
    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise _connection_error(self.base_url) from exc
        return self._unwrap(response)

    def _post(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=dict(body))
        except httpx.HTTPError as exc:
            raise _connection_error(self.base_url) from exc
        return self._unwrap(response)

    @staticmethod
    def _unwrap(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(
                "HTTP_ERROR",
                f"后端返回了无法解析的响应（HTTP {response.status_code}）",
                response.status_code,
            ) from exc
        if not isinstance(body, dict):
            raise ApiError("HTTP_ERROR", "后端响应不是信封格式", response.status_code)
        if response.status_code >= 400 or body.get("ok") is not True:
            raise _error_from_body(body, response.status_code)
        data = body.get("data")
        return data if isinstance(data, dict) else {}


def parse_sse(lines: Iterable[str]) -> Iterator[ChatEvent]:
    """把 SSE 文本行解析成事件。**纯函数**，可用固定夹具离线单测。

    支持注释行（`: heartbeat`）、`event:` / `data:` 字段与多行 data。
    空行是事件边界；流结束时最后一段未以空行结尾也要吐出来。
    """
    event_type = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data_lines:
                yield ChatEvent(event_type, _decode_data("\n".join(data_lines)))
            event_type, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if not sep:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_type = value or "message"
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield ChatEvent(event_type, _decode_data("\n".join(data_lines)))


def _decode_data(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError("BAD_STREAM", "流式事件不是合法 JSON", 200) from exc
    if not isinstance(payload, dict):
        return {"value": payload}
    return payload


def _connection_error(base_url: str) -> ApiError:
    return ApiError("CONNECTION_ERROR", f"无法连接后端：{base_url}", 0)


def _error_from(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
    except ValueError:
        return ApiError(
            "HTTP_ERROR",
            f"后端返回 HTTP {response.status_code}，且响应不是信封格式",
            response.status_code,
        )
    if not isinstance(body, dict):
        return ApiError("HTTP_ERROR", "后端响应不是信封格式", response.status_code)
    return _error_from_body(body, response.status_code)


def _error_from_body(body: Mapping[str, Any], status: int) -> ApiError:
    error = body.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or f"HTTP_{status}")
        message = str(error.get("message") or "请求失败")
        return ApiError(code, message, status)
    return ApiError(f"HTTP_{status}", "请求失败", status)


__all__ = [
    "ALL_PATHS",
    "ApiClient",
    "ApiError",
    "ChatEvent",
    "parse_sse",
]
