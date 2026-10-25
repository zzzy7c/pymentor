"""v4 的推荐工具（SPEC §5.5 / D7）。

模型只产 `NextStepDraft`；**引用 id 的严格校验在图层**，因为那里同时拿得到
候选集与状态。候选为空时不进模型 —— 新用户没有错题是正常状态，不是检索失败。

本文件不碰数据库：`mistakes` 由服务层从 `repositories.list_mistakes` 读出后注入。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from decimal import Decimal
from functools import cache
from typing import Annotated

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import InjectedToolArg
from pydantic import BaseModel, Field

from pyassist.agent.state import MistakeRecord
from pyassist.config import (
    OUTPUT_LANGUAGE,
    PROGRESS_CONTEXT_PROMPT_NAME,
    PROGRESS_PROMPT_NAME,
)
from pyassist.errors import ConfigError, LLMBadResponse
from pyassist.llm.client import get_structured_output
from pyassist.llm.prompts import PROMPT_DIR, load_prompt
from pyassist.llm.tracing import UsageSnapshot, extract_usage, log_llm_response, model_id_of


class NextStepDraft(BaseModel):
    """`recommend_next` 的结构化输出。**没有 URL 字段**：链接由 `build_citations` 生成。"""

    topic: str
    reason: str
    exercise_ids: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


async def recommend_next(
    *,
    user_id: str,
    mistakes: Annotated[Sequence[MistakeRecord] | None, InjectedToolArg] = None,
    profile_block: Annotated[str, InjectedToolArg] = "",
    model: Annotated[BaseChatModel | None, InjectedToolArg] = None,
) -> tuple[NextStepDraft, UsageSnapshot]:
    """按候选错题生成推荐草稿。候选为空时由调用方短路，不该走到这里。"""
    if model is None:
        raise ConfigError("recommend_next 缺少模型注入")
    if not mistakes:
        raise LLMBadResponse("推荐缺少候选错题", details={"stage": "recommend", "user_id": user_id})

    model_id = model_id_of(model)
    structured = get_structured_output(model, NextStepDraft)
    messages = _progress_messages(mistakes, profile_block)

    started = time.perf_counter()
    result = await structured.ainvoke(messages)
    draft, usage = _unwrap_structured(result, model_id=model_id)
    log_llm_response(
        usage,
        model_id=model_id,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return _validate(draft), usage


def _progress_messages(mistakes: Sequence[MistakeRecord], profile_block: str) -> list[BaseMessage]:
    system = load_prompt(PROGRESS_PROMPT_NAME).format_messages(
        history=[], output_language=OUTPUT_LANGUAGE
    )[0]
    return [
        system,
        *_progress_context_template().format_messages(
            mistakes=list(mistakes), profile_block=profile_block
        ),
    ]


@cache
def _progress_context_template() -> ChatPromptTemplate:
    """`*_context.jinja2` 的独立加载器。**不走 `load_prompt()`**（同守护 #37）。"""
    path = PROMPT_DIR / f"{PROGRESS_CONTEXT_PROMPT_NAME}.jinja2"
    return ChatPromptTemplate.from_messages(
        [("human", path.read_text(encoding="utf-8"))], template_format="jinja2"
    )


def _validate(draft: NextStepDraft) -> NextStepDraft:
    topic = draft.topic.strip()
    reason = draft.reason.strip()
    if not topic or not reason:
        raise LLMBadResponse("推荐缺少 topic 或 reason", details={"stage": "recommend"})
    return NextStepDraft(
        topic=topic,
        reason=reason,
        exercise_ids=[item.strip() for item in draft.exercise_ids if item.strip()],
        next_steps=[item.strip() for item in draft.next_steps if item.strip()],
    )


def _unwrap_structured(result: object, *, model_id: str) -> tuple[NextStepDraft, UsageSnapshot]:
    if not isinstance(result, dict):
        raise LLMBadResponse("结构化输出返回了非 dict 对象", details={"stage": "recommend"})
    raw = result.get("raw")
    parsed = result.get("parsed")
    usage = extract_usage(raw, model_id=model_id) if isinstance(raw, AIMessage) else _zero_usage()
    if not isinstance(parsed, NextStepDraft):
        raise LLMBadResponse(
            "结构化输出解析失败",
            details={"stage": "recommend", "parsing_error": str(result.get("parsing_error"))},
        )
    return parsed, usage


def _zero_usage() -> UsageSnapshot:
    return UsageSnapshot(
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=0,
        cost_usd=Decimal(0),
        is_off_peak=True,
    )


__all__ = ["NextStepDraft", "recommend_next"]
