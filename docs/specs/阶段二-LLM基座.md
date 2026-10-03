# SPEC.md — 阶段二 · LLM 基座与 `reasoning_content` 修复 · 实施规格

本文件是 **ROADMAP.md「阶段二」的施工细则**，回答"这一阶段怎么建、建成什么样才算完"。

- `AGENTS.md` 是**契约**（不可协商）。本文件不重新定义契约，只把它翻译成可执行的任务。
- `ROADMAP.md` 是**施工顺序**。本文件的每个任务都能追到 ROADMAP「阶段二」的某条产出。
- 三者冲突时以 `AGENTS.md` 为准；本文件与 `AGENTS.md` 冲突，说明本文件写错了。
- 阶段一的规格已归档到 `docs/specs/阶段一-工程底座.md`（见「待定项 1」）。

**阶段二不交付版本，交付"任何图都能安全地取模型"的地基。** 验收标准是补丁经得起"连续两轮工具调用"，不是单轮问答能跑通 —— 单轮问答在本阶段**测不出任何问题**。

---

## 0. 范围边界

### 做

- `llm/deepseek_chat.py`：`ChatDeepSeekPatched`，两块补丁（`reasoning_content` 出站回写 + 缓存命中用量归一化）。
- `llm/client.py`：唯一模型实例工厂 + 启动时模型名校验 + `--check-models` CLI。
- `llm/tracing.py`：唯一 Token/成本记账（命中与未命中分别计价、错峰半价）。
- `llm/prompts/qa.jinja2` + 加载器。
- 四个测试文件 + 一条守护断言扩展。

### 不做（明确排除，避免"顺手加一下"）

| 不建 | 理由 |
|---|---|
| `src/pyassist/errors.py` | 阶段四的单点所有权（§3.2 异常树）。本阶段缺 key 时抛 `RuntimeError`，阶段四统一换成 `ConfigError`，**不留 TODO 占位**（见「待定项 2」） |
| `agent/state.py` / `agent/registry.py` / 任何图 | 阶段六。阶段二只保证"图一旦开建，取模型这一步已经是对的" |
| `api/schemas.py` / `api/errors.py` / `/health` 路由 | 阶段四。本阶段只把模型校验**结果**存在 `client.py` 的模块级变量里，供阶段四读 |
| `db/` 任何文件、`token_ledger` 表 | 阶段三。Token 账本的**持久化**是阶段三，本阶段只做**计价**（§3.1 的 `cost_usd`） |
| `rag/`、`sandbox/` | 阶段五、阶段七 |
| `diagnose.jinja2` / `exercise.jinja2` / `grade.jinja2` | 阶段七、八。本阶段只建 `qa.jinja2` 与**加载器**，把"怎么写模板"这件事定型 |
| 单日 Token 熔断逻辑 | 阶段八（§5.5）。本阶段只出 `UsageSnapshot`，不做累计与熔断 |
| 实例缓存 / 连接池调优 | 本阶段没有热路径，每次 `get_chat_model()` 造一个新的 pydantic 对象开销可忽略。等阶段六出现"每请求多次取模型"时再加缓存，且要说明理由 |

---

## 1. 前置核查结果（本机实测，2026-10-03）

### 1.1 已装版本

| 包 | 版本 | 影响 |
|---|---|---|
| `langchain-deepseek` | **1.1.0** | 补丁的**靶子**。缺陷 A、B 都在这个版本上实测确认 |
| `langchain-openai` | **1.1.9** | 缺陷 A、B 的**上游根源**都在这里 |
| `langchain-core` | 1.6.6 | `UsageMetadata` / `InputTokenDetails` 定义在 `langchain_core/messages/ai.py`，并从 `langchain_core.messages` 再导出 |
| `langgraph` | 1.1.10 | 阶段六才用，本阶段不依赖 |
| `openai` | 1.109.1 | 仅 `client.py` 的 `models.list()` 用（§1 / §2） |
| `respx` | 0.23.1 | 已在 `[dependency-groups].dev` 里，本阶段三个测试都靠它保持离线 |
| `jinja2` | 已装 | `ChatPromptTemplate` 的 `template_format="jinja2"` 需要它 |

### 1.2 缺陷 A：`reasoning_content` 只入不出（必须自修）

直接读 `.venv` 里的源码，不凭记忆：

| 事实 | 位置 |
|---|---|
| 入站**有**：非流式把响应里的 `reasoning_content` 写进 `AIMessage.additional_kwargs` | `langchain_deepseek/chat_models.py:315-327` |
| 入站**有**：流式同一字段写进 `AIMessageChunk.additional_kwargs` | `langchain_deepseek/chat_models.py:342-359` |
| 出站**完全没有**：`_get_request_payload` 只修 `tool` 的 list content 与 assistant 的 list content，**不碰** `reasoning_content` | `langchain_deepseek/chat_models.py:264-297` |
| 出站**完全没有**：`_convert_message_to_dict` 的 `AIMessage` 分支只处理 `tool_calls` / `function_call` / `audio`，**不读** `additional_kwargs["reasoning_content"]` | `langchain_openai/chat_models/base.py:289-366` |

**结论：社区 PR #37461（出站镜像）与 #35093（`tool_calls` 场景回填 `""`）在这个版本上都没有合入。** 补丁是必需的，不是空壳。将来升级依赖后，本文件的测试会告诉你上游是否已修 —— 修了则 `deepseek_chat.py` 可退化为薄壳，**但测试必须留着**（§1.4：它是防回归的唯一保险）。

