"""工具注册表（SPEC §5.17）。判据 9 / 11 / L8 / L16。

四件事必须钉住，它们各自对应一个"能静默坏掉"的点：

1. **工具名 == 函数名**（L8）：`@tool` 会拿函数名当工具名，但一旦有人包了一层
   （`search_docs` → `do_search`），模型看到的与代码里的就对不上，而调用仍然"成功"；
2. **artifact 只给产出结构化结果的工具**（L16）：`read_doc` 若也有 artifact，
   一页几十个 chunk 会整页灌进检查点；
3. **注入参数不出现在 schema 里**：`top_k` / `searcher` / `runner` 若进了 schema，
   模型会试着填它们 —— `run_code` 尤其危险：模型不该能指定"在哪个沙箱跑"；
4. **按图取子集**（D7）：v1 看不到 `run_code`，v2 四项齐全。
"""

from __future__ import annotations

import pytest

from pyassist.agent.registry import (
    EXPLAIN_ERROR,
    GENERATE_EXERCISE,
    GRADE_CODE,
    READ_DOC,
    RUN_CODE,
    RUN_TESTS,
    SEARCH_DOCS,
    TOOLS,
    get_tool,
    tool_specs,
)
from pyassist.config import (
    DIAGNOSE_TOOL_NAMES,
    EXERCISE_TOOL_NAMES,
    QA_TOOL_NAMES,
)

_ALL_INJECTED = {
    "top_k",
    "start_index",
    "searcher",
    "fetcher",
    "runner",
    "cache",
    "exercise",
    "report",
    "model",
}


def test_tool_name_equals_function_name() -> None:
    for candidate in TOOLS:
        assert candidate.coroutine is not None
        assert candidate.coroutine.__name__ == candidate.name
    assert SEARCH_DOCS.name == "search_docs"
    assert READ_DOC.name == "read_doc"
    assert EXPLAIN_ERROR.name == "explain_error"
    assert RUN_CODE.name == "run_code"
    assert GENERATE_EXERCISE.name == "generate_exercise"
    assert RUN_TESTS.name == "run_tests"
    assert GRADE_CODE.name == "grade_code"


def test_artifact_is_only_on_tools_that_produce_structured_results() -> None:
    """逐工具断言，**不是** `all(...)` —— 那样"给所有工具都加"也会绿（L16）。"""
    assert SEARCH_DOCS.response_format == "content_and_artifact"
    assert EXPLAIN_ERROR.response_format == "content_and_artifact"
    assert RUN_CODE.response_format == "content_and_artifact"
    assert READ_DOC.response_format == "content"
    # v3 的三件都产出结构化 artifact（草稿 / 报告 / 草稿），不是给模型看的字符串
    assert GENERATE_EXERCISE.response_format == "content_and_artifact"
    assert RUN_TESTS.response_format == "content_and_artifact"
    assert GRADE_CODE.response_format == "content_and_artifact"


def test_injected_args_are_hidden_from_schema() -> None:
    assert SEARCH_DOCS._injected_args_keys == {"top_k", "start_index", "searcher"}
    assert EXPLAIN_ERROR._injected_args_keys == {"top_k", "start_index", "searcher"}
    assert READ_DOC._injected_args_keys == {"fetcher"}
    assert RUN_CODE._injected_args_keys == {"runner", "cache"}
    assert GENERATE_EXERCISE._injected_args_keys == {"model"}
    assert RUN_TESTS._injected_args_keys == {"exercise", "runner", "cache"}
    assert GRADE_CODE._injected_args_keys == {"exercise", "report", "model"}

    schema_names = {spec["function"]["name"] for spec in tool_specs()}
    assert schema_names == {tool.name for tool in TOOLS}
    for spec in tool_specs():
        properties = set(spec["function"]["parameters"].get("properties", {}))
        assert properties.isdisjoint(_ALL_INJECTED), f"{spec['function']['name']} 泄漏了注入参数"


def test_run_code_exposes_only_the_code_argument() -> None:
    spec = next(item for item in tool_specs() if item["function"]["name"] == "run_code")
    assert set(spec["function"]["parameters"]["properties"]) == {"code"}


def test_v3_tools_expose_only_model_visible_arguments() -> None:
    by_name = {item["function"]["name"]: item for item in tool_specs()}
    assert set(by_name["generate_exercise"]["function"]["parameters"]["properties"]) == {
        "topic",
        "attempt_feedback",
    }
    assert set(by_name["run_tests"]["function"]["parameters"]["properties"]) == {"code"}
    assert set(by_name["grade_code"]["function"]["parameters"]["properties"]) == {"code"}


def test_tool_specs_correspond_to_tools() -> None:
    specs = tool_specs()
    assert [spec["function"]["name"] for spec in specs] == [tool.name for tool in TOOLS]


def test_tool_specs_subsets_per_graph() -> None:
    """判据 11：v1 拿不到 `run_code`（tools 节点没注入 runner），v2 四项齐全。"""
    qa_names = [spec["function"]["name"] for spec in tool_specs(QA_TOOL_NAMES)]
    assert qa_names == ["search_docs", "read_doc"]
    assert "run_code" not in qa_names

    diagnose_names = [spec["function"]["name"] for spec in tool_specs(DIAGNOSE_TOOL_NAMES)]
    assert diagnose_names == ["search_docs", "read_doc", "explain_error", "run_code"]

    exercise_names = [spec["function"]["name"] for spec in tool_specs(EXERCISE_TOOL_NAMES)]
    assert exercise_names == ["generate_exercise", "run_tests", "grade_code"]
    # v3 的模型不该看到检索/沙箱/诊断工具：它们由节点直接调，给模型只增加误调路径
    assert set(exercise_names).isdisjoint({"search_docs", "read_doc", "run_code"})


def test_tool_specs_unknown_name_raises_key_error() -> None:
    """构图期就炸，别等模型调用了才发现工具名拼错（D7）。"""
    with pytest.raises(KeyError):
        tool_specs(["search_docs", "不存在的工具"])


def test_tool_specs_are_copies() -> None:
    """调用方改写顶层字段不能污染下一次（`bind_tools` 可能就地改 dict）。"""
    mutated = tool_specs()
    mutated[0]["type"] = "mutated"
    assert tool_specs()[0]["type"] != "mutated"


def test_get_tool_roundtrip_and_unknown_name() -> None:
    for candidate in TOOLS:
        assert get_tool(candidate.name) is candidate
    with pytest.raises(KeyError):
        get_tool("不存在的工具")
