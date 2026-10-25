"""v2 诊断图（SPEC §5.17）：检索 → 工具循环 → 结构化修复 → 沙箱验证 → 回环。

全部离线：
- 图用 `ScriptedChatModel`（含 `structured_script`），不触网；
- 沙箱用 `FakeSandboxRunner`，**绝不执行任何代码**；
- 只有 `test_two_tool_rounds_keep_reasoning_content` 用 `respx` 打桩真实
  `ChatDeepSeekPatched` —— 它断言的正是出站序列化这一步（R1）。

检索用确定性替身（返回固定两条片段）：本文件测的是**图**，不是检索质量；
检索质量在 `test_retriever.py` / `test_rag_eval.py`。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from pydantic import SecretStr

from fakes.llm import ScriptedChatModel
from fakes.sandbox import FakeSandboxRunner, result_for
from pyassist.agent.checkpointer import open_checkpointer
from pyassist.agent.graphs.diagnose import (
    DiagnoseGraph,
    RepairDraft,
    build_diagnose_graph,
)
from pyassist.config import MAX_REPAIR_ATTEMPTS, MAX_TOOL_ROUNDS, MODEL_FLASH, settings
from pyassist.errors import ConfigError, LLMBadResponse, RetrievalEmpty
from pyassist.llm.client import get_chat_model
from pyassist.rag.retriever import RetrievedChunk
from pyassist.sandbox.runner import SandboxResult, sandbox_key

TEST_API_KEY = SecretStr("test-only-placeholder-key")
CHAT_URL = f"{settings.deepseek_api_base_url}/chat/completions"

ERROR_TEXT = """Traceback (most recent call last):
  File "app.py", line 7, in <module>
    total = int("abc")
            ^^^^^^^^^^
ValueError: invalid literal for int() with base 10: 'abc'
"""

_DOC_URL = "https://docs.python.org/3.12/library/exceptions.html"


# ---------- 夹具与脚本 ----------


def _chunks() -> list[RetrievedChunk]:
    """固定两条片段。与夹具语料无关 —— 图测试只关心"有几条、编号怎么走"。"""
    return [
        RetrievedChunk(
            chunk_id="exceptions#valueerror",
            text="ValueError 在参数类型正确而取值不合法时由内建操作抛出。",
            doc_url=_DOC_URL,
            source_url=f"{_DOC_URL}#ValueError",
            title="Built-in Exceptions",
            section="ValueError",
            score=0.91,
        ),
        RetrievedChunk(
            chunk_id="exceptions#int",
            text="int() 无法解析字符串时抛出 ValueError。",
            doc_url=_DOC_URL,
            source_url=f"{_DOC_URL}#int",
            title="Built-in Functions",
            section="int",
            score=0.84,
        ),
    ]


def _deps(
    search: Callable[[str, int], Awaitable[list[RetrievedChunk]]] | None = None,
) -> dict[str, Any]:
    """成对注入（D11）：search / fetch / preflight 必须同源。"""

    async def default_search(query: str, top_k: int) -> list[RetrievedChunk]:
        return _chunks()

    async def fetch(doc_url: str) -> list[RetrievedChunk]:
        return _chunks()

    async def preflight() -> None:
        return None

    return {"search": search or default_search, "fetch": fetch, "preflight": preflight}


def _ai(
    content: str,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning: str | None = None,
) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=list(tool_calls or []),
        additional_kwargs={} if reasoning is None else {"reasoning_content": reasoning},
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 64},
        },
    )


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id}


def _draft(
    code: str = "print(int('42'))",
    *,
    error_type: str = "ValueError",
    explanation: str = "int() 收到无法解析的字符串（见 [1]）。",
    summary: list[str] | None = None,
) -> RepairDraft:
    return RepairDraft(
        error_type=error_type,
        explanation=explanation,
        fixed_code=code,
        change_summary=["把字符串换成可解析的数字"] if summary is None else summary,
    )


def _graph(
    saver: Any,
    model: ScriptedChatModel,
    runner: FakeSandboxRunner,
    *,
    search: Callable[[str, int], Awaitable[list[RetrievedChunk]]] | None = None,
) -> DiagnoseGraph:
    return build_diagnose_graph(checkpointer=saver, model=model, runner=runner, **_deps(search))


def _config(thread_id: str = "thread-1") -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


async def _run(
    diagnose: DiagnoseGraph,
    *,
    error_text: str = ERROR_TEXT,
    code: str | None = None,
    thread_id: str = "thread-1",
) -> dict[str, Any]:
    config = _config(thread_id)
    await diagnose.graph.ainvoke({"error_text": error_text, "broken_code": code}, config)
    snapshot = await diagnose.graph.aget_state(config)
    return dict(snapshot.values)


@pytest.fixture
async def saver(tmp_path) -> AsyncIterator[Any]:
    """临时检查点库。**不用 MemorySaver**（AGENTS.md §2 明令禁止）。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'checkpoint.db').as_posix()}"
    async with open_checkpointer(database_url=url) as handle:
        yield handle