两个由此推出的实现约束：

1. `_convert_message_to_dict` 是 `langchain_openai` 的**模块级函数**，不是方法，**子类无法 override 它**。只能在 `_get_request_payload` 里对 payload **后处理**。
2. `_get_request_payload` 里 `payload["messages"]` 与 `self._convert_input(input_).to_messages()` **一一对应、顺序一致**（同一个列表推导，见 `langchain_openai/chat_models/base.py:1472-1477`），所以可以用 `zip(..., strict=True)` 把「原始消息」与「出站 dict」对齐。对齐失败时（长度不符）**宁可不动手**。

### 1.3 缺陷 B：缓存命中的输入 token 会被算成全价（ROADMAP / AGENTS 均未记载）

| 事实 | 位置 |
|---|---|
| `langchain_openai` 取缓存命中数只认 OpenAI 的**嵌套**形状 `prompt_tokens_details.cached_tokens` | `langchain_openai/chat_models/base.py:3723-3739` |
| DeepSeek 返回的是**扁平**的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，没有 `prompt_tokens_details` 这个对象 | DeepSeek 官方 Context Caching 文档 |
| 未加工的原始 usage dict 仍然拿得到：`llm_output["token_usage"]` | `langchain_openai/chat_models/base.py:1508, 1527-1528` |
| `InputTokenDetails` 的正确键名是 `cache_read` | `langchain_core/messages/ai.py:38-72` |

**后果**：`usage_metadata.input_token_details` 里**根本没有 `cache_read`**，于是 §3.1 要求的 `usage.cached_tokens` 恒为空，每一次缓存命中都按全价 miss 计费 —— 而 §1 说命中与未命中的**输入价差可达 50 倍**。这不是"精度问题"，是成本报表从上线第一天起就是错的。

**归入本阶段**：归一化落在 `deepseek_chat.py`（与缺陷 A 同一个类、同一个"修上游集成"的理由）；`tracing.py` 只消费 `usage_metadata`，**不读 `response_metadata["token_usage"]` 兜底** —— 两条取数路径会互相矛盾。理由与备选见「待定项 4」。

### 1.4 阶段一留下的闸门（本阶段必须对齐）

| 闸门 | 位置 | 对阶段二的约束 |
|---|---|---|
| `ChatDeepSeek(` / `ChatOpenAI(` 只许在 `llm/client.py`（**精确路径**，断言是集合相等） | `tests/test_contract_guardrails.py:163-176` | 本阶段把 `ChatDeepSeekPatched` 也加进去（T7）。注意：**类定义**不是 `ast.Call`，所以 `deepseek_chat.py` 里的 `class ChatDeepSeekPatched(ChatDeepSeek)` 不会误伤 |
| `BaseSettings` 子类只在 `config.py` | 同上 `:191-204` | 本阶段不得新增 settings |
| `structlog.configure` 只在 `logging.py` | 同上 `:207-224` | 本阶段只用 `logging.get_logger()` |
| `tests/` 在扫描面内 | 同上 `SCAN_DIRS` | **测试文件的注释与字符串里也不得出现已停用模型名** |

`config.py` 已备好本阶段所需的全部常量，不需要改动：`MODEL_FLASH` / `MODEL_PRO` / `EXPECTED_MODEL_IDS`、`PRICE_TABLE`（六字段 `PriceTier`，峰值/非峰值两列）、`PEAK_WINDOWS_UTC`、`Settings.deepseek_api_key`（注释已预告"阶段二 `llm/client.py` 取模型时才报错"）。

### 1.5 两段核心草案已实测

§4.1 的 payload 后处理与 §4.4 的加载器，已在**当前 `.venv` 上跑通过一次**（一次性脚本，未入库）：

- `ChatPromptTemplate.from_messages([("system", 模板), MessagesPlaceholder("history")], template_format="jinja2")` 可以共存并渲染；同输入两次渲染**逐字节一致**。
- `_convert_input(input_).to_messages()` 与 `_get_request_payload` 的 `payload["messages"]` **长度与顺序一致**（用一个 `assert` 验过），`zip(..., strict=True)` 对齐成立。
- 镜像函数写出的是出站 dict 的**顶层键**；带 `tool_calls` 而字段缺失时回填 `""`，**键存在**。

也就是说本文件的代码草案不是"看起来对"，而是已经跑过一遍的。实施时若这三点之一不成立，说明 `.venv` 里的包被换过版本 —— 先回到 §1.1 核对版本。

---

## 2. 目标文件清单

`✓` = 阶段二新建；`△` = 阶段二修改；其余为既有文件。

