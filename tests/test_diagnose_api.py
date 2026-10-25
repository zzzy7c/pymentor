"""`POST /diagnose` 的 HTTP 契约（SPEC §5.17 / §5.16）。

判据：信封、`meta.usage`、422、空检索 → 200、沙箱结果 → 200、
基础设施故障 → 500。全部离线：脚本模型 + 假沙箱 + 确定性检索。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from conftest import create_business_tables
from fakes.llm import ScriptedChatModel
from fakes.sandbox import FakeSandboxRunner, result_for
from pyassist.agent.graphs.diagnose import RepairDraft
from pyassist.api.main import create_app
from pyassist.errors import SandboxError
from pyassist.rag.retriever import RetrievedChunk

ERROR_TEXT = """Traceback (most recent call last):
  File "app.py", line 7, in <module>
    total = int("abc")
            ^^^^^^^^^^
ValueError: invalid literal for int() with base 10: 'abc'
"""

_DOC_URL = "https://docs.python.org/3.12/library/exceptions.html"
Search = Callable[[str, int], Awaitable[list[RetrievedChunk]]]


def _chunks() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id="exceptions#valueerror",
            text="ValueError 在参数类型正确而取值不合法时由内建操作抛出。",
            doc_url=_DOC_URL,
            source_url=f"{_DOC_URL}#ValueError",
            title="Built-in Exceptions",
            section="ValueError",
            score=0.91,
        )
    ]


async def _default_search(query: str, top_k: int) -> list[RetrievedChunk]:
    return _chunks()


async def _empty_search(query: str, top_k: int) -> list[RetrievedChunk]:
    return []


@contextmanager
def _client(
    database_url: str,
    model: ScriptedChatModel,
    runner: Any,
    *,
    search: Search = _default_search,
) -> Iterator[TestClient]:
    """每个用例一个 app：**context manager 必须进**，否则 lifespan 不跑、图不存在。"""

    async def fetch(doc_url: str) -> list[RetrievedChunk]:
        return _chunks()

    async def preflight() -> None:
        return None

    create_business_tables(database_url)
    app = create_app(
        database_url=database_url,
        rate_limit_enabled=False,
        chat_model=model,
        diagnose_model=model,
        sandbox_runner=runner,
        search=search,
        fetch=fetch,
        preflight=preflight,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _ai(content: str, tool_calls: list[dict[str, Any]] | None = None) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=list(tool_calls or []),
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 64},
        },
    )


def _draft(code: str = "print(int('42'))") -> RepairDraft:
    return RepairDraft(
        error_type="ValueError",
        explanation="int() 收到无法解析的字符串（见 [1]）。",
        fixed_code=code,
        change_summary=["换成可解析的数字"],
    )


def _post(client: TestClient, **overrides: Any):
    payload = {"error_text": ERROR_TEXT}
    payload.update(overrides)
    return client.post("/diagnose", json=payload)


# ---------- ① 成功：完整信封 + usage ----------


def test_diagnose_returns_envelope_with_usage(
    tmp_database_url: str,
    scripted_model: ScriptedChatModel,
    fake_sandbox: FakeSandboxRunner,
) -> None:
    scripted_model.script = [_ai("证据够了。")]
    scripted_model.structured_script = [_draft()]
    fake_sandbox.script = [result_for("print(int('42'))", stdout="42\n")]

    with _client(tmp_database_url, scripted_model, fake_sandbox) as client:
        response = _post(client, code="int('abc')")

    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("req_")
    body = response.json()
    assert set(body) == {"ok", "data", "error", "meta"}
    assert body["ok"] is True
    assert body["error"] is None

    data = body["data"]
    # `data` 永远是对象（§3.1）
    assert isinstance(data, dict)
    assert data["status"] == "ok"
    assert data["error_type"] == "ValueError"
    assert data["fixed_code"] == "print(int('42'))"
    assert data["change_summary"] == ["换成可解析的数字"]
    assert data["verification"]["status"] == "ok"
    assert data["verification"]["exit_code"] == 0
    assert data["verification"]["stdout"] == "42\n"
    assert data["verification"]["is_truncated"] is False
    assert [citation["index"] for citation in data["citations"]] == [1]
    assert data["citations"][0]["url"] == f"{_DOC_URL}#ValueError"

    usage = body["meta"]["usage"]
    assert set(usage) == {
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cost_usd",
        "is_off_peak",
    }
    # agent（100+20） + 结构化修复（40+20）
    assert usage["prompt_tokens"] == 140
    assert usage["completion_tokens"] == 40
    assert usage["cached_tokens"] == 64
    assert usage["cost_usd"] > 0


def test_diagnose_keeps_request_id_in_meta(
    tmp_database_url: str,
    scripted_model: ScriptedChatModel,
    fake_sandbox: FakeSandboxRunner,
) -> None:
    scripted_model.script = [_ai("证据够了。")]
    scripted_model.structured_script = [_draft()]
    fake_sandbox.script = [result_for("print(int('42'))")]

    with _client(tmp_database_url, scripted_model, fake_sandbox) as client:
        response = _post(client)

    body = response.json()
    assert body["meta"]["request_id"] == response.headers["X-Request-ID"]
    assert body["meta"]["latency_ms"] >= 0


# ---------- ② 请求校验 ----------


def test_missing_error_text_is_422(
    tmp_database_url: str,
    scripted_model: ScriptedChatModel,
    fake_sandbox: FakeSandboxRunner,
) -> None:
    with _client(tmp_database_url, scripted_model, fake_sandbox) as client:
        response = client.post("/diagnose", json={})

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]
    assert set(body["meta"]) == {"request_id", "latency_ms", "usage"}


def test_empty_error_text_is_422(
    tmp_database_url: str,
    scripted_model: ScriptedChatModel,
    fake_sandbox: FakeSandboxRunner,
) -> None:
    with _client(tmp_database_url, scripted_model, fake_sandbox) as client:
        response = _post(client, error_text="")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------- ③ 空检索：200 + status="empty" ----------


def test_empty_retrieval_is_a_200_business_result(
    tmp_database_url: str,
    scripted_model: ScriptedChatModel,
    fake_sandbox: FakeSandboxRunner,
) -> None:
    scripted_model.script = [_ai("这条永远不该被生成。")]

    with _client(tmp_database_url, scripted_model, fake_sandbox, search=_empty_search) as client:
        response = _post(client)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["data"]["status"] == "empty"
    assert body["data"]["query"] == "ValueError: invalid literal for int() with base 10: 'abc'"
    # 零值 usage 仍然在位，不省略字段
    assert body["meta"]["usage"]["prompt_tokens"] == 0
    assert scripted_model.requests == []
    assert fake_sandbox.calls == []


# ---------- ④ 沙箱失败是实验结果，不是 5xx ----------


@pytest.mark.parametrize("status", ["timeout", "rejected", "error"])
def test_sandbox_failure_is_200_with_data_status(
    status: str,
    tmp_database_url: str,
    scripted_model: ScriptedChatModel,
    fake_sandbox: FakeSandboxRunner,
) -> None:
    scripted_model.script = [_ai("证据够了。")]
    scripted_model.structured_script = [_draft("print(1)"), _draft("print(2)")]
    # 两次尝试都得到同一种失败 → 用完 MAX_REPAIR_ATTEMPTS 后收尾
    fake_sandbox.script = [
        result_for("print(1)", status=status, reason="test-only"),
        result_for("print(2)", status=status, reason="test-only"),
    ]

    with _client(tmp_database_url, scripted_model, fake_sandbox) as client:
        response = _post(client)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["data"]["status"] == status
    assert body["data"]["verification"]["status"] == status
    assert len(fake_sandbox.calls) == 2


# ---------- ⑤ 基础设施故障 → 500 ----------


class _BrokenRunner:
    """daemon 挂了：**不是**用户代码的错，必须冒泡成 SandboxError（→500）。"""

    async def run(self, source: str, *, stdin: str = "", timeout_seconds: int | None = None) -> Any:
        raise SandboxError("沙箱不可用：test-only")


def test_sandbox_infrastructure_failure_is_500(
    tmp_database_url: str,
    scripted_model: ScriptedChatModel,
) -> None:
    scripted_model.script = [_ai("证据够了。")]
    scripted_model.structured_script = [_draft()]

    with _client(tmp_database_url, scripted_model, _BrokenRunner()) as client:
        response = _post(client)

    assert response.status_code == 500
    body = response.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "沙箱" in body["error"]["message"]
    assert set(body["meta"]) == {"request_id", "latency_ms", "usage"}
