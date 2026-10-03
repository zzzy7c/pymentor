"""Prompt 模板加载器（§2：prompt 文本的唯一家）。

阶段一起所有 `__init__.py` 保持为空。这里有意破例：`prompts` 不是占位包，
它的公开面就是 `load_prompt()`；另开 `loader.py` 只多一个文件，不多一层抽象。
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

PROMPT_DIR: Final = Path(__file__).resolve().parent

_HISTORY_PLACEHOLDER: Final = "history"


@cache
def load_prompt(name: str) -> ChatPromptTemplate:
    """按名字加载 `llm/prompts/<name>.jinja2`，渲染顺序固定 `system` → `history`。

    用 `jinja2` 而不是 f-string：模板里有 Python 代码示例，`{}` 会被 f-string
    当成变量（§1 也要求用 jinja2）。易变的用户输入永远走 `history`（放最后），
    系统提示逐字节稳定 —— 这是 DeepSeek 前缀缓存命中的前提（§1）。
    """
    path = PROMPT_DIR / f"{name}.jinja2"
    if not path.is_file():
        raise FileNotFoundError(f"没有这个 prompt 模板：{path}")
    return ChatPromptTemplate.from_messages(
        [("system", path.read_text(encoding="utf-8")), MessagesPlaceholder(_HISTORY_PLACEHOLDER)],
        template_format="jinja2",
    )