```
python助手/
├── AGENTS.md / ROADMAP.md               （既有，只读）
├── SPEC.md                          △    本文件（阶段一规格已归档）
├── docs/specs/阶段一-工程底座.md   ✓    阶段一规格归档
├── src/pyassist/
│   ├── config.py                        （既有，本阶段不改）
│   ├── logging.py                       （既有，本阶段只调用）
│   └── llm/
│       ├── __init__.py                  （既有，保持空）
│       ├── deepseek_chat.py         ✓    ★ ChatDeepSeekPatched（两块补丁）
│       ├── client.py                ✓    ★ 唯一模型实例工厂 + 模型名校验 + CLI
│       ├── tracing.py               ✓    ★ 唯一 Token/成本记账
│       └── prompts/
│           ├── __init__.py          ✓    加载器（有意破例，见 §4 T6）
│           └── qa.jinja2            ✓    v1 问答系统提示
└── tests/
    ├── test_smoke.py                    （既有）
    ├── test_contract_guardrails.py  △    追加 ChatDeepSeekPatched 唯一构造点
    ├── test_deepseek_reasoning.py   ✓    ★ 三条（含强制的连续两轮回归）
    ├── test_prompt_stability.py     ✓    逐字节稳定 + 易变 token 扫描
    ├── test_tracing.py              ✓    分别计价 / 错峰边界
    └── test_usage_mapping.py        ✓    扁平 cache 字段 → cache_read
```

`llm/__init__.py` **保持为空** —— 全项目用绝对导入（`from pyassist.llm.client import ...`），不需要 re-export。`llm/prompts/__init__.py` 是唯一破例，理由见 T6。

---

## 3. 任务分解

| # | 任务 | 产出 | 预估 |
|---|---|---|---|
| T0 | 归档阶段一规格 | `docs/specs/阶段一-工程底座.md` | 10 min |
| T1 | 补丁类 | `llm/deepseek_chat.py` | 60 min |
| T2 | 三条离线回归测试 | `tests/test_deepseek_reasoning.py` | 60 min |
| T3 | 唯一工厂 + 模型名校验 + CLI | `llm/client.py` | 60 min |
| T4 | 记账模块 | `llm/tracing.py` | 50 min |
| T5 | 记账与用量映射测试 | `tests/test_tracing.py`、`tests/test_usage_mapping.py` | 45 min |
| T6 | Prompt 模板 + 加载器 + 稳定性测试 | `llm/prompts/`、`tests/test_prompt_stability.py` | 40 min |
| T7 | 扩展契约守护 | `tests/test_contract_guardrails.py` | 15 min |
| T8 | 五条验收命令 + 真机 `--check-models` + 提交 | 绿的阶段二 | 30 min |

**合计约 4–5 小时。** 无硬阻塞项。

### T0 · 归档阶段一规格

```bash
mkdir -p docs/specs
cp SPEC.md "docs/specs/阶段一-工程底座.md"     # 本文件随即被重写
```

阶段一规格里有 4 处被代码反向引用（`config.py` 的"§4 T4"、"待定项 4"，`pyproject.toml` 的"§5.6"、"§5.1 的疏漏"）。归档保留它们，归档件就是那些引用的解析目标。

### T1 · `llm/deepseek_chat.py`

见 §4.1。两块补丁写在一个类里，**两个纯函数放在模块级**（便于测试直接调，也便于 `mypy --strict` 收窄类型）。

顺序要求：先写缺陷 A 的镜像函数与 `_get_request_payload`，跑通 T2 的前两条测试，再补缺陷 B 的部分。**不要两处同时调** —— 这正是 §4 警告的"最容易踩且最难排查"的处境。

### T2 · `tests/test_deepseek_reasoning.py`

见 §4.5。三条测试**全离线**：前两条直接构造消息调 `_get_request_payload`（不发请求），第三条用 `respx` mock 两轮。

**模型实例必须经 `llm/client.py` 取**，测试里不得自己 `ChatDeepSeekPatched(...)` —— 那会变成事实上的第二个所有者（阶段一 SPEC §2 的同一理由）。`get_chat_model(api_key=...)` 就是为此留的缝。

### T3 · `llm/client.py`

见 §4.2。三件事：唯一工厂、启动校验、CLI。

`test_no_direct_llm_construction_outside_client` 断言的是**精确路径** `src/pyassist/llm/client.py`，所以文件名与位置不能动。

### T4 · `llm/tracing.py`

见 §4.3。只做计价与快照，**不做累计**（那是阶段三的 `token_ledger` 表与阶段八的熔断）。

### T5 · `tests/test_tracing.py` + `tests/test_usage_mapping.py`

见 §4.7。前者覆盖计价与错峰边界，后者覆盖缺陷 B 的修好与否 —— 它是升级 `langchain-deepseek` 后**第一个**该跑的测试。

### T6 · `llm/prompts/`

见 §4.4。**`llm/prompts/__init__.py` 里写加载器是对阶段一约定的有意破例**：阶段一说"所有 `__init__.py` 保持为空"，那条针对的是**占位包**（空文件不发散）；`prompts` 不是占位包，它的公开面就是 `load_prompt()`。另开 `loader.py` 只会多一个文件，不多一层抽象。

### T7 · 扩展 `tests/test_contract_guardrails.py`

把 `"ChatDeepSeekPatched"` 加进 `FORBIDDEN_LLM_CONSTRUCTORS`（`:30`），并同步更新该测试的 docstring 与断言信息。**已经验证过不会误伤**：`deepseek_chat.py` 里的类定义是 `ast.ClassDef`，扫描只匹配 `ast.Call`。

### T8 · 验收

跑 §5 的全部命令。真机那一条（`--check-models`）**不进 CI**，是人工步骤。

---

## 4. 关键文件规格

### 4.1 `src/pyassist/llm/deepseek_chat.py`

