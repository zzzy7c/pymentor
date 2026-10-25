"""★ 契约冻结（AGENTS.md §4）。

改本文件的字段 = 破坏性变更，必须同时：
  1. bump API_VERSION；
  2. 更新 frontend/ 的消费代码（阶段八）；
  3. 更新 tests/snapshots/envelope.json（否则 test_envelope.py 立刻变红）。

本文件**不 import starlette / fastapi** —— 它是纯数据契约，能被任何层引用。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer

from pyassist.errors import ErrorCode
from pyassist.llm.tracing import UsageSnapshot

# 契约版本。前端与集成方按它判断兼容性。变更规则见文件头。
API_VERSION = "v1"


class Usage(BaseModel):
    """§3.1 的 meta.usage。字段名与 llm/tracing.UsageSnapshot **逐字一致**（刻意的缝）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    is_off_peak: bool = True

    @field_serializer("cost_usd")
    def _serialize_cost(self, value: Decimal) -> float:
        """pydantic 2.13.5 默认把 Decimal 序列化成**字符串**（实测 "0.0004"）。

        契约示例是数字 0.0004，故必须显式转 float。
        round 到 8 位：避免 Decimal→float 的二进制尾巴进快照。
        """
        return round(float(value), 8)


class ErrorDetail(BaseModel):
    """§3.1 的 error。`code` 的类型就是枚举本身，故不可能出现枚举外的值。"""

    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class Meta(BaseModel):
    """§3.1 的 meta。三字段**永远在位**；缺 usage 时填零值而非省略（示例已同步一致）。"""

    request_id: str
    latency_ms: int
    usage: Usage = Field(default_factory=Usage)


class Envelope[T](BaseModel):
    """§3.1 的响应信封。成功与失败**共用**这一个形状，只有 ok / data / error 三者互换。"""

    ok: bool
    data: T | None = None
    error: ErrorDetail | None = None
    meta: Meta


class Page[T](BaseModel):
    """§3.1 的列表口径：列表放 data.items，绝不裸数组；分页带 next_cursor。"""

    items: list[T]
    total: int
    next_cursor: str | None = None


class HealthData(BaseModel):
    """GET /health 的 data。契约允许 data 是任意对象，这里定死它的形状。"""

    status: str  # "ok" | "degraded"
    api_version: str
    database_ok: bool
    model_check_ok: bool | None  # None = 尚未校验过（进程刚起）
    checked_at: str  # ISO8601
    # 阶段八追加：沙箱不可用只降级 /health，不影响 v1（SPEC §5.12）。None = 未探测。
    sandbox_ok: bool | None = None


# ---------- 阶段六：v1 文档问答 ----------
# **只追加新的请求/响应模型**，既有字段一个没改 —— 按 AGENTS.md §6 第 3 条，
# 这不是破坏性变更，故 API_VERSION 保持 "v1"（SPEC §5.10）。


class ChatRequest(BaseModel):
    """`POST /chat` 的请求体。

    `max_length=2_000`：超长提问直接变成 422 真信封，而不是流到一半才炸 ——
    流一旦开始就改不了状态码（SPEC D6 的同一逻辑）。
    """

    question: str = Field(min_length=1, max_length=2_000)
    session_id: str | None = Field(default=None, max_length=64)


class KnowledgeRequest(ChatRequest):
    """`POST /knowledge` 与 `/chat` 同参：两个路由共用同一条服务路径（D10）。"""


class CitationData(BaseModel):
    """一条结构化引用。`url` 是**带锚点**的可点击地址。"""

    index: int
    title: str
    section: str | None
    url: str
    snippet: str


class QAData(BaseModel):
    """`POST /knowledge` 的 data。

    `status="empty"` 与 `ok=true` 同时出现是刻意的：检索不到是**正常业务结果**
    （§3.2 把 `RetrievalEmpty` 映射到 200），不是故障。
    """

    answer: str
    citations: list[CitationData]
    status: Literal["ok", "empty"]


# ---------- 阶段七：v2 错误诊断 ----------
# 同样**只追加**：既有模型与 API_VERSION 均不变（AGENTS.md §6 第 3 条）。


class DiagnoseRequest(BaseModel):
    """`POST /diagnose` 的请求体。`error_text` 必填，`code` 是用户原来的坏代码（可选）。"""

    error_text: str = Field(min_length=1, max_length=8_000)
    code: str | None = Field(default=None, max_length=8_000)
    session_id: str | None = Field(default=None, max_length=64)


class SandboxData(BaseModel):
    """一次沙箱验证的可展示结果。`stdout/stderr` 已在 runner 内截断（D3）。"""

    status: Literal["ok", "error", "timeout", "rejected"]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    is_truncated: bool


class DiagnosisData(BaseModel):
    """`POST /diagnose` 的 data。

    `status="unverified"` 表示**没有**沙箱结果（例如沙箱不可用导致验证未发生）——
    此时 `verification` 为 null，前端不应把它展示成"修复失败"。
    """

    status: Literal["ok", "error", "timeout", "rejected", "unverified"]
    error_type: str | None
    explanation: str
    fixed_code: str | None
    change_summary: list[str]
    verification: SandboxData | None
    citations: list[CitationData]


