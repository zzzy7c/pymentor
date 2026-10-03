"""阶段一冒烟测试：证明包能装、能 import、config 能在无 key 环境构造。

不作"断言常量等于自己"的空转测试 —— 两条断言各验证一个真实的阶段一风险（SPEC §5.5）。
"""

from __future__ import annotations

import importlib.metadata

from pyassist.config import settings


def test_package_is_installed() -> None:
    """`importlib.metadata.version` 有返回 → 打包配置正确且已 editable 安装。

    src 布局最容易在 `[tool.hatch.build.targets.wheel] packages` 这里翻车，
    该断言若失败即说明包没被正确安装。
    """
    assert importlib.metadata.version("pyassist")


def test_settings_constructs_without_api_key() -> None:
    """`config.py` 能在没有 API key 的机器上成功构造（§4 T4 的关键设计）。"""
    assert settings.database_url.startswith("sqlite")
