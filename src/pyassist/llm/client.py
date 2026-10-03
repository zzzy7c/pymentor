"""★ 唯一模型实例工厂（§2）。

全项目取模型只走 `get_chat_model()`；任何其他模块自行 `ChatDeepSeek(...)` /
`ChatOpenAI(...)` / `ChatDeepSeekPatched(...)` 都会被
`tests/test_contract_guardrails.py` 拦下。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import openai
from pydantic import SecretStr

from pyassist.config import (
    EXPECTED_MODEL_IDS,
    MODEL_FLASH,
    MODEL_PRO,
    settings,
)
from pyassist.llm.deepseek_chat import ChatDeepSeekPatched
from pyassist.logging import get_logger

logger = get_logger(__name__)

ModelTier = Literal["flash", "pro"]

_TIER_TO_MODEL_ID: dict[ModelTier, str] = {"flash": MODEL_FLASH, "pro": MODEL_PRO}
_DEFAULT_REQUEST_TIMEOUT = 60.0
_DEFAULT_MAX_RETRIES = 2  # 重试交给 SDK，不手写 for i in range(3)（§1）


@dataclass(frozen=True, slots=True)
class ModelCheckResult:
    """启动时模型名校验的结果。阶段四把它接进 `/health`。"""

    is_ok: bool
    available: frozenset[str]
    missing: frozenset[str]
    checked_at: datetime
    error: str | None = None


_last_model_check: ModelCheckResult | None = None


def get_last_model_check() -> ModelCheckResult | None:
    """返回最近一次 `check_models()` 的结果（没跑过则为 `None`）。"""
    return _last_model_check


def get_chat_model(
    *,
    tier: ModelTier = "flash",
    thinking: bool = True,
    api_key: SecretStr | None = None,
    **kwargs: Any,
) -> ChatDeepSeekPatched:
    """取唯一的 DeepSeek 模型实例。

    `api_key` 是给测试留的缝：不传则取 `settings.deepseek_api_key`，缺失即报错。
    这样"构造模型"这个动作在物理上只存在于本文件，测试不必自己 new 一个。

    思考模式由 `extra_body` 控制，**不是靠换模型**（§1）。注意思考模式下
    `temperature` / `top_p` 等参数全部不生效，设了不报错但会被忽略。
    """
    resolved_key = api_key or settings.deepseek_api_key
    if resolved_key is None:
        # 阶段四会把它换成 errors.ConfigError（见 SPEC 待定项 2）。此处不留 TODO 占位。
        raise RuntimeError("DEEPSEEK_API_KEY 未设置，无法构造模型实例。请在 .env 配置后重试。")

    extra_body: dict[str, Any] = dict(kwargs.pop("extra_body", None) or {})
    extra_body.setdefault("thinking", {"type": "enabled" if thinking else "disabled"})

    return ChatDeepSeekPatched(
        model=_TIER_TO_MODEL_ID[tier],
        api_key=resolved_key,
        # 必须显式传：库的默认值读的是环境变量 DEEPSEEK_API_BASE，与本项目的
        # DEEPSEEK_API_BASE_URL 不同名，不传就会打到库自己的默认端点。
        base_url=settings.deepseek_api_base_url,
        timeout=_DEFAULT_REQUEST_TIMEOUT,
        max_retries=_DEFAULT_MAX_RETRIES,
        extra_body=extra_body,
        **kwargs,
    )


def check_models(*, api_key: SecretStr | None = None) -> ModelCheckResult:
    """调一次 `models.list()`，与 `config.EXPECTED_MODEL_IDS` 比对（§1）。

    **只告警不阻断。** 模型名本身正在漂移（§1 明说定价页写的是
    `deepseek-flash`，`deepseek-v4-flash` 是仍在被路由的旧名），把它做成硬失败
    等于让一次上游改名打挂整个应用。结果存进模块级变量，供阶段四的 `/health` 读。
    """
    global _last_model_check

    resolved_key = api_key or settings.deepseek_api_key
    checked_at = datetime.now(UTC)

    if resolved_key is None:
        result = ModelCheckResult(
            is_ok=False,
            available=frozenset(),
            missing=EXPECTED_MODEL_IDS,
            checked_at=checked_at,
            error="DEEPSEEK_API_KEY 未设置",
        )
    else:
        client = openai.OpenAI(
            api_key=resolved_key.get_secret_value(),
            base_url=settings.deepseek_api_base_url,
            timeout=10.0,
            max_retries=1,
        )
        try:
            available = frozenset(model.id for model in client.models.list().data)
        except openai.OpenAIError as exc:
            # 捕获链条的根（§3.2）：鉴权、限流、连接、端点不存在都归这里，
            # 不做硬失败。日志只记类型与消息，不记 key。
            result = ModelCheckResult(
                is_ok=False,
                available=frozenset(),
                missing=EXPECTED_MODEL_IDS,
                checked_at=checked_at,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            missing = frozenset(EXPECTED_MODEL_IDS - available)
            result = ModelCheckResult(
                is_ok=not missing,
                available=available,
                missing=missing,
                checked_at=checked_at,
            )

    _last_model_check = result
    if result.is_ok:
        logger.info("llm.models_check.ok", available=sorted(result.available))
    else:
        logger.error(
            "llm.models_check.mismatch",
            expected=sorted(EXPECTED_MODEL_IDS),
            missing=sorted(result.missing),
            error=result.error,
        )
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyassist.llm.client",
        description="DeepSeek 模型名校验（§1）。人工步骤，不进 CI。",
    )
    parser.add_argument(
        "--check-models",
        action="store_true",
        help="调用 models.list()，比对 config.EXPECTED_MODEL_IDS",
    )
    args = parser.parse_args(argv)
    if not args.check_models:
        parser.print_help()
        return 2

    result = check_models()
    if result.is_ok:
        print(f"模型名校验通过：{sorted(result.available)}")
        return 0
    print(
        f"模型名校验未通过（仅告警，不阻断）：missing={sorted(result.missing)} error={result.error}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(_main())
