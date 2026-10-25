"""AGENTS.md §3.2 的异常层级。

这个文件**只定义异常**，不 import 任何 Web 框架 —— 领域层（rag / sandbox / services）
要能 `raise` 它而不依赖 FastAPI。把它翻译成 HTTP 是 api/errors.py 的唯一职责。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar


class ErrorCode(StrEnum):
    """§3.2 的枚举。`Envelope.error.code` 只能取这里的值（§3.1）。

    取值的形态是 UPPER_SNAKE，与契约示例 ``error.code == "RETRIEVAL_EMPTY"`` 一致；
    而类名是 PascalCase，二者之间**有且只有** api/errors.py 的映射表做转换。
    用 StrEnum 而非 Enum：它序列化后就是字符串，少一层 .value。
    """

    CONFIG_ERROR = "CONFIG_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RETRIEVAL_EMPTY = "RETRIEVAL_EMPTY"
    CORPUS_MISSING = "CORPUS_MISSING"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
    LLM_INSUFFICIENT_BALANCE = "LLM_INSUFFICIENT_BALANCE"
    LLM_MODEL_NOT_FOUND = "LLM_MODEL_NOT_FOUND"
    LLM_BAD_RESPONSE = "LLM_BAD_RESPONSE"
    SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
    SANDBOX_REJECTED = "SANDBOX_REJECTED"
    STORE_ERROR = "STORE_ERROR"

    # ↓↓↓ 以下四条是传输层的码：由框架在路由匹配 / 限流阶段抛出，不经业务代码。
    #     §3.2 原本没有它们，而 §3.1 禁止 error.code 自造字符串，于是 404 / 405 / 429 /
    #     兜底 500 无码可用。2026-10-03 **已批准增补**（属新增，不改既有码的语义），
    #     已落地进 AGENTS.md §3.2；见 docs/decisions/0002-信封与异常映射.md 决定 1。
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """§3.2 异常树的根。**基类不直接 raise**（由 test_error_mapping.py 断言）。

    若真的抛了基类，映射表会把它当未分类错误落 500 —— 这是有意的兜底，不是漏洞。
    """

    # ClassVar 而非实例字段：子类用**完全相同的注解**覆盖它，mypy --strict 才不报错。
    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        # 仅"映射到 200 的三个异常"用它携带成功载荷（data）。
        # 其余异常走 details（进 error.details）。二者语义不同，不合并 —— 见要点 3。
        # 声明成实例字段（不是类字段）：类级别的可变类型注解会被 RUF012 拦下。
        self.data: dict[str, Any] | None = None
        # 429 / 503 的响应可能带 Retry-After；放基类避免在处理器里做 isinstance 分支。
        self.retry_after: int | None = None


class ConfigError(AppError):
    """配置缺失或不可用。→ 500（这是**运维错误**，不是用户输入错误）。"""

    code: ClassVar[ErrorCode] = ErrorCode.CONFIG_ERROR


class ValidationError(AppError):
    """业务层输入校验失败。→ 422。与 pydantic 的 ValidationError 同名但无关（待定项 10）。"""

    code: ClassVar[ErrorCode] = ErrorCode.VALIDATION_ERROR


class RetrievalError(AppError):
    """检索类异常的中转基类，**不直接抛**。"""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR


class RetrievalEmpty(RetrievalError):
    """→ **200**（§3.2）。"文档里没有"是正常业务结果，不是故障。data 携带查询回显。"""

    code: ClassVar[ErrorCode] = ErrorCode.RETRIEVAL_EMPTY

    def __init__(
        self, message: str = "未在语料中检索到相关内容", *, query: str | None = None
    ) -> None:
        super().__init__(message)
        self.data = {"status": "empty", "query": query}


class CorpusMissing(RetrievalError):
    """语料库缺失。→ 503。"""

    code: ClassVar[ErrorCode] = ErrorCode.CORPUS_MISSING


class LLMError(AppError):
    """上游模型类异常的中转基类，**不直接抛**。"""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR


class LLMTimeout(LLMError):
    """→ 504。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_TIMEOUT


class LLMRateLimited(LLMError):
    """→ 429，且**透传 Retry-After**（§3.2）。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_RATE_LIMITED

    def __init__(
        self,
        message: str = "上游限流，请稍后重试",
        *,
        retry_after: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after


class LLMInsufficientBalance(LLMError):
    """→ 503。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_INSUFFICIENT_BALANCE


class LLMModelNotFound(LLMError):
    """→ 500。注意与 ADR 0001 的区别：模型名**漂移**只告警，**不存在**才抛这个。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_MODEL_NOT_FOUND


class LLMBadResponse(LLMError):
    """→ 502。"""

    code: ClassVar[ErrorCode] = ErrorCode.LLM_BAD_RESPONSE


class SandboxError(AppError):
    """沙箱类异常的中转基类，**不直接抛**。"""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR


class SandboxTimeout(SandboxError):
    """→ **200**（§3.2）。超时是正常业务结果，data 带可展示的部分输出。"""

    code: ClassVar[ErrorCode] = ErrorCode.SANDBOX_TIMEOUT

    def __init__(self, message: str = "执行超时", *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.data = {"status": "timeout", "stdout": stdout, "stderr": stderr}


class SandboxRejected(SandboxError):
    """→ **200**（§3.2）。代码被安全边界拒绝是正常业务结果。"""

    code: ClassVar[ErrorCode] = ErrorCode.SANDBOX_REJECTED

    def __init__(self, message: str = "代码未通过安全检查", *, reason: str | None = None) -> None:
        super().__init__(message)
        self.data = {"status": "rejected", "reason": reason}


class StoreError(AppError):
    """状态存储（checkpointer / store）异常。→ 500。"""

    code: ClassVar[ErrorCode] = ErrorCode.STORE_ERROR


# ---------- 阶段八：v3 / v4（SPEC D10）----------
# 不新增 `ErrorCode` 取值：`NOT_FOUND` / `RATE_LIMITED` 已在 §3.2 登记为传输层码。
# 这里把它们的语义扩展到业务资源与配额，按 AGENTS.md §6 留 ADR 0003。


class ExerciseNotFound(AppError):
    """`exercise_id` 在检查点里不存在。→ **404 + `NOT_FOUND`**。

    这是**调用方错误**（用了一个不存在的题目 id），不是"用户代码失败是实验结果"
    那一类语义（§3.2）。两者混用会让 404 与 200 的边界消失。
    """

    code: ClassVar[ErrorCode] = ErrorCode.NOT_FOUND

    def __init__(self, exercise_id: str) -> None:
        super().__init__("练习不存在或已过期", details={"exercise_id": exercise_id})


class QuotaExceeded(AppError):
    """单日 Token 达到上限。→ **429 + `RATE_LIMITED` + `Retry-After`**。

    与 `LLMRateLimited` 是两件事：那是**上游**限我们，这是**我们**限调用方（§3.2）。
    混用会让"谁被限流了"在报表里消失。
    """

    code: ClassVar[ErrorCode] = ErrorCode.RATE_LIMITED

    def __init__(self, *, retry_after: int, limit: int, used: int) -> None:
        super().__init__(
            "今日 Token 额度已用尽，请明日再试",
            details={"limit": limit, "used": used},
        )
        self.retry_after = max(1, retry_after)