# ---------- ① 实质路径：工具轮 + 结构化修复 + 沙箱通过 ----------


async def test_happy_path_with_one_tool_round(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    scripted_model.script = [
        _ai("", tool_calls=[_call("explain_error", {"error_text": ERROR_TEXT}, "c1")]),
        _ai("证据够了。"),
    ]
    scripted_model.structured_script = [_draft()]
    fake_sandbox.script = [result_for("print(int('42'))", stdout="42\n")]

    values = await _run(_graph(saver, scripted_model, fake_sandbox))

    assert values["error_type"] == "ValueError"
    assert values["fixed_code"] == "print(int('42'))"
    assert values["change_summary"] == ["把字符串换成可解析的数字"]
    assert values["tool_rounds"] == 1
    assert values["repair_attempts"] == 1
    assert isinstance(values["last_verification"], SandboxResult)
    assert values["last_verification"].status == "ok"
    assert values["last_verification"].stdout == "42\n"

    # 首轮两条 + explain_error 两条 = 4，编号连续
    assert [chunk.chunk_id for chunk in values["chunks"]] == [
        "exceptions#valueerror",
        "exceptions#int",
        "exceptions#valueerror",
        "exceptions#int",
    ]
    assert [citation.index for citation in values["citations"]] == [1, 2, 3, 4]

    tool_message = next(m for m in values["messages"] if isinstance(m, ToolMessage))
    assert tool_message.tool_call_id == "c1"
    # explain_error 拿到节点算的全局起点：已有 2 条 → 从 [3] 开始
    assert _first_marker(tool_message.content) == "3"
    assert tool_message.artifact
    assert len(tool_message.artifact) == 2

    assert values["answer"].startswith("int() 收到无法解析的字符串")
    assert "**来源**" in values["answer"]
    assert values["answer"].count("](https://docs.python.org") == 4

    # 用量：两轮 agent（100+20） + 一轮结构化（40+20）
    assert values["usage"].prompt_tokens == 240
    assert values["usage"].completion_tokens == 60
    assert values["usage"].cached_tokens == 128
    assert isinstance(values["usage"].cost_usd, Decimal)

    # 模型只看到四种工具；system 唯一且在最前
    assert {spec["function"]["name"] for spec in scripted_model.bound_tool_specs[0]} == {
        "search_docs",
        "read_doc",
        "explain_error",
        "run_code",
    }
    first, second = scripted_model.requests
    assert isinstance(first[0], SystemMessage)
    assert isinstance(second[0], SystemMessage)
    assert first[0].content == second[0].content
    # 工具轮之后，最后一条是 ToolMessage（不重复塞片段块）
    assert isinstance(second[-1], ToolMessage)
    assert isinstance(second[1], HumanMessage)


def _first_marker(text: str) -> str:
    match = re.search(r"\[(\d+)\]", text)
    return "" if match is None else match.group(1)


# ---------- ② run_code 工具 + verify 复用同一结果（D4）----------


async def test_run_code_artifact_is_cached_and_verify_reuses_it(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    code = "print('hi')"
    scripted_model.script = [
        _ai("", tool_calls=[_call("run_code", {"code": code}, "c1")]),
        _ai("沙箱已确认。"),
    ]
    scripted_model.structured_script = [_draft(code)]
    fake_sandbox.script = [result_for(code, stdout="hi\n")]

    values = await _run(_graph(saver, scripted_model, fake_sandbox))

    # 工具跑过一次；verify 的 fixed_code 与它逐字节相同 → 命中缓存，不再起容器
    assert fake_sandbox.calls == [(code, "", None)]
    assert list(values["sandbox_results"]) == [sandbox_key(code)]
    assert isinstance(values["sandbox_results"][sandbox_key(code)], SandboxResult)
    assert values["last_verification"].stdout == "hi\n"

    tool_message = next(m for m in values["messages"] if isinstance(m, ToolMessage))
    assert tool_message.tool_call_id == "c1"
    assert "status=ok exit_code=0" in tool_message.content
    assert "hi" in tool_message.content
    # run_code 不产生引用编号：仍是首轮那两条
    assert [citation.index for citation in values["citations"]] == [1, 2]


# ---------- ③ 修复失败 → 换一段代码重试 ----------


async def test_failed_verification_retries_with_a_different_fix(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    scripted_model.script = [_ai("没有更多证据了。")]
    scripted_model.structured_script = [_draft("print(1)"), _draft("print(2)")]
    fake_sandbox.script = [
        result_for("print(1)", status="error", stderr="NameError: name 'x' is not defined\n"),
        result_for("print(2)", status="ok", stdout="2\n"),
    ]

    values = await _run(_graph(saver, scripted_model, fake_sandbox))

    assert fake_sandbox.calls == [("print(1)", "", None), ("print(2)", "", None)]
    assert values["repair_attempts"] == 2
    assert values["fixed_code"] == "print(2)"
    assert values["last_verification"].status == "ok"

    # 第二次修复必须看见上一次的失败结果（否则模型只会重复同一个错误）
    second_repair = scripted_model.structured_requests[1]
    assert isinstance(second_repair[-1], HumanMessage)
    assert "NameError" in second_repair[-1].content
    assert "上一次修复" in second_repair[-1].content


async def test_repair_stops_at_max_attempts_and_reports_failure(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    scripted_model.script = [_ai("没有更多证据了。")]
    scripted_model.structured_script = [_draft("print(1)"), _draft("print(2)")]
    fake_sandbox.script = [
        result_for("print(1)", status="error", stderr="boom-1\n"),
        result_for("print(2)", status="error", stderr="boom-2\n"),
    ]

    values = await _run(_graph(saver, scripted_model, fake_sandbox))

    assert values["repair_attempts"] == MAX_REPAIR_ATTEMPTS
    assert len(scripted_model.structured_requests) == MAX_REPAIR_ATTEMPTS
    # 失败也是结果：不抛异常，交给服务层原样返回（D2）
    assert values["last_verification"].status == "error"
    assert values["last_verification"].stderr == "boom-2\n"
    assert values["fixed_code"] == "print(2)"


async def test_timeout_is_a_result_not_an_exception(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    scripted_model.script = [_ai("没有更多证据了。")]
    scripted_model.structured_script = [_draft("while True: pass"), _draft("print(0)")]
    fake_sandbox.script = [
        result_for("while True: pass", status="timeout"),
        result_for("print(0)", status="timeout"),
    ]

    values = await _run(_graph(saver, scripted_model, fake_sandbox))

    assert values["last_verification"].status == "timeout"
    assert values["last_verification"].exit_code is None


# ---------- ④ 工具轮上限 ----------


async def test_tool_round_limit_stops_the_loop(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    call = _call("search_docs", {"query": "ValueError"}, "c")
    scripted_model.script = [
        _ai("", tool_calls=[{**call, "id": f"c{index}"}]) for index in range(MAX_TOOL_ROUNDS + 2)
    ]
    scripted_model.structured_script = [_draft()]
    fake_sandbox.script = [result_for("print(int('42'))", stdout="42\n")]

    values = await _run(_graph(saver, scripted_model, fake_sandbox))

    assert values["tool_rounds"] == MAX_TOOL_ROUNDS
    # `MAX + 1` 条在"到上限"那一轮被消费（它的 tool_calls 不再执行），
    # 之后的脚本一条都不该动 —— 否则就是绕过上限继续循环
    assert len(scripted_model.script) == 1
    assert values["last_verification"].status == "ok"


# ---------- ⑤ 并行工具调用的递增编号 ----------


async def test_two_tool_rounds_advance_the_global_index(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    scripted_model.script = [
        _ai("", tool_calls=[_call("explain_error", {"error_text": ERROR_TEXT}, "c1")]),
        _ai("", tool_calls=[_call("search_docs", {"query": "ValueError"}, "c2")]),
        _ai("够了。"),
    ]
    scripted_model.structured_script = [_draft()]
    fake_sandbox.script = [result_for("print(int('42'))", stdout="42\n")]

    values = await _run(_graph(saver, scripted_model, fake_sandbox))

    tool_messages = [m for m in values["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]
    # 已有 2 条 → explain 从 [3]；再加 2 条 → search 从 [5]
    assert [_first_marker(m.content) for m in tool_messages] == ["3", "5"]
    assert [citation.index for citation in values["citations"]] == [1, 2, 3, 4, 5, 6]


# ---------- ⑥ 检查点：读状态不重跑沙箱 ----------


async def test_aget_state_does_not_rerun_the_sandbox(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    scripted_model.script = [_ai("没有更多证据了。")]
    scripted_model.structured_script = [_draft()]
    fake_sandbox.script = [result_for("print(int('42'))", stdout="42\n")]

    diagnose = _graph(saver, scripted_model, fake_sandbox)
    await diagnose.graph.ainvoke(
        {"error_text": ERROR_TEXT, "broken_code": None}, _config("checkpoint")
    )
    assert len(fake_sandbox.calls) == 1

    first = dict((await diagnose.graph.aget_state(_config("checkpoint"))).values)
    second = dict((await diagnose.graph.aget_state(_config("checkpoint"))).values)

    assert len(fake_sandbox.calls) == 1
    # 冻结 dataclass / Decimal 穿过 serde 后仍是原类型（L3）
    assert isinstance(first["last_verification"], SandboxResult)
    assert isinstance(first["usage"].cost_usd, Decimal)
    assert first["fixed_code"] == second["fixed_code"] == "print(int('42'))"


# ---------- ⑦ 空检索 / 非法结构化输出 ----------


async def test_empty_retrieval_raises_before_calling_the_model(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    async def empty_search(query: str, top_k: int) -> list[RetrievedChunk]:
        return []

    scripted_model.script = [_ai("这条永远不该被生成。")]

    with pytest.raises(RetrievalEmpty):
        await _run(_graph(saver, scripted_model, fake_sandbox, search=empty_search))
    assert scripted_model.requests == []
    assert fake_sandbox.calls == []


async def test_empty_fixed_code_is_a_bad_response(
    saver: Any, scripted_model: ScriptedChatModel, fake_sandbox: FakeSandboxRunner
) -> None:
    scripted_model.script = [_ai("没有更多证据了。")]
    scripted_model.structured_script = [_draft("   ")]

    with pytest.raises(LLMBadResponse):
        await _run(_graph(saver, scripted_model, fake_sandbox))
    assert fake_sandbox.calls == []


def test_runner_is_required() -> None:
    with pytest.raises(ConfigError):
        build_diagnose_graph(checkpointer=None, model=ScriptedChatModel(), runner=None)


# ---------- ⑧ 连续两轮工具调用：reasoning_content 必须回传（respx）----------


def _completion(
    message: dict[str, Any],
    *,
    finish: str = "stop",
    usage: dict[str, Any] | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {
        "id": "cmpl",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL_FLASH,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", **message},
                "finish_reason": finish,
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


def _tool_message(
    call_id: str, name: str, arguments: dict[str, Any], *, reasoning: str
) -> httpx.Response:
    return _completion(
        {
            "content": "",
            "reasoning_content": reasoning,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        finish="tool_calls",
        usage=_usage(),
    )


def _usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 300,
        "completion_tokens": 30,
        "total_tokens": 330,
        "prompt_cache_hit_tokens": 128,
        "prompt_cache_miss_tokens": 172,
    }


@respx.mock
async def test_two_tool_rounds_keep_reasoning_content(
    saver: Any, fake_sandbox: FakeSandboxRunner
) -> None:
    code = "print(int('42'))"
    draft = _draft(code)
    respx.post(CHAT_URL).mock(
        side_effect=[
            _tool_message("call_1", "search_docs", {"query": "ValueError"}, reasoning="第一步"),
            _tool_message(
                "call_2", "explain_error", {"error_text": ERROR_TEXT}, reasoning="第二步"
            ),
            _completion({"content": "证据已足够。"}, usage=_usage()),
            # 结构化修复走 JSON 模式（`get_structured_output`）：回 JSON 正文，不是工具调用。
            # 走 function_calling 会绑 tool_choice，思考模式直接 400。
            _completion(
                {
                    "content": json.dumps(draft.model_dump(), ensure_ascii=False),
                    "reasoning_content": "出修复",
                },
                usage=_usage(),
            ),
        ]
    )
    fake_sandbox.script = [result_for(code, stdout="42\n")]

    model = get_chat_model(api_key=TEST_API_KEY)
    values = await _run(_graph(saver, model, fake_sandbox))  # type: ignore[arg-type]

    assert len(respx.calls) == 4
    third_payload = json.loads(respx.calls[2].request.content)
    assistants = [m for m in third_payload["messages"] if m["role"] == "assistant"]
    assert len(assistants) == 2
    # ← R1：少任何一个字段，DeepSeek 在第三轮直接 400
    assert [m.get("reasoning_content") for m in assistants] == ["第一步", "第二步"]

    # ← 修复轮走 JSON 模式：不能带 tool_choice / tools，否则思考模式 400
    #   `Thinking mode does not support this tool_choice`
    repair_payload = json.loads(respx.calls[3].request.content)
    assert repair_payload["response_format"] == {"type": "json_object"}
    assert "tool_choice" not in repair_payload
    assert "tools" not in repair_payload
    # json_mode 不随请求发 schema，格式说明必须并进末条 human 消息
    assert "fixed_code" in repair_payload["messages"][-1]["content"]

    assert values["error_type"] == "ValueError"
    assert values["last_verification"].status == "ok"
    assert fake_sandbox.calls == [(code, "", None)]
