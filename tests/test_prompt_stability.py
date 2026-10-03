"""缓存前缀稳定性守护（SPEC 阶段二 §1）：模板渲染必须逐字节可复现。"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from pyassist.llm.prompts import PROMPT_DIR, load_prompt

# 易变 token：出现在模板里就会击穿前缀缓存，成本涨 50 倍。
VOLATILE_TOKENS = ("datetime.now", "time.time", "uuid", "random", "timestamp", "today")


def test_same_input_renders_byte_identical() -> None:
    """同一模板、同样输入渲染两次，输出必须逐字节一致。"""
    prompt = load_prompt("qa")
    history = [HumanMessage("asyncio.gather 怎么用")]
    first = prompt.format_messages(history=history, output_language="中文")
    second = prompt.format_messages(history=history, output_language="中文")
    assert [m.content for m in first] == [m.content for m in second]
    assert first[0].content.encode("utf-8") == second[0].content.encode("utf-8")


def test_templates_contain_no_volatile_tokens() -> None:
    """静态扫描：模板里不得出现时间戳 / 随机 id / 随机数 token。"""
    offenders: list[str] = []
    for path in sorted(PROMPT_DIR.glob("*.jinja2")):
        text = path.read_text(encoding="utf-8")
        hits = [token for token in VOLATILE_TOKENS if token in text]
        if hits:
            offenders.append(f"{path.name}: {hits}")
    assert not offenders, f"模板中出现易变 token：{offenders}"


def test_history_placeholder_renders_after_system_prefix() -> None:
    """易变输入只经 `history` 进入，且排在 system 之后 —— 前缀缓存只认前缀（§4.4）。"""
    prompt = load_prompt("qa")
    history = [HumanMessage("易变的用户输入")]
    messages = prompt.format_messages(history=history, output_language="中文")
    assert messages[0].type == "system"
    assert messages[-1].type == "human"
    assert messages[-1].content == "易变的用户输入"