```python
"""★ ChatDeepSeekPatched：修 `langchain-deepseek` 1.1.0 的两处缺陷（§1 / §2）。

缺陷 A：`reasoning_content` 只入不出。
    入站方向 ChatDeepSeek 会把响应里的 `reasoning_content` 存进
    `AIMessage.additional_kwargs`；出站方向（`_get_request_payload` →
    langchain_openai 的模块级 `_convert_message_to_dict`）完全不读这个字段，
    于是被静默丢弃。思考模式下，一旦出现「模型 → tool_calls → ToolMessage →
    再调模型」，第二轮请求缺 `reasoning_content`，DeepSeek 直接返回 400。

缺陷 B：缓存命中的输入 token 会算成全价。
    langchain_openai 的 `_create_usage_metadata` 只认 OpenAI 的嵌套形状
    `prompt_tokens_details.cached_tokens`；DeepSeek 返回的是扁平的
    `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。不归一化，
    §3.1 的 `usage.cached_tokens` 恒为空，成本报表严重偏高（价差可达 50 倍）。

上游状态（2026-10-03 实测 1.1.0）：社区 PR #37461 / #35093 **均未合入**，
两个补丁都必须自己写。升级依赖后先跑
`tests/test_deepseek_reasoning.py` 与 `tests/test_usage_mapping.py`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    InputTokenDetails,
    UsageMetadata,
)
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_deepseek import ChatDeepSeek

REASONING_CONTENT_KEY = "reasoning_content"
CACHE_HIT_KEY = "prompt_cache_hit_tokens"
EMPTY_REASONING = ""  # 带 tool_calls 却缺该字段时回填这个值，不能省略键


def mirror_reasoning_content(messages: Sequence[BaseMessage], payload: dict[str, Any]) -> None:
    """把入站存下的 `reasoning_content` 写回出站 assistant 消息（§1）。

    带 `tool_calls` 而字段缺失时回填空串 —— **键必须存在，不能被省略**。
    `payload["messages"]` 与传入的 `messages` 一一对应；长度不符时宁可不动手。
    """
    outbound = payload.get("messages")
    if not isinstance(outbound, list) or len(outbound) != len(messages):
        return
    for source, target in zip(messages, outbound, strict=True):
        if not isinstance(source, AIMessage) or target.get("role") != "assistant":
            continue
        reasoning = source.additional_kwargs.get(REASONING_CONTENT_KEY)
        if reasoning is None:
            if "tool_calls" in target:
                target[REASONING_CONTENT_KEY] = EMPTY_REASONING
        else:
            target[REASONING_CONTENT_KEY] = reasoning


def normalize_cache_usage(
    message: AIMessage | AIMessageChunk,
    raw_usage: Mapping[str, Any] | None,
) -> None:
    """把 DeepSeek 的扁平 `prompt_cache_hit_tokens` 归一到 `input_token_details["cache_read"]`。

    只认上游原始 usage —— `usage_metadata` 里这个信息已经被 langchain_openai 丢掉了。
    """
    if not raw_usage:
        return
    cache_hit = raw_usage.get(CACHE_HIT_KEY)
    if cache_hit is None:
        return
    usage = message.usage_metadata
    if usage is None:
        return
    details: InputTokenDetails = dict(usage.get("input_token_details") or {})
    details["cache_read"] = int(cache_hit)
    message.usage_metadata = UsageMetadata(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        total_tokens=usage["total_tokens"],
        input_token_details=details,
        output_token_details=usage.get("output_token_details") or {},
    )


class ChatDeepSeekPatched(ChatDeepSeek):
    """全项目唯一允许使用的模型类（§2）。**构造点只有 `llm/client.py`。**"""

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        mirror_reasoning_content(self._convert_input(input_).to_messages(), payload)
        return payload

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict[str, Any] | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        raw_usage = (result.llm_output or {}).get("token_usage")
        for generation in result.generations:
            normalize_cache_usage(generation.message, raw_usage)
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk | None:
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is not None:
            normalize_cache_usage(generation.message, chunk.get("usage"))
        return generation
```

三条要点：

1. **`reasoning_content` 必须写成出站 dict 的顶层键**，不能塞进 `content` 的内容块里 —— 基类的 `_format_message_content` 会**静默剥掉** `type` 为 `thinking` / `reasoning_content` 的块。测试断言的就是顶层键。
2. `_create_chat_result` 与 `_convert_chunk_to_generation_chunk` **都要 override**：前者管非流式，后者管流式。只修一个，阶段六的 SSE 路径会静默漏掉缓存命中。
3. `response: Any` 是有意的 —— 精确类型是 `dict | openai.BaseModel`，引入 `openai` 的名字到本文件没有好处，而**放宽被覆写方法的形参类型**在 Liskov 意义上合法，`mypy --strict` 接受。

### 4.2 `src/pyassist/llm/client.py`

```python
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
        # 阶段四会把它换成 errors.ConfigError（见「待定项 2」）。此处不留 TODO 占位。
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未设置，无法构造模型实例。请在 .env 配置后重试。"
        )

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
    print(f"模型名校验未通过（仅告警，不阻断）：missing={sorted(result.missing)} error={result.error}")
    return 1


if __name__ == "__main__":
    sys.exit(_main())
