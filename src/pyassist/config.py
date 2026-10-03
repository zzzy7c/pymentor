"""唯一配置入口（§2）。其他模块一律从此处取配置，不得另建 settings 模块。

分工（SPEC §4 T4）：
- 随代码评审变更的（模型 ID、价格、沙箱上限）是模块常量；
- 随部署环境变更的（API key、DB URL、日志级别）进 `Settings`。
二者都在本文件，仍然只有一个配置入口。
"""

from __future__ import annotations

from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DATA_DIR: Final = REPO_ROOT / "data"

# ---------- 模型 ID：全项目只允许出现在这里（§1 / §2）----------
MODEL_FLASH: Final = "deepseek-v4-flash"
MODEL_PRO: Final = "deepseek-v4-pro"
EXPECTED_MODEL_IDS: Final[frozenset[str]] = frozenset({MODEL_FLASH, MODEL_PRO})

# ---------- 端点 ----------
DEEPSEEK_API_BASE_URL: Final = "https://api.deepseek.com"


# ---------- 单价表：USD / 1M tokens（§1）。仅用于量级判断与成本告警 ----------
class PriceTier(BaseModel):
    """单模型六个单价。显式存峰值/非峰值两列，避免在计价处做浮点乘法。"""

    cache_hit_off_peak: Decimal
    cache_hit_peak: Decimal
    input_miss_off_peak: Decimal
    input_miss_peak: Decimal
    output_off_peak: Decimal
    output_peak: Decimal


# 数值来自 AGENTS.md §1 价格表；以官网定价页为准，改动只在这里。
PRICE_TABLE: Final[dict[str, PriceTier]] = {
    MODEL_FLASH: PriceTier(
        cache_hit_off_peak=Decimal("0.003"),
        cache_hit_peak=Decimal("0.006"),
        input_miss_off_peak=Decimal("0.15"),
        input_miss_peak=Decimal("0.30"),
        output_off_peak=Decimal("0.60"),
        output_peak=Decimal("1.20"),
    ),
    MODEL_PRO: PriceTier(
        cache_hit_off_peak=Decimal("0.022"),
        cache_hit_peak=Decimal("0.044"),
        input_miss_off_peak=Decimal("0.66"),
        input_miss_peak=Decimal("1.32"),
        output_off_peak=Decimal("1.98"),
        output_peak=Decimal("3.96"),
    ),
}

# ---------- 错峰窗口：UTC 周一至周五（§1）----------
# 纯数据。判断"现在是否错峰"的函数属于记账逻辑，放阶段二 llm/tracing.py。
PEAK_WINDOWS_UTC: Final[tuple[tuple[dt_time, dt_time], ...]] = (
    (dt_time(1, 0), dt_time(4, 0)),
    (dt_time(6, 0), dt_time(10, 0)),
)


# ---------- 沙箱上限（§5.3）。数值待实测校准，见 SPEC 待定项 4 ----------
class SandboxLimits(BaseModel):
    timeout_seconds: int = 10
    memory_mb: int = 512
    cpus: float = 1.0
    output_head_lines: int = 100  # 超长输出截断：保留头尾（§2 检查点膨胀）
    output_tail_lines: int = 100
    is_network_enabled: bool = False
    is_rootfs_read_only: bool = True
    is_non_root: bool = True


SANDBOX_LIMITS: Final = SandboxLimits()


# ---------- 环境相关配置 ----------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 缺 key 不得让 `import pyassist` 失败：导入期不校验，阶段二 llm/client.py 取模型时才报错。
    deepseek_api_key: SecretStr | None = None
    deepseek_api_base_url: str = DEEPSEEK_API_BASE_URL
    # 绝对路径，避免阶段三的"检查点目标 == data/app.db"断言随 CWD 漂移。
    database_url: str = f"sqlite+pysqlite:///{DATA_DIR / 'app.db'}"
    log_level: str = "INFO"
    log_json: bool = True
    embedding_model_id: str | None = None  # 阶段五拍板（ROADMAP 待定项 1）
    daily_token_limit: int | None = None  # ROADMAP 待定项 5


settings = Settings()
