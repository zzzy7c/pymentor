"""`search_docs` / `read_doc` 的**实现**（纯 async 函数，不含 `tool(...)` 工厂）。

`tool(...)` 只在 `agent/registry.py`（AGENTS.md §2「定义工具」）。
本文件只 import `InjectedToolArg` 这个**标记类型**，不 import `tool`：
前者是"这个参数由节点注入、模型看不见"的声明，后者才是"注册工具"的动作。
于是函数能被任何地方直接 await，"检索能不能用"与"LLM 会不会调"是两件独立可测的事。

**注入的是检索*行为*（`Searcher` / `Fetcher`），不是 Chroma 句柄。**
理由：`agent/` 层不该 import `chromadb`（守护 #24 的白名单只有三个 rag 文件），
而"给类型注解开个白名单口子"会让那条守护从此挡不住真东西。
注入一个可调用对象后，`agent/` 对"检索怎么实现"零知识，测试也照样能塞夹具。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated

from langchain_core.tools import InjectedToolArg

from pyassist.config import DEFAULT_TOP_K, READ_DOC_MAX_CHARS
from pyassist.logging import get_logger
from pyassist.rag import retriever as retriever_module
from pyassist.rag.retriever import RetrievedChunk

logger = get_logger(__name__)

Searcher = Callable[[str, int], Awaitable[list[RetrievedChunk]]]  # (query, top_k)
Fetcher = Callable[[str], Awaitable[list[RetrievedChunk]]]  # (doc_url,)

# ---------- 报错解析（SPEC §5.4）：纯函数，离线可测 ----------

_MAX_MESSAGE_CHARS = 500
_MAX_RAW_TAIL_CHARS = 1_000
_MAX_FRAMES = 10

_EXCEPTION_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s?(?P<message>.*))?$")
_FRAME_LINE = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<function>.+?))?\s*$'
)

# 没有冒号的异常行（`KeyboardInterrupt` / `SystemExit`）靠后缀兜底。
# 若不筛，traceback 里被打印出来的源码行（例如单独一个变量名）会被误认成异常类。
_EXCEPTION_SUFFIXES = ("Error", "Exception", "Exit", "Interrupt", "Warning", "Iteration")


@dataclass(frozen=True, slots=True)
class TraceFrame:
    """一帧调用位置。`file` 只留 basename —— 宿主绝对路径不进检查点、不进 prompt。"""

    file: str
    line: int | None
    function: str


@dataclass(frozen=True, slots=True)
class ErrorSignature:
    """从 traceback 文本里能**确定**拿到的全部信息（D10 的检索 query 就由它派生）。

    这里不做任何"推测"：解析不出类型就是 `Unknown`，宁可让模型说"信息不足"，
    也不要猜一个错误类型去误导检索。
    """

    exception_type: str
    message: str
    frames: tuple[TraceFrame, ...]
    last_frame: TraceFrame | None
    raw_tail: str


def parse_traceback(error_text: str) -> ErrorSignature:
    """解析 Python 3.12 的 traceback 文本（SPEC §5.4 五条规则）。

    - 从**末尾**往前找异常行：链式异常取最外层（文件末尾那条）；
    - `json.decoder.JSONDecodeError` 归一到 `JSONDecodeError` —— 模型与评估集
      说同一种名字，且模块前缀对检索没有帮助；
    - `SyntaxError` 的 `File "..."` 行没有 `in ...`，`function` 记为 `<module>`；
    - 帧只留最后 10 帧（离出错点最近的），长度上限是为了不让检查点变成日志文件。
    """
    lines = error_text.splitlines()
    exception_type = "Unknown"
    message = lines[0].strip()[:_MAX_MESSAGE_CHARS] if lines else ""

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        match = _EXCEPTION_LINE.match(line)
        if match is None:
            continue
        type_name = match.group("type").rsplit(".", 1)[-1]
        if ":" not in line and not type_name.endswith(_EXCEPTION_SUFFIXES):
            continue
        exception_type = type_name
        message = (match.group("message") or "").strip()[:_MAX_MESSAGE_CHARS]
        break

    frames: list[TraceFrame] = []
    for raw_line in lines:
        match = _FRAME_LINE.match(raw_line)
        if match is None:
            continue
        path = match.group("file").replace("\\", "/")
        function = (match.group("function") or "<module>").strip()
        frames.append(
            TraceFrame(
                file=PurePosixPath(path).name,
                line=int(match.group("line")),
                function=function,
            )
        )

    kept = tuple(frames[-_MAX_FRAMES:])
    return ErrorSignature(
        exception_type=exception_type,
        message=message,
        frames=kept,
        last_frame=kept[-1] if kept else None,
        raw_tail=error_text[-_MAX_RAW_TAIL_CHARS:],
    )


async def _default_search(query: str, top_k: int) -> list[RetrievedChunk]:
    """生产默认：走 `retriever` 的全局依赖（真实向量库 + 真实嵌入模型）。

    与 `agent/graphs/qa.py` 的 `retrieve` 节点用的是**同一个**函数（跨模块 import），
    复制一份就等于开第二条检索路径（AGENTS.md §2 单点所有权）。
    """
    return await retriever_module.retrieve(query, top_k=top_k)


async def _default_fetch(doc_url: str) -> list[RetrievedChunk]:
    """生产默认：按 URL 取整页。"""
    return await retriever_module.fetch_document(doc_url)


def render_chunks(chunks: Sequence[RetrievedChunk], *, start_index: int) -> str:
    """把片段渲染成**带编号**的文本。纯函数，**起点由调用方给**（SPEC D4）。

    编号规则与 `rag/citations.build_citations` 逐字同构：`index = 全局位置 + 1`。
    `chunks` 在状态里**只追加**，所以 `start_index = len(已有 chunks) + 1`
    让"这里渲染出的 `[7]`"与"`citations[6]`"恒等 —— 不需要第二个编号来源。
    """
    return "\n\n".join(
        f"[{start_index + offset}] {chunk.title}"
        + (f" — {chunk.section}" if chunk.section else "")
        + f"\n{chunk.text}"
        for offset, chunk in enumerate(chunks)
    )


async def search_docs(
    query: str,
    *,
    top_k: Annotated[int, InjectedToolArg] = DEFAULT_TOP_K,
    start_index: Annotated[int, InjectedToolArg] = 1,
    searcher: Annotated[Searcher | None, InjectedToolArg] = None,
) -> tuple[str, list[RetrievedChunk]]:
    """当前上下文不足以回答时，再检索一次 Python 官方文档。返回带编号的片段文本。

    无命中返回 `("", [])`。`query` 是**唯一**模型可见的参数（L15：其余三个都是
    `InjectedToolArg`，
      不出现在 schema 里，由 `tools` 节点写进 `tool_call["args"]`）。
    - `start_index` 由节点按"这批片段的全局首号"算好传来 —— **工具自己不编号**，
      它只把节点给的起点往后排（SPEC D4）。
    - `searcher` 是节点注入的检索行为，与 `retrieve` 节点**同一个可调用对象**，
      于是工具轮与首轮检索走同一条路径。不注入时落到本模块的 `_default_search`。
    - 空串直接返回 `("", [])`（不打向量库：空查询会嵌入出零向量，cosine 距离退化为
      1.0，等于随机返回 5 条 —— 那比"没有"更危险）。
    """
    if not query.strip():
        return "", []
    chunks = await (searcher or _default_search)(query, top_k)
    return render_chunks(chunks, start_index=start_index), chunks


async def read_doc(
    doc_url: str,
    *,
    fetcher: Annotated[Fetcher | None, InjectedToolArg] = None,
) -> str:
    """需要某页完整内容时，按文档 URL 取整页文本。超长会截断并明确声明。

    入参可带锚点，`retrieve.fetch_document` 内部会剥掉（阶段五已定型）。
    返回**字符串**而不是 chunk 列表：这个工具是给模型"补上下文"的，**不产生新引用编号** ——
    一页可能几十个 chunk，全塞进 `citations` 会撑爆来源块，而 grounding 的依据
    仍是 `search_docs` 给过编号的那些片段。所以它在 registry 里用 `response_format`
    默认的 `"content"`，**没有** artifact。
    """
    chunks = await (fetcher or _default_fetch)(doc_url)
    if not chunks:
        return f"文档中没有可读取的内容：{doc_url}"
    text = "\n\n".join(chunk.text for chunk in chunks)
    if len(text) > READ_DOC_MAX_CHARS:
        return text[:READ_DOC_MAX_CHARS] + "\n\n（已截断，只保留前段内容）"
    return text


async def explain_error(
    error_text: str,
    *,
    top_k: Annotated[int, InjectedToolArg] = DEFAULT_TOP_K,
    start_index: Annotated[int, InjectedToolArg] = 1,
    searcher: Annotated[Searcher | None, InjectedToolArg] = None,
) -> tuple[str, list[RetrievedChunk]]:
    """解析一段 Python 报错（traceback），并在官方文档里查找相关说明。

    当用户只贴了报错、上下文不足时用它：它会提取异常类型、出错位置与消息，
    再按这些信息检索文档。返回带编号的片段，可直接引用。`error_text` 是唯一模型可见参数。
    """
    signature = parse_traceback(error_text)
    if signature.message:
        query = f"{signature.exception_type}: {signature.message}"[:200]
    else:
        query = signature.exception_type[:200]

    chunks = await (searcher or _default_search)(query, top_k) if query.strip() else []

    if signature.last_frame is None:
        location = "未解析到调用位置"
    else:
        frame_line = "?" if signature.last_frame.line is None else str(signature.last_frame.line)
        location = f"{signature.last_frame.file}:{frame_line} in {signature.last_frame.function}"

    parts = [
        f"错误类型：{signature.exception_type}",
        f"最后位置：{location}",
        f"错误消息：{signature.message or '（无）'}",
    ]
    if chunks:
        parts.append("相关文档片段：")
        parts.append(render_chunks(chunks, start_index=start_index))
    else:
        parts.append("相关文档片段：未检索到相关内容。")
    return "\n".join(parts), chunks