# ---------- 阶段八：v3 练习 / v4 推荐 / 历史 / 统计 ----------
# 只追加（AGENTS.md §6 第 3 条）：既有字段一个不改，API_VERSION 保持 "v1"。

ExerciseTopic = Literal["list", "dict", "decorator", "async"]


class ExerciseRequest(BaseModel):
    """`POST /exercises` 的请求体。"""

    topic: ExerciseTopic
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    session_id: str | None = Field(default=None, max_length=64)


class ExerciseData(BaseModel):
    """`POST /exercises` 的 data。

    **刻意没有 `tests` / `reference_solution`**：把测试下发给客户端等于让用户
    照着断言写死值（D2 / 守护 #39）。
    """

    exercise_id: str
    topic: ExerciseTopic
    title: str
    prompt: str
    starter_code: str


class SubmitRequest(BaseModel):
    """`POST /exercises/{id}/submit` 的请求体。"""

    code: str = Field(min_length=1, max_length=8_000)
    session_id: str | None = Field(default=None, max_length=64)


class TestOutcomeData(BaseModel):
    """单条测试结果。`status="error"` 是用户代码在测试里抛了异常（不是服务错误）。"""

    name: str
    status: Literal["passed", "failed", "error", "skipped"]
    message: str
    duration_ms: int


class FeedbackItem(BaseModel):
    """可解释反馈：指出错在哪、为什么、怎么改（§5.4，不许只给分数）。"""

    where: str
    why: str
    hint: str


class SubmissionData(BaseModel):
    """`POST /exercises/{id}/submit` 的 data。"""

    exercise_id: str
    status: Literal["passed", "failed"]
    is_passed: bool
    score: int
    tests: list[TestOutcomeData]
    stdout: str
    stderr: str
    duration_ms: int
    summary: str
    feedback: list[FeedbackItem]
    citations: list[CitationData]


class HistoryItem(BaseModel):
    """`GET /history` 的一行。`submitted_code` 供前端回放（§5.4）。"""

    attempt_id: str
    exercise_id: str
    topic: str
    is_passed: bool
    score: int | None
    submitted_code: str
    stdout: str | None
    stderr: str | None
    duration_ms: int | None
    created_at: str


class TopicCount(BaseModel):
    topic: str
    count: int


class StatsData(BaseModel):
    """`GET /stats` 的 data。`pass_rate` 在没有提交时是 `0.0`，不是 NaN。"""

    today_prompt_tokens: int
    today_completion_tokens: int
    today_cached_tokens: int
    today_cost_usd: Decimal
    total_attempts: int
    passed_attempts: int
    pass_rate: float
    mistake_total: int
    unresolved_mistakes: int
    mistakes_by_topic: list[TopicCount]
    quota_limit: int | None
    quota_used: int
    quota_remaining: int | None
    is_exhausted: bool

    @field_serializer("today_cost_usd")
    def _serialize_cost(self, value: Decimal) -> float:
        """与 `Usage.cost_usd` 同一口径：契约里是数字，不是 Decimal 字符串。"""
        return round(float(value), 8)


class RecommendRequest(BaseModel):
    """`POST /recommend` 的请求体。**不让客户端传错题**（§5.13）。"""

    session_id: str | None = Field(default=None, max_length=64)


class RecommendationData(BaseModel):
    topic: str
    reason: str
    exercise_ids: list[str]
    next_steps: list[str]
    citations: list[CitationData]


def usage_to_schema(snapshot: UsageSnapshot) -> Usage:
    """`UsageSnapshot` → 契约里的 `Usage`。HTTP 契约的转换属于契约模块。

    阶段六这函数住在 `services/qa.py`；阶段七的诊断路由也要用同一条转换，
    放在服务层会让第二条路径各写一份（SPEC §9.6）。
    """
    return Usage(
        prompt_tokens=snapshot.prompt_tokens,
        completion_tokens=snapshot.completion_tokens,
        cached_tokens=snapshot.cached_tokens,
        cost_usd=snapshot.cost_usd,
        is_off_peak=snapshot.is_off_peak,
    )


# ---------- 信封构造器（模块级函数，不是 classmethod）----------
# 理由：泛型 classmethod 的返回类型在 mypy --strict 下推断得很差
# （cls 参数无法绑定 T）。模块级函数 + PEP 695 泛型参数能精确推断。


def envelope_success[T](data: T, *, meta: Meta) -> Envelope[T]:
    """成功信封。也是那三个"映射到 200"的异常要用的形状（ok=True）。"""
    return Envelope[T](ok=True, data=data, error=None, meta=meta)


def envelope_failure(error: ErrorDetail, *, meta: Meta) -> Envelope[dict[str, Any]]:
    """失败信封。data 恒为 None（§3.1：失败时 data 不承载业务载荷）。"""
    return Envelope[dict[str, Any]](ok=False, data=None, error=error, meta=meta)
