"""★ 唯一图状态 schema（AGENTS.md §2）。每张图都从这里扩展，禁止各图自定义互不兼容的 State。

两条设计决定（SPEC D2 / D7）值得在这里说清：

1. **检索层的对象直接进状态**（`RetrievedChunk` / `Citation` / `UsageSnapshot`），
   不是"存 dict 再反序列化"的胶水。检查点 serde 对 frozen dataclass 与 `Decimal`
   都能原样还原（L3 实测），代价是必须有一条往返测试钉住它 —— 这个坑只在"恢复"时才炸。
2. **除 `messages` 外一律覆盖**（`_overwrite`）。检查点会把上一轮的 `chunks / citations /
   tool_rounds / usage / answer` 带过来；若是追加，同一 thread 的第二问就会带着上一问的
   片段回答 —— 既污染答案又抬成本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import MessagesState

from pyassist.agent.tools.docs import ErrorSignature
from pyassist.llm.tracing import UsageSnapshot
from pyassist.rag.citations import Citation
from pyassist.rag.retriever import RetrievedChunk
from pyassist.sandbox.runner import SandboxResult

QaEventType = Literal["token", "citations", "tool", "error", "done"]


class QaEvent(TypedDict, total=False):
    """节点写给 `get_stream_writer()` 的事件。**这是图对外的唯一出口形状**（SPEC D5）。

    加事件类型必须同时改 `services/qa.py` 与 `api/routers/chat.py` 的 `_PAYLOAD_KEYS`，
    否则线上格式会静默变化（`test_sse` 会红）。
    """

    type: QaEventType
    text: str  # token
    citations: list[dict[str, Any]]  # citations：已序列化，路由不再加工
    phase: str  # tool: "start" | "end"
    name: str  # tool
    code: str  # error（取自 ErrorCode）
    message: str  # error
    details: dict[str, Any] | None  # error
    request_id: str  # done
    usage: dict[str, Any]  # done


def _overwrite[T](old: T, new: T) -> T:
    """每轮**覆盖**而非累积（SPEC D2 / D7）。具名函数而非 lambda：栈里看得见名字。"""
    return new


class AppState(MessagesState):
    """所有图共享的底座。`messages` 之外的一切都是"本轮的中间产物"。

    继承 `MessagesState` 而不是自己写 `messages` 注解：后者的 reducer
    （`add_messages`）是本项目唯一不能写错的 reducer —— 它负责补 id、把
    `AIMessage(tool_calls)` 与 `ToolMessage` 正确配对。抄一份注解等于抄一份出错的可能。
    """

    chunks: Annotated[list[RetrievedChunk], _overwrite]
    citations: Annotated[list[Citation], _overwrite]
    usage: Annotated[UsageSnapshot, _overwrite]
    tool_rounds: Annotated[int, _overwrite]
    answer: Annotated[str, _overwrite]


def _merge_results(
    old: dict[str, SandboxResult], new: dict[str, SandboxResult]
) -> dict[str, SandboxResult]:
    """沙箱结果的**合并** reducer（SPEC D4）。这里刻意不是覆盖：

    `tools` 节点可能返回本轮新增的几条，`verify` 节点返回另一条；若覆盖，
    谁后写谁留下，前面执行过的结果就丢了 —— 重试时同一份代码会被重新跑一遍，
    既慢又让"同 thread 不重复执行"的幂等性破功。键是 `sandbox_key`，
    同一份代码（含 stdin / 限额）天然同键。
    """
    return {**old, **new}


class QAState(AppState):
    """v1 文档问答图的状态：底座 + 本轮问题。"""

    question: Annotated[str, _overwrite]


class DiagnoseState(AppState):
    """v2 诊断图的状态（SPEC D9）。

    `usage` / `messages` / `chunks` / `citations` 全部继承自 `AppState`，
    **不在这里重定义** —— 重定义会让两张图的 reducer 悄悄分叉。
    """

    error_text: Annotated[str, _overwrite]
    broken_code: Annotated[str | None, _overwrite]
    error_signature: Annotated[ErrorSignature | None, _overwrite]
    sandbox_results: Annotated[dict[str, SandboxResult], _merge_results]
    last_verification: Annotated[SandboxResult | None, _overwrite]
    repair_attempts: Annotated[int, _overwrite]
    error_type: Annotated[str | None, _overwrite]
    explanation: Annotated[str, _overwrite]
    fixed_code: Annotated[str | None, _overwrite]
    change_summary: Annotated[list[str], _overwrite]


# ---------- 阶段八：v3 练习 / v4 推荐（SPEC §5.2）----------

Topic = Literal["list", "dict", "decorator", "async"]


@dataclass(frozen=True, slots=True)
class FeedbackIssue:
    """一条可解释的反馈：错在哪、为什么错、怎么改（AGENTS.md §5.4）。

    **没有分数字段。** 分数由测试报告确定性计算（SPEC D5），模型无权改判。
    """

    where: str
    why: str
    hint: str


@dataclass(frozen=True, slots=True)
class TestCaseSpec:
    """一道题的一条测试。`name` 只允许合法标识符，`source` 是完整方法源码。"""

    name: str
    source: str


@dataclass(frozen=True, slots=True)
class ExerciseSpec:
    """练习的**唯一真相**。只存检查点（SPEC D2），不下发客户端、不建表。

    `exercise_id` 就是生成图那一次的 `thread_id` —— 不另立一套题目身份。
    """

    exercise_id: str
    topic: Topic
    title: str
    prompt: str
    starter_code: str
    reference_solution: str
    tests: list[TestCaseSpec]
    self_check_attempts: int


@dataclass(frozen=True, slots=True)
class TestOutcome:
    """一条用例的结果。`message` 只在失败/错误时非空，且已截断尾段。"""

    name: str
    status: Literal["passed", "failed", "error", "skipped"]
    message: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class TestReport:
    """沙箱测试报告的解析结果（SPEC D4）。

    `is_complete=False` 表示**没拿到结果标记**（用户代码崩在顶层 / 超时 / 沙箱故障）：
    此时各计数为 0，`is_passed` 必为假。
    """

    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    outcomes: list[TestOutcome]
    duration_ms: int
    is_complete: bool


@dataclass(frozen=True, slots=True)
class MistakeRecord:
    """`db.models.Mistake` 的只读投影。

    **不让 ORM 对象进检查点**：检查点不该依赖 SQLAlchemy 的 session 生命周期（§5.2）。
    """

    id: str
    exercise_id: str | None
    topic: str
    error_signature: str | None
    is_resolved: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """`db.models.ExerciseAttempt` 的只读投影（记忆聚合用，不含完整代码）。"""

    exercise_id: str
    topic: str
    is_passed: bool
    score: int | None
    created_at: str


class ExerciseState(AppState):
    """v3 生成图的状态。入口节点必须补齐 `AppState` 的五个键（SPEC §5.2）。"""

    topic: Annotated[Topic, _overwrite]
    exercise: Annotated[ExerciseSpec | None, _overwrite]
    self_check_report: Annotated[TestReport | None, _overwrite]
    generation_attempts: Annotated[int, _overwrite]
    generation_feedback: Annotated[str, _overwrite]
    error_message: Annotated[str | None, _overwrite]
    sandbox_results: Annotated[dict[str, SandboxResult], _merge_results]


class GradingState(AppState):
    """v3 批改图的状态。输入是服务层从生成图检查点取出的 `ExerciseSpec`。"""

    exercise: Annotated[ExerciseSpec | None, _overwrite]
    submitted_code: Annotated[str, _overwrite]
    report: Annotated[TestReport | None, _overwrite]
    grade_summary: Annotated[str, _overwrite]
    issues: Annotated[list[FeedbackIssue], _overwrite]
    doc_queries: Annotated[list[str], _overwrite]
    sandbox_results: Annotated[dict[str, SandboxResult], _merge_results]
    last_sandbox_result: Annotated[SandboxResult | None, _overwrite]


class ProgressState(AppState):
    """v4 推荐图的状态。候选集来自数据库，不来自客户端（SPEC D7）。"""

    user_id: Annotated[str, _overwrite]
    mistakes: Annotated[list[MistakeRecord], _overwrite]
    profile_block: Annotated[str, _overwrite]
    topic: Annotated[str | None, _overwrite]
    reason: Annotated[str, _overwrite]
    exercise_ids: Annotated[list[str], _overwrite]
    next_steps: Annotated[list[str], _overwrite]
