"""★ 唯一工具定义点（AGENTS.md §2）。`tool(...)` 只在这个文件出现。

**工具名 == 实现名 == `__name__`**（§2「不允许 search_docs → do_search 这种映射层」）。
后者由 `tests/test_registry.py` 用 `.coroutine.__name__` 断言。

`response_format="content_and_artifact"` 给 `search_docs` / `explain_error` / `run_code`：
`ToolMessage.content` 给模型看（带编号的片段文本 / 执行输出），`ToolMessage.artifact`
给图看（原始 `RetrievedChunk` 列表 / `SandboxResult`）。两者由**同一个**函数产出，
编号与结果不会漂移。`read_doc` 用默认的 `"content"` —— 一页几十个 chunk 不该灌进状态。

**工具集按图取子集**（D7）：`TOOLS` 是全集，`tool_specs(names)` 才是图看到的口径。
v1 的 agent 节点若拿到 `run_code` 而 tools 节点没注入 runner，模型一调就崩 ——
这不是"多给点能力"，是给了一条必炸的路径。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from langchain_core.tools import BaseTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool

from pyassist.agent.tools.docs import explain_error, read_doc, search_docs
from pyassist.agent.tools.grading import generate_exercise, grade_code
from pyassist.agent.tools.progress import recommend_next
from pyassist.agent.tools.sandbox_tools import run_code, run_tests

SEARCH_DOCS: Final = tool(search_docs, response_format="content_and_artifact")
READ_DOC: Final = tool(read_doc)  # 默认 "content"：read_doc 不产生引用（SPEC §5.3）
EXPLAIN_ERROR: Final = tool(explain_error, response_format="content_and_artifact")
RUN_CODE: Final = tool(run_code, response_format="content_and_artifact")

# 阶段八。三者的 artifact 分别是 UsageSnapshot / TestReport / UsageSnapshot；
# `run_tests` 的第一元是给模型看的文本，另两个的第一元是结构化草稿（节点直接消费）。
GENERATE_EXERCISE: Final = tool(generate_exercise, response_format="content_and_artifact")
RUN_TESTS: Final = tool(run_tests, response_format="content_and_artifact")
GRADE_CODE: Final = tool(grade_code, response_format="content_and_artifact")
RECOMMEND_NEXT: Final = tool(recommend_next, response_format="content_and_artifact")

TOOLS: Final[tuple[BaseTool, ...]] = (
    SEARCH_DOCS,
    READ_DOC,
    EXPLAIN_ERROR,
    RUN_CODE,
    GENERATE_EXERCISE,
    RUN_TESTS,
    GRADE_CODE,
    RECOMMEND_NEXT,
)

_SPECS: Final[dict[str, dict[str, Any]]] = {
    candidate.name: convert_to_openai_tool(candidate) for candidate in TOOLS
}


def tool_specs(names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """传给 `model.bind_tools()` 的 OpenAI 风格 tools 数组。

    `names=None` 返回**全部**（守护断言与文档展示用）；否则按 `names` 的顺序取子集。
    未注册的名字抛 `KeyError` —— 构图期就炸，别等模型调用了才发现（D7）。
    返回**拷贝**：调用方（`bind_tools`）可能改写，别让缓存被污染。
    """
    if names is None:
        return [dict(spec) for spec in _SPECS.values()]
    selected: list[dict[str, Any]] = []
    for name in names:
        spec = _SPECS.get(name)
        if spec is None:
            raise KeyError(f"未注册的工具：{name!r}")
        selected.append(dict(spec))
    return selected


def get_tool(name: str) -> BaseTool:
    """按名字取工具。`tools` 节点用它把 `tool_call["name"]` 映射回可调用的对象。

    未注册的名字抛 `KeyError` —— 由 `services/qa.py` 翻译成 `LLMBadResponse`（502），
    节点里不做 try/except（错误映射只有一处）。
    """
    for candidate in TOOLS:
        if candidate.name == name:
            return candidate
    raise KeyError(f"未注册的工具：{name!r}")