```

三条要点：

1. **`base_url` 必须显式传**（见上文注释）。这是"库的默认值恰好不是你想要的默认值"这类静默故障的典型，值得在文件里留一行理由。
2. `check_models()` 里 `except openai.OpenAIError` 是 §3.2 的"捕获链条再往上兜一层"，**不是** `except Exception` —— §4 禁止的是后者。
3. **不做实例缓存**（见 §0「不做」表）。本阶段没有热路径，等阶段六有真实调用频率时再议。
4. `thinking=False` 是 §1「兜底」的逃生门：补丁若阻塞进度，可以临时关闭思考模式让图先跑起来，**但必须在 `docs/decisions/` 写下决定与恢复计划**。默认值为 `True`，不要为了"先跑通"把它改成默认关闭。

### 4.3 `src/pyassist/llm/tracing.py`

```python
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

from langchain_core.messages import AIMessage

from pyassist.config import PEAK_WINDOWS_UTC, PRICE_TABLE
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
        raise RuntimeError(
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
    路径比一条不完整的路径更危险（见「待定项 4」）。
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
```

三条要点：

1. **全程 `Decimal`**，不出现 `float`。`cost_usd` 到阶段四的信封里才转 JSON number，转换发生在序列化边界。
2. `cached_tokens = min(cached_tokens, prompt_tokens)` 是防负数计费的**防御**，不是对上游的假设。
3. `is_off_peak` 对无时区输入直接报错 —— 用本地时区算价格是"看起来对但全错"的典型。

### 4.4 `src/pyassist/llm/prompts/`

**`__init__.py`**（加载器；本文件不是占位包，见 T6）：

```python
"""Prompt 模板加载器（§2：prompt 文本的唯一家）。

阶段一起所有 `__init__.py` 保持为空。这里有意破例：`prompts` 不是占位包，
它的公开面就是 `load_prompt()`；另开 `loader.py` 只多一个文件，不多一层抽象。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

PROMPT_DIR: Final = Path(__file__).resolve().parent

_HISTORY_PLACEHOLDER: Final = "history"


@lru_cache(maxsize=None)
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
```

**`qa.jinja2`**（v1 问答的系统提示，阶段五的检索结果由 `history` 注入）：

```jinja
你是 Python 学习助手。回答必须严格基于检索到的文档片段。

规则：
1. 只使用文档里出现的内容作答，不得凭记忆补充 API、参数或行为。
2. 文档里没有答案时，直接说明"文档中没有相关内容"，不要猜测。
3. 涉及 API 的结论必须能追到具体文档片段。
4. 代码示例用 ```python 围栏，保持可直接运行。
5. 用中文回答，术语保留英文原文（generator、decorator、GIL）。

回答语言：{{ output_language }}
```

**模板硬约束**（`test_prompt_stability.py` 会扫）：禁止时间戳、随机 id、`datetime.now()`、`uuid`、`random`、未排序的 dict 迭代。这些会击穿前缀缓存，让成本涨 50 倍（§1 / §4）。

**关于 ROADMAP 说的"渲染顺序固定 `tools` → `system` → `messages`"**：这三段不由加载器排。`tools` 是 `bind_tools()` 放进**出站 payload 顶层**的独立字段（OpenAI 协议如此），`system` 与 `messages` 才是加载器的产物。加载器要保证的是另一件事、也是真正会出错的那件事：**`system` 段逐字节稳定、一切易变输入只经由 `history`（排在最后）进入**。前缀缓存只认前缀，`history` 在前面塞任何东西都会把命中率打到零。

### 4.5 `tests/test_deepseek_reasoning.py`（三条，全离线）

```python
"""阶段二的强制回归测试（§1.4 / §5.1）。

三条全部离线：前两条直接构造消息调 `_get_request_payload`（不发请求），
第三条用 `respx` mock 两轮。模型实例一律经 `llm/client.py` 取 ——
测试里自己构造模型会变成事实上的第二个所有者。
"""

from __future__ import annotations

import json

import httpx
import respx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

from pyassist.config import MODEL_FLASH, settings
from pyassist.llm.client import get_chat_model

TEST_API_KEY = SecretStr("test-only-placeholder-key")
CHAT_URL = f"{settings.deepseek_api_base_url}/chat/completions"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "检索 Python 文档",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
]


def _assistant_with_tool_call(*, reasoning: str | None) -> AIMessage:
    additional = {} if reasoning is None else {"reasoning_content": reasoning}
    return AIMessage(
        content="",
        tool_calls=[{"name": "search_docs", "args": {"query": "asyncio"}, "id": "call_1"}],
        additional_kwargs=additional,
    )


def test_payload_carries_reasoning_content_of_tool_call_message() -> None:
    """payload 级：带 tool_calls 的 assistant 消息，reasoning_content 必须出现在出站 JSON 里。"""
    model = get_chat_model(api_key=TEST_API_KEY)
    payload = model._get_request_payload(
        [HumanMessage("asyncio.gather 怎么用"), _assistant_with_tool_call(reasoning="先查文档")]
    )
    assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "先查文档"


def test_payload_backfills_empty_string_when_reasoning_content_missing() -> None:
    """回填级：带 tool_calls 却没有 reasoning_content 时，键必须存在且为空串 —— 不是被省略。"""
    model = get_chat_model(api_key=TEST_API_KEY)
    payload = model._get_request_payload(
        [HumanMessage("asyncio.gather 怎么用"), _assistant_with_tool_call(reasoning=None)]
    )
    assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert "reasoning_content" in assistant
    assert assistant["reasoning_content"] == ""


@respx.mock
def test_second_round_request_keeps_reasoning_content() -> None:
    """连续两轮回归（强制）：第一轮 tool_calls → ToolMessage → 第二轮请求必须带上该字段。

    这就是 §5.1 给所有涉及工具调用的功能设的那道门：只测"第一步能调工具"不算通过。
    """
    first_round = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL_FLASH,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "第一步：检索 asyncio 文档",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "search_docs", "arguments": '{"query": "asyncio"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 64,
            "prompt_cache_miss_tokens": 36,
        },
    }
    second_round = {
        "id": "cmpl-2",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL_FLASH,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "用 asyncio.gather 并发执行协程。"},
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 15, "total_tokens": 215},
    }
    respx.post(CHAT_URL).mock(
        side_effect=[httpx.Response(200, json=first_round), httpx.Response(200, json=second_round)]
    )

    model = get_chat_model(api_key=TEST_API_KEY).bind_tools(TOOLS)
    messages = [HumanMessage("asyncio.gather 怎么用")]
    first_reply = model.invoke(messages)
    model.invoke(
        [*messages, first_reply, ToolMessage(content="文档片段：gather 并发执行。", tool_call_id="call_1")]
    )

    assert len(respx.calls) == 2
    second_request = json.loads(respx.calls[1].request.content)
    assistant = next(m for m in second_request["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "第一步：检索 asyncio 文档"
```

### 4.6 `tests/test_prompt_stability.py`

```python
"""缓存前缀稳定性守护（§1）：模板渲染必须逐字节可复现。"""

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
```

### 4.7 `tests/test_tracing.py` 与 `tests/test_usage_mapping.py`

```python
# tests/test_tracing.py —— 分别计价与错峰边界
def test_cache_hit_and_miss_are_priced_separately() -> None:
    """命中与未命中必须分别计价：合并成一个 prompt_tokens 会让报表严重失真（§3.1）。"""
    cost = price_usage(
        MODEL_FLASH,
        cached_tokens=1_000_000,
        uncached_input_tokens=1_000_000,
        output_tokens=1_000_000,
        is_off_peak=True,
    )
    assert cost == Decimal("0.753")  # 0.003 + 0.15 + 0.60


def test_peak_price_is_double_off_peak() -> None:
    off_peak = price_usage(MODEL_PRO, cached_tokens=0, uncached_input_tokens=1_000_000,
                           output_tokens=0, is_off_peak=True)
    peak = price_usage(MODEL_PRO, cached_tokens=0, uncached_input_tokens=1_000_000,
                       output_tokens=0, is_off_peak=False)
    assert peak == off_peak * 2


@pytest.mark.parametrize(
    ("moment", "expected_off_peak"),
    [
        (datetime(2026, 10, 5, 0, 59, tzinfo=UTC), True),    # 周一，窗口前
        (datetime(2026, 10, 5, 1, 0, tzinfo=UTC), False),    # 周一，峰值起点（含）
        (datetime(2026, 10, 5, 4, 0, tzinfo=UTC), True),     # 周一，峰值终点（不含）
        (datetime(2026, 10, 5, 6, 0, tzinfo=UTC), False),    # 周一，第二个窗口起点
        (datetime(2026, 10, 5, 10, 0, tzinfo=UTC), True),    # 周一，第二个窗口终点
        (datetime(2026, 10, 10, 2, 0, tzinfo=UTC), True),    # 周六，全时段半价
    ],
)
def test_is_off_peak_boundaries(moment: datetime, expected_off_peak: bool) -> None:
    assert is_off_peak(moment) is expected_off_peak
```

```python
# tests/test_usage_mapping.py —— 缺陷 B 的守护（升级 langchain-deepseek 后先跑这个）
@respx.mock
def test_flat_cache_hit_tokens_become_cache_read() -> None:
    """DeepSeek 返回扁平的 prompt_cache_hit_tokens；langchain_openai 只认
    prompt_tokens_details.cached_tokens。不归一化，cached_tokens 恒为空。"""
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=RESPONSE_WITH_FLAT_CACHE_USAGE))
    message = get_chat_model(api_key=TEST_API_KEY).invoke([HumanMessage("你好")])

    assert message.usage_metadata is not None
    assert message.usage_metadata["input_token_details"]["cache_read"] == 64

    snapshot = extract_usage(message, model_id=MODEL_FLASH, now_utc=datetime(2026, 10, 5, 2, tzinfo=UTC))
    assert snapshot.cached_tokens == 64
    assert snapshot.cost_usd == price_usage(
        MODEL_FLASH, cached_tokens=64, uncached_input_tokens=36, output_tokens=20, is_off_peak=False
    )


@respx.mock
def test_absent_flat_cache_fields_do_not_break_usage() -> None:
    """响应里没有扁平缓存字段时，计价照常走全价 miss，不抛异常。"""
```

---

## 5. 验收

沿用阶段一的五条命令，加上 ROADMAP 阶段二指定的三件套：

```bash
uv sync
uv run python -c "import pyassist"
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src/pyassist
uv run pytest

# 阶段二专属
uv run pytest tests/test_deepseek_reasoning.py tests/test_prompt_stability.py \
              tests/test_tracing.py tests/test_usage_mapping.py

# 人工步骤（不进 CI）：真机模型名校验
uv run python -m pyassist.llm.client --check-models
```

### 验收判据

- [ ] `uv run pytest` 退出码 0（不是 5），全部测试**不碰网络**
- [ ] **三条回归全绿**：payload 级 / 回填级 / **连续两轮工具调用**（第三条是 §5.1 的门）
- [ ] 回填级断言的是 `"reasoning_content" in assistant` **且** `== ""` —— 键存在，不是被省略
- [ ] 模板同输入渲染两次**逐字节一致**；`*.jinja2` 静态扫描无易变 token
- [ ] 计价：命中与未命中分别计价；峰值恰为错峰的两倍；错峰边界（01:00 / 04:00 / 06:00 / 10:00 / 周六）逐条断言
- [ ] `usage_metadata.input_token_details["cache_read"]` 能从扁平的 `prompt_cache_hit_tokens` 归一化出来
- [ ] `ruff check` / `ruff format --check` 无 diff；`mypy --strict src/pyassist` 零错误
- [ ] `src/` 内除 `llm/client.py` 外不出现 `ChatDeepSeek(` / `ChatOpenAI(` / `ChatDeepSeekPatched(`
- [ ] 未创建 `errors.py`、未创建任何图、未建 token 账本
- [ ] `--check-models` 人工跑过一次；**结论记进提交信息或 ADR**（见「待定项 3」）

### 与 ROADMAP 验收的差异（三处，均需知悉）

1. ROADMAP 列了三个测试文件；本文件加了 `tests/test_usage_mapping.py`（缺陷 B 的守护）与一条契约守护扩展。原因见 §1.3：缓存命中用量丢失是实测发现的、ROADMAP 与 AGENTS 都没记载的缺陷，不修则 §3.1 的 `cached_tokens` 恒为空。
2. ROADMAP 说"不一致打 `error` 级日志并在 `/health` 暴露"；`/health` 是阶段四的产物，本阶段只把结果存进 `client.get_last_model_check()`，阶段四接管暴露。
3. ROADMAP 的验收里 `--check-models` 是"真机校验需人工跑一次"；本文件保留该定位并明确**退出码 1（不一致）不算本阶段验收失败** —— 模型名漂移是上游事实，不是集成错误（见「待定项 3」）。

---

## 6. 风险与缓解

| 风险 | 触发点 | 缓解 |
|---|---|---|
| **补丁写错方向**：把 `reasoning_content` 塞进 `content` 的内容块 | T1 | 基类 `_format_message_content` 会**静默剥掉** `type` 为 `thinking` / `reasoning_content` 的块。必须写成出站 dict 的**顶层键**；T2 断言的就是顶层键 |
| 对**普通** assistant 消息（无 `tool_calls`）也回写 `reasoning_content`，真机可能拒绝 | T1 / T8 | 契约 §1 原文要求"写回出站 assistant 消息"，故默认全部回写。真机若报错，**收窄为只处理带 `tool_calls` 的消息**并在 `docs/decisions/` 写 ADR（见「待定项 5」） |
| `payload["messages"]` 与原始消息**对不齐** | T1 | 长度不符时直接返回、不改 payload；不靠索引猜 |
| `models.list()` 与 `EXPECTED_MODEL_IDS` 不一致 | T3 | **只告警不阻断**。§1 自己就说定价页写的是 `deepseek-flash`，`deepseek-v4-flash` 是旧名 —— 上游改名不该打挂应用 |
| `models.list()` 端点不存在 / 网络不通 | T3 | `except openai.OpenAIError` 降级为 `error` 日志 + `is_ok=False`，绝不抛到调用方 |
| `usage_metadata` 的 `cache_read` 键名随 langchain-core 版本变化 | T1 / T5 | 归一化集中在一处；`test_usage_mapping.py` 把行为钉死，升级时会红 |
| 流式路径漏修（只修了非流式） | T1 | `_convert_chunk_to_generation_chunk` 与 `_create_chat_result` **两个都须 override**。阶段六的 SSE 走流式，漏了要等那时才暴露 |
| `is_off_peak` 收到本地时间 | T4 | 无时区直接 `raise ValueError`。用本地时区算价格是"看起来对但全错" |
| 用 `float` 计价产生精度假象 | T4 | 全程 `Decimal`，日志里 `cost_usd` 用 `str()` |
| **测试里出现已停用模型名** | T2 / T5 | `tests/` 在守护扫描面内（`test_contract_guardrails.py:18`），注释与字符串都要避开 |
| **补丁阻塞进度**，需要临时关掉思考模式 | T1 | `get_chat_model(thinking=False)` 就是那个逃生门（§1 兜底）。代价是诊断质量下降，**必须**在 `docs/decisions/` 写下决定与恢复计划。它只该在卡住时启用，不该成为默认值 |
| 贪快把 token 账本 / 熔断一起做了 | T4 | §0「不做」表；账本是阶段三，熔断是阶段八 |
| 依赖升级后忘了补丁是否还需要 | T1 / 全程 | 两个测试文件就是探针：`test_deepseek_reasoning.py`（缺陷 A）与 `test_usage_mapping.py`（缺陷 B） |

---

## 7. 待定项

不阻塞阶段二动工，但需要人拍板。

### 1. `SPEC.md` 的归档策略（本文件已按推荐执行）

`SPEC.md` 原本是阶段一的规格（标题即"阶段一 · 工程底座 · 实施规格"）。本次把它重写为阶段二，阶段一原文**逐行一致**地归档到 `docs/specs/阶段一-工程底座.md`。

- **推荐（已执行）**：归档而非覆盖。理由：`config.py`（"§4 T4"、"待定项 4"）与 `pyproject.toml`（"§5.6"、"§5.1 的疏漏"）有 4 处反向引用，覆盖会让它们悬空。
- 需要人确认：是否接受 `docs/specs/` 这个新目录；若希望 SPEC 逐阶段累积在同一文件里，改回来只是移动文件。
- **附带项**：那 4 处引用现在指向归档件。是否把它们改写成 `docs/specs/阶段一-工程底座.md §4 T4` 这类显式路径，属于代码注释改动，**本阶段未做**，请拍板。

### 2. 缺 API key 时抛什么（T3 会立刻遇到）

`errors.py` 按 ROADMAP 是阶段四的产物。

- **推荐**：本阶段抛 `RuntimeError`，**不留 TODO 占位**，阶段四把 `client.py` 里的这一处换成 `errors.ConfigError`，由 `api/errors.py` 映射成 500。
- 备选：提前在阶段二建 `errors.py`（只放异常层级，不含映射）。代价是阶段四的"契约冻结"要在半成品上做，且阶段一的 SPEC 明确把 `errors.py` 划给阶段四。

### 3. `/health` 在模型名校验不一致 / 缺 key 时是硬失败还是警告

- **推荐**：**警告**。模型名正在漂移（§1 原文），硬失败会让一次上游改名打挂部署；缺 key 则是本地开发常态，硬失败会让 `docker compose up` 在没配 key 时起不来。
- 阶段四落地时需一并定：`/health` 返回 200 但带 `data.status="degraded"`，还是 503。ROADMAP 只写了"暴露"，没写状态码。

### 4. 「缓存命中用量归一化」是否算阶段二范围（本文件已按推荐纳入）

- **推荐（已纳入）**：算。理由：不做则 §3.1 的 `usage.cached_tokens` 恒为空，阶段四的信封与成本报表从上线第一天就是错的，而阶段八的 `/stats` 还要基于它做熔断。
- 备选：放到阶段四，在信封组装处读 `response_metadata["token_usage"]` 兜底。**不推荐** —— 会产生两条取数路径（`usage_metadata` 与 `response_metadata`），互相矛盾时无从判断谁对。
- 需要人确认：把这项写进 `ROADMAP.md` 阶段二的产出清单（现在它只在本文件里）。

### 5. 普通 assistant 消息是否也回写 `reasoning_content`

契约 §1 说"把 `additional_kwargs["reasoning_content"]` 写回出站 assistant 消息"，未区分是否带 `tool_calls`。

- **推荐**：按契约原文全部回写（本文件 §4.1 即如此）。
- 若真机对**非** tool_calls 的 assistant 消息报 400，收窄为只处理带 `tool_calls` 的消息，并写 ADR 到 `docs/decisions/`。这条只能**真机实测**，mock 测不出来。

### 6. `DEEPSEEK_API_BASE_URL` 不带 `/v1` 是否有意

`config.py` 的值是 `https://api.deepseek.com`，而 `langchain-deepseek` 的 `DEFAULT_API_BASE` 是 `https://api.deepseek.com/v1`。

- **推荐**：保持现状（与 DeepSeek 官方 openai-SDK 示例一致），`client.py` 显式传 `base_url` 覆盖库默认值。
- 需人工跑一次 `--check-models` 确认端点可用；若 404，改成带 `/v1` 的形式并在此处记一笔。

---

## 8. 为后续阶段预留的接口

阶段二结束时，下列"缝"必须已经存在，否则后续阶段会以错误的方式重造它们：

| 缝 | 阶段二提供 | 被谁消费 |
|---|---|---|
| 取模型 | `llm.client.get_chat_model(tier=..., thinking=...)` | 阶段六/七/八的**所有**图与 service |
| 修 `reasoning_content` | `llm.deepseek_chat.ChatDeepSeekPatched` | 全部经 `get_chat_model()` 拿到，无人直接构造 |
| 缓存命中用量 | `usage_metadata.input_token_details["cache_read"]`（已归一化） | 阶段四的 `meta.usage.cached_tokens` |
| 计价 | `llm.tracing.extract_usage()` / `price_usage()` / `is_off_peak()` | 阶段四的信封 `meta.usage`、阶段八的熔断 |
| 用量快照 | `llm.tracing.UsageSnapshot`（字段名 = 信封字段名） | 阶段四 `api/schemas.py` 直接序列化 |
| 模型名校验结果 | `llm.client.get_last_model_check()` | 阶段四的 `/health` |
| Prompt 加载 | `llm.prompts.load_prompt(name)` | 阶段六/七/八的 `diagnose.jinja2` 等新模板 |
| 用量打点 | `llm.tracing.log_llm_response()` | 阶段六起的每个 LLM 节点（§3.5 的 `llm.response`） |

阶段二**不**为后续阶段留任何空函数、空类或 TODO 占位 —— 那些是平行实现的种子。缝只以"唯一入口 + 已归一化的数据 + 守护测试"的形式存在。
