"""`Settings` 对空环境变量的处理。

`.env.example`（"留空 = 用默认"）与 compose 的 `${VAR:-}` 都会把变量设成空串。
空串必须等价于"没配"，否则 `import pyassist.config` 直接 ValidationError，
后端连启动都做不到。
"""

from __future__ import annotations

import pytest

from pyassist.config import DEFAULT_EMBEDDING_MODEL_ID, Settings


def test_empty_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_BASE_URL",
        "DATABASE_URL",
        "LOG_LEVEL",
        "LOG_JSON",
        "EMBEDDING_MODEL_ID",
        "DAILY_TOKEN_LIMIT",
    ):
        monkeypatch.setenv(name, "")

    settings = Settings(_env_file=None)

    assert settings.deepseek_api_key is None
    assert settings.deepseek_api_base_url == "https://api.deepseek.com"
    assert settings.database_url.startswith("sqlite+aiosqlite:///")
    assert settings.log_level == "INFO"
    assert settings.log_json is True
    assert settings.embedding_model_id == DEFAULT_EMBEDDING_MODEL_ID
    assert settings.daily_token_limit is None


def test_zero_daily_limit_means_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """`0` 与空串语义不同：前者明确关闭熔断，后者回落到默认额度。"""
    monkeypatch.setenv("DAILY_TOKEN_LIMIT", "0")

    assert Settings(_env_file=None).daily_token_limit == 0
