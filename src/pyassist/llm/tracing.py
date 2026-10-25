"""★ 唯一 Token/成本记账（§2）。

两条铁律来自 §3.1：
- 缓存命中与未命中的输入 token **必须分别记录、分别计价**（输入价差可达 50 倍）；
- 错峰时段半价，单价一律来自 `config.PRICE_TABLE`，**不用 float、不硬编码**。

本模块只做「一次调用的用量与成本」。累计与熔断是阶段三（Token 账本表）与
阶段八（单日上限），本阶段不建。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from pyassist.config import PEAK_WINDOWS_UTC, PRICE_TABLE
from pyassist.errors import ConfigError
from pyassist.logging import get_logger

logger = get_logger(__name__)

MICRO_TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)  # 单价表单位是 USD / 1M tokens
_PEAK_WEEKDAYS = frozenset({0, 1, 2, 3, 4})  # UTC 周一至周五


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """字段名与 §3.1 信封的 `meta.usage` 一一对应，阶段四直接序列化，不再改名。"""

    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: Decimal
    is_off_peak: bool


def is_off_peak(now_utc: datetime) -> bool:
    """UTC 周一至周五 01:00–04:00 与 06:00–10:00 为峰值，其余时段半价（§1）。

    半开区间 `[start, end)`：10:00 整已不在窗口内。
    """
    if now_utc.tzinfo is None:
        raise ValueError("is_off_peak 只接受带时区的时刻，避免本地时区把价格算错")
    moment = now_utc.astimezone(UTC)
    if moment.weekday() not in _PEAK_WEEKDAYS:
        return True
    current = moment.time()
    return not any(start <= current < end for start, end in PEAK_WINDOWS_UTC)


def price_usage(
    model_id: str,
    *,
    cached_tokens: int,
    uncached_input_tokens: int,
    output_tokens: int,
    is_off_peak: bool,
) -> Decimal:
    """按 `config.PRICE_TABLE` 分别计价后求和。单价单位 USD / 1M tokens。"""
    tier = PRICE_TABLE.get(model_id)
    if tier is None:
        # 服务级配置缺失 → ConfigError（阶段四 T7 收口，见 SPEC 待定项 11）。
        raise ConfigError(
            f"PRICE_TABLE 里没有 {model_id!r} 的单价。模型名只允许写在 config.py（§1 / §2）。"
        )

    if is_off_peak:
        cache_hit_price = tier.cache_hit_off_peak
        input_miss_price = tier.input_miss_off_peak
        output_price = tier.output_off_peak
    else:
        cache_hit_price = tier.cache_hit_peak
        input_miss_price = tier.input_miss_peak
        output_price = tier.output_peak

    total = (
        Decimal(cached_tokens) * cache_hit_price
        + Decimal(uncached_input_tokens) * input_miss_price
        + Decimal(output_tokens) * output_price
    )
    return total / MICRO_TOKENS_PER_PRICE_UNIT


def extract_usage(
    message: AIMessage,
    *,
    model_id: str,
    now_utc: datetime | None = None,
) -> UsageSnapshot:
    """从 `AIMessage.usage_metadata` 取用量并计价。

    **只认 `usage_metadata`。** 缓存命中数的归一化在 `llm/deepseek_chat.py` 完成，
    这里不去读 `response_metadata["token_usage"]` 兜底 —— 两条会互相矛盾的取数
    路径比一条不完整的路径更危险（见 SPEC 待定项 4）。
    """
    moment = now_utc or datetime.now(UTC)
    off_peak = is_off_peak(moment)

    usage = message.usage_metadata
    if usage is None:
        return UsageSnapshot(
            prompt_tokens=0,
            completion_tokens=0,
            cached_tokens=0,
            cost_usd=Decimal(0),
            is_off_peak=off_peak,
        )

    prompt_tokens = usage["input_tokens"]
    completion_tokens = usage["output_tokens"]
    cached_tokens = int((usage.get("input_token_details") or {}).get("cache_read") or 0)
    # 防御：上游若给出大于输入总量的命中数，未命中按 0 处理，不产生负数计费。
    cached_tokens = min(cached_tokens, prompt_tokens)

    cost_usd = price_usage(
        model_id,
        cached_tokens=cached_tokens,
        uncached_input_tokens=prompt_tokens - cached_tokens,
        output_tokens=completion_tokens,
        is_off_peak=off_peak,
    )
    return UsageSnapshot(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        cost_usd=cost_usd,
        is_off_peak=off_peak,
    )


def add_usage(left: UsageSnapshot, right: UsageSnapshot) -> UsageSnapshot:
    """多轮工具调用的用量合计（状态里每轮覆盖，对外只报一个数）。

    阶段六曾把这段写在 `services/qa.py`；阶段七的诊断图要用同一条规则，
    所以上收到本模块 —— 否则两张图会各自累加，迟早出现"一个算缓存命中一个不算"。
    `is_off_peak` 取右值：同一请求内跨过峰值边界是极小概率，取最新时刻即可。
    """
    return UsageSnapshot(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        cached_tokens=left.cached_tokens + right.cached_tokens,
        cost_usd=left.cost_usd + right.cost_usd,
        is_off_peak=right.is_off_peak,
    )


def model_id_of(model: BaseChatModel) -> str:
    """取模型的 `model_name` 用来计价。缺失时明确报错，不猜一个默认值。

    `PRICE_TABLE` 只认 `config.py` 里的两个模型 ID（§1）—— 猜一个会在成本报表里
    静默记到别的模型头上。
    """
    name = getattr(model, "model_name", None)
    if isinstance(name, str) and name:
        return name
    raise ConfigError("模型实例没有 model_name，无法按 PRICE_TABLE 计价")


def log_llm_response(snapshot: UsageSnapshot, *, model_id: str, latency_ms: int) -> None:
    """按 §3.5 打 `llm.response`（带 usage）。

    字段名固定：`request_id` / `session_id` / `user_id` 由 `logging.bind_request_context()`
    绑定，`latency_ms` / usage 各字段在这里显式带上。**不记 prompt 全文、不记 API Key。**
    `llm.request` 由调用方在自己的节点里打（阶段六起），本模块只管记账侧。
    """
    logger.info(
        "llm.response",
        model_id=model_id,
        prompt_tokens=snapshot.prompt_tokens,
        completion_tokens=snapshot.completion_tokens,
        cached_tokens=snapshot.cached_tokens,
        cost_usd=str(snapshot.cost_usd),  # 日志里用字符串，避免 float 精度假象
        is_off_peak=snapshot.is_off_peak,
        latency_ms=latency_ms,
    )
