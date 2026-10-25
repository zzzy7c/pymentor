"""★ 唯一写向量库的地方（§2 单点所有权）。

**幂等不是"盲 upsert"，是"先收敛后写入"**（SPEC D9）。纯 `upsert` 永不删除：
语料删了一篇、或改了分块参数，重跑后 `count()` 只增不减 —— 库里会永远留着
上一代切法的幽灵块，检索命中它们时给出的引用指向已不存在的内容。
所以每次 ingest 都先算 `desired` 与 `existing` 的差集并 `delete` 掉陈旧项。

**运行时禁令**（§1）：`services/*` 不得在运行时往向量库 upsert。
ingestion 是一个**独立、显式**的步骤 —— 它是本模块的 CLI，不是任何请求路径的一部分。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from itertools import batched
from pathlib import Path
from typing import Literal, cast

from chromadb.api.models.Collection import Collection
from chromadb.api.types import Where

from pyassist.config import (
    CHUNK_MAX_CHARS,
    CHUNK_OVERLAP_CHARS,
    CORPUS_DIR,
    settings,
)
from pyassist.errors import AppError, CorpusMissing, StoreError
from pyassist.logging import configure_logging, get_logger
from pyassist.rag import corpus as corpus_module
from pyassist.rag import retriever as retriever_module
from pyassist.rag import store as store_module
from pyassist.rag.corpus import ParsedDocument
from pyassist.rag.embedder import Embedder, get_embedder

logger = get_logger(__name__)

_PARAGRAPH_SEPARATOR = "\n\n"

# 分批写入不是过早优化：Chroma 单次写入过大会撞 `BatchSizeExceededError`（错误码 413）。
#
# 上界由**内存**定，不是由 Chroma 定：嵌入期的注意力掩码是 O(batch × seq²)。
# seq 由 `EMBEDDING_MAX_SEQ_TOKENS`（1024）封顶，于是这一批最坏 64 × 1024² × 4B ≈ 268MB；
# 原来的 256 在同一条算式下是 1GB 起步（2026-10-13 OOM 的直接乘数）。
_UPSERT_BATCH_SIZE = 64

# `<pre>` 超过 max_chars 的这个倍数才退让去按行切。低于它一律原子保留 ——
# 把一段完整代码从中间劈开，检索能命中、引用却给出半截程序（SPEC D5）。
#
# **正文块没有这个豁免**，见 `_iter_pieces`。
_CODE_SPLIT_RATIO = 2

# 一块正文的**真实**字符上界：代码块有 `_CODE_SPLIT_RATIO × max` 的原子性豁免
# （SPEC D5 第 3 条 —— 1000~2000 字符的代码块会整块留下），聚合时再叠一个重叠窗口，
# 中间那个 `\n\n` 分隔符算 2。超过它才是"某个块根本没被拆开"，即 OOM 的入口。
#
# **别按 `max_chars + overlap` 收紧这个数**：那会让 `_warn_oversize` 在健康语料上
# 每次 ingest 报 73 条（本机 3.12 文档实测）—— 一个天天响的警告等于没有警告，
# 真正出事那次反而没人看。
_MAX_BODY_CHARS = _CODE_SPLIT_RATIO * CHUNK_MAX_CHARS + CHUNK_OVERLAP_CHARS + 2

# 长任务报进度的密度：整轮一共约 20 条（每 5% 一条）。不按"每批一条"记 ——
# 那会在 273 个批次里刷 273 行，把真正要看的 warn/error 顶出屏幕。
_PROGRESS_REPORTS = 20

# 验收时最多要多少条检索结果，以及"要得到却给不全"的判据，都在
# `rag/retriever.py::probe_index_depth` 里 —— §2 把"读向量库"整体划给了它（守护 #24）。

# 验收失败时给出的恢复办法。**这里必须说清"向量还在 WAL 里"** ——
# 否则看到失败的人第一反应是"又得重跑几小时"，而实际上删掉那个段目录就能重建。
_RECOVER_HINT = (
    "删掉 data/chroma 下的那个 UUID 段目录（保留 chroma.sqlite3）后重跑本命令；"
    "向量仍然在 WAL 里，读路径会现场重建，不必重新嵌入"
)


@dataclass(frozen=True)
class Chunk:
    """向量库里一条记录的完整形状。`body` 与 `embed_text` 分开是刻意的（SPEC D5）。"""

    chunk_id: str
    body: str  # 存进 collection 的 documents —— 纯正文
    embed_text: str  # 送去嵌入的文本 —— title + section + body
    doc_url: str
    source_url: str  # 带锚点
    title: str
    section: str | None
    ordinal: int


@dataclass(frozen=True)
class IngestReport:
    documents: int
    chunks: int
    upserted: int
    deleted: int
    duration_ms: int


# ---------- 分块 ----------


def build_chunks(
    document: ParsedDocument,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    """一篇文档 → 有序 `Chunk` 列表。纯函数（不出网、不读盘、不碰向量库）。

    三步：
    1. 块级元素按 (section, anchor) **分组** —— 跨小节聚合会让 chunk 的主题漂移；
    2. 组内按 `max_chars` 聚合，相邻块之间留 `overlap_chars` 的重叠窗口；
    3. 编号、派生 id、生成嵌入文本。

    `chunk_id` 严格按 §5.2 的「**内容哈希 + 来源 URL**」派生，**位置无关、不含 ordinal**。
    代价是同一页里完全重复的段落会合并成一条 —— 那是去重，不是 bug（ordinal 仍落 metadata 供排序）。

    **产出上界**：每个 `body` ≤ `_MAX_BODY_CHARS`（代码块的原子性豁免已计入）。
    这个上界是嵌入期内存的前提（`EMBEDDING_MAX_SEQ_TOKENS` 按它推算），由
    `tests/test_ingest_chunk_size.py` 与 `_warn_oversize` 两头盯着。
    """
    chunks: list[Chunk] = []
    seen: set[str] = set()
    ordinal = 0

    for (section, anchor), pieces in _group_by_section(document.blocks, max_chars=max_chars):
        source_url = f"{document.doc_url}#{anchor}" if anchor else document.doc_url
        for body in _aggregate(pieces, max_chars=max_chars, overlap_chars=overlap_chars):
            chunk_id = _chunk_id(source_url, body)
            # 同页重复段落合并：后一条与先一条完全同源，留着只会让检索结果里出现两个
            # 编号相同、指向同一锚点的引用条目。
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    body=body,
                    embed_text=_embed_text(document.title, section, body),
                    doc_url=document.doc_url,
                    source_url=source_url,
                    title=document.title,
                    section=section,
                    ordinal=ordinal,
                )
            )
            ordinal += 1

    return chunks


def _chunk_id(source_url: str, body: str) -> str:
    """`sha256(f"{source_url}\\0{body}")[:32]`（§5.2 / SPEC D5）。

    用 NUL 而非 `|` 之类的可见字符当分隔符：正文里出现 `|` 是家常便饭，
    而它一旦出现在分隔位置，"两个字段的哈希"就会与"另一个字段组合的哈希"碰撞。
    """
    digest = hashlib.sha256(f"{source_url}\0{body}".encode()).hexdigest()
    return digest[:32]


def _embed_text(title: str, section: str | None, body: str) -> str:
    """嵌入文本带标题上下文前缀 —— 对小语料 Recall 提升明显（SPEC D5）。

    但 `documents` 存**纯 body**：引用片段要是干净正文，不带重复标题。
    """
    return f"{title}\n{section or ''}\n{body}"


def _group_by_section(
    blocks: Sequence[corpus_module.Block], *, max_chars: int
) -> Iterator[tuple[tuple[str | None, str | None], list[str]]]:
    """按 (section, anchor) 把块切成连续的组；超长代码块在这一步被按行拆开。

    用 `itertools.groupby` 的语义（相邻分组，不跨组归并）而非 dict 归并：
    同一小节在文档里出现两次时（Sphinx 的返回顶部链接会造出这种结构），
    合并会让 chunk 顺序与文档顺序脱节，`ordinal` 排序随之失真。
    """
    current: tuple[str | None, str | None] | None = None
    pieces: list[str] = []

    for block in _iter_pieces(blocks, max_chars=max_chars):
        key = (block[1], block[2])
        if current is not None and key != current:
            yield current, pieces
            pieces = []
        current = key
        pieces.append(block[0])

    if current is not None:
        yield current, pieces


def _iter_pieces(
    blocks: Sequence[corpus_module.Block], *, max_chars: int
) -> Iterator[tuple[str, str | None, str | None]]:
    """产出 `(text, section, anchor)`，保证每片都 ≤ `max_chars`（代码里的超长单行除外）。

    两类块的待遇**不同，且是刻意的**（SPEC D5 第 3 条）：

    - **代码块**：尽量原子。超过 2×max 才按行退让 —— 把一段完整程序从中间劈开，
      检索能命中、引用却给出半截代码，比不切更糟。
    - **正文块**：没有原子豁免，超 `max_chars` 就按词边界拆。因为"一块正文"不是
      "一段完整语义"：`<li>` / `<dd>` 里嵌子列表时，`parse_document` 的 `get_text(" ")`
      会把整棵子树展平成一个块，Python 官方文档里最大的有 47723 字符。
      它既是一条概括整页目录的垃圾向量，也是嵌入期 OOM 的入口
      （掩码内存 O(batch × seq²)，2026-10-13 实测崩在这）。
    """
    for block in blocks:
        if block.kind == "code":
            if len(block.text) > _CODE_SPLIT_RATIO * max_chars:
                for piece in _split_code(block.text, max_chars):
                    yield piece, block.section, block.anchor
            else:
                yield block.text, block.section, block.anchor
        else:
            for piece in _split_prose(block.text, max_chars):
                yield piece, block.section, block.anchor


def _split_prose(text: str, max_chars: int) -> list[str]:
    """按**词边界**把正文拆成 ≤ `max_chars` 的片段。纯函数。

    为什么不复用 `_split_code`：代码里换行是唯一安全的边界，正文里空白才是
    （在词中间切会造出半个标识符，检索到了也读不懂）。

    唯一硬切的场合是**单个词本身超长**（没有空白的 base64 / 压缩串 / 超长 URL）——
    那时没有更好的边界可选，切坏一个词也好过让整批嵌入炸掉。
    """
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    buffer = ""
    for word in text.split():
        candidate = f"{buffer} {word}" if buffer else word
        if buffer and len(candidate) > max_chars:
            pieces.append(buffer)
            buffer = word
        else:
            buffer = candidate
        while len(buffer) > max_chars:
            pieces.append(buffer[:max_chars])
            buffer = buffer[max_chars:]

    if buffer:
        pieces.append(buffer)
    return pieces


def _split_code(text: str, max_chars: int) -> list[str]:
    """按**行**切超长代码。行是代码里唯一不会造出半个 token 的边界。

    单行本身超过 `max_chars` 时会被整行留下（宁可超一点，也不能从一行代码中间劈开）。
    这类片段由 `_warn_oversize` 报出来，最终仍受嵌入模型的 token 上限保护。
    """
    pieces: list[str] = []
    buffer: list[str] = []
    size = 0
    for line in text.splitlines():
        extra = len(line) + (1 if buffer else 0)
        if buffer and size + extra > max_chars:
            pieces.append("\n".join(buffer))
            buffer = []
            size = 0
        buffer.append(line)
        size += extra
    if buffer:
        pieces.append("\n".join(buffer))
    return pieces


def _aggregate(texts: Sequence[str], *, max_chars: int, overlap_chars: int) -> list[str]:
    """把同一小节内的若干块聚合成 ≤ `max_chars` 的正文，相邻正文之间有重叠窗口。"""
    bodies: list[str] = []
    buffer = ""

    for text in texts:
        candidate = f"{buffer}{_PARAGRAPH_SEPARATOR}{text}" if buffer else text
        if buffer and len(candidate) > max_chars:
            bodies.append(buffer)
            carry = _carry_over(buffer, overlap_chars)
            buffer = f"{carry}{_PARAGRAPH_SEPARATOR}{text}" if carry else text
        else:
            buffer = candidate

    if buffer:
        bodies.append(buffer)
    return bodies


def _carry_over(body: str, overlap_chars: int) -> str:
    """取 `body` 尾部作为重叠窗口，**切在段落边界**（SPEC D5 第 2 条）。

    三条边界规则：
    - 窗口为 0 或 body 只有一段 → 不重叠。单段整体重复不叫重叠，叫复制；
    - 从尾部往前收，直到再多一段就超出窗口为止；
    - 全部段落都能装进窗口 → 返回空：那说明窗口比正文还大，重复整段毫无意义。

    段落比窗口长时**不硬切** —— 在段落中间切会造出半个词，
    检索到的片段读起来是坏的，比不重叠更糟。
    """
    if overlap_chars <= 0:
        return ""

    paragraphs = body.split(_PARAGRAPH_SEPARATOR)
    if len(paragraphs) < 2:
        return ""

    carry: list[str] = []
    size = 0
    for paragraph in reversed(paragraphs):
        extra = len(paragraph) + (len(_PARAGRAPH_SEPARATOR) if carry else 0)
        if size + extra > overlap_chars:
            break
        carry.append(paragraph)
        size += extra

    if len(carry) == len(paragraphs):
        return ""
    return _PARAGRAPH_SEPARATOR.join(reversed(carry))


# ---------- 写入 ----------


def ingest_corpus(
    corpus_dir: Path,
    *,
    collection: Collection,
    embedder: Embedder,
    paths: Sequence[str] | None = None,
) -> IngestReport:
    """语料 → 向量库。**先收敛后写入**（SPEC D9）。

    `documents` 是按相对 posix 路径排序的 `list`，所以 desired 集合的生成顺序确定；
    集合本身无序，但 re-run 的可复现性靠的是排序后的扫描，不是集合的迭代顺序。
    """
    started = time.perf_counter()

    documents = corpus_module.iter_corpus_documents(corpus_dir)
    if paths is not None:
        wanted = {path.replace("\\", "/") for path in paths}
        documents = [doc for doc in documents if _relative_posix(doc.path, corpus_dir) in wanted]

    if not documents:
        raise CorpusMissing(
            f"语料目录 {corpus_dir} 下没有可索引的文档"
            + (f"（--paths 过滤后为空：{sorted(paths or [])}）" if paths else ""),
            details={"corpus_dir": str(corpus_dir)},
        )

    chunks = [
        chunk
        for document in documents
        for chunk in build_chunks(corpus_module.parse_document(document))
    ]
    desired_ids = {chunk.chunk_id for chunk in chunks}
    _warn_oversize(chunks)

    # `--paths` 模式下把收敛范围收窄到这些页面 —— 否则单页重建会删光其余文档（D9 的安全阀）。
    doc_urls = [document.doc_url for document in documents] if paths is not None else None
    deleted = prune_stale(collection, desired_ids, doc_urls=doc_urls)
    upserted = upsert_chunks(collection, chunks, embedder)

    report = IngestReport(
        documents=len(documents),
        chunks=len(chunks),
        upserted=upserted,
        deleted=deleted,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    logger.info("ingest.done", **asdict(report))
    return report


def _warn_oversize(chunks: Sequence[Chunk]) -> None:
    """报出超出 `_MAX_BODY_CHARS` 的 chunk。**正常语料上不该有输出。**

    留这一行是因为它守的那件事不会自己出声：块一旦超上界，嵌入期要么被
    `EMBEDDING_MAX_SEQ_TOKENS` 静默截断（检索悄悄变差），要么在 seq 更大的模型上
    直接把掩码撑爆。**别把"今天没触发"当成"不需要"。**
    """
    oversize = [chunk for chunk in chunks if len(chunk.body) > _MAX_BODY_CHARS]
    if not oversize:
        return
    longest = max(oversize, key=lambda chunk: len(chunk.body))
    logger.warning(
        "ingest.chunk_oversize",
        count=len(oversize),
        total=len(chunks),
        max_body_chars=len(longest.body),
        bound_chars=_MAX_BODY_CHARS,
        sample_source_url=longest.source_url,
    )


def prune_stale(
    collection: Collection,
    desired_ids: set[str],
    *,
    doc_urls: Sequence[str] | None = None,
) -> int:
    """删掉库里不在 `desired_ids` 里的 chunk，返回删除条数。

    `doc_urls` 非空时只在这些页面的既有 chunk 里做差集 —— 这是防
    「开发期单页重建变清库」的**安全阀**，由测试覆盖（判据 17）。
    """
    where = _doc_url_filter(doc_urls)
    existing = _existing_ids(collection, where=where)
    stale = sorted(existing - desired_ids)
    if stale:
        # `sorted`：让日志与测试输出可复现，而不是随集合迭代顺序抖动。
        collection.delete(ids=stale)
        logger.info("ingest.pruned", deleted=len(stale))
    return len(stale)


def _existing_ids(collection: Collection, *, where: Where | None) -> set[str]:
    """读回库里的 id 集合。`include` 只允许 `metadatas` / `documents`（C6：不允许 distances）。"""
    result = collection.get(where=where, include=["metadatas"])
    ids = result["ids"]
    return {str(item) for item in (ids or [])}


def _doc_url_filter(doc_urls: Sequence[str] | None) -> Where | None:
    """`{"doc_url": {"$in": [...]}}`，或 `None`（不过滤）。

    单独成函数不是为了复用（只用一处），而是为了让**每一层**都拿到 mypy 的期望类型上下文：
    `Where` 是深度嵌套的 `Dict` 联合，直接写一行字面量时内层 `list[str]` 会被推成
    `list[str | int | float | bool]` 的对立面，报一个与真实意图无关的错。
    """
    if not doc_urls:
        return None
    values: list[str | int | float | bool] = list(doc_urls)
    clause: dict[Literal["$in", "$nin"], list[str | int | float | bool]] = {"$in": values}
    return {"doc_url": clause}


def upsert_chunks(
    collection: Collection,
    chunks: Sequence[Chunk],
    embedder: Embedder,
    *,
    batch_size: int = _UPSERT_BATCH_SIZE,
) -> int:
    """分批嵌入并 `upsert`，返回写入条数。

    `upsert` 是**替换语义且幂等**（C8）：同 id 重复 upsert 后 `count()` 不翻倍。
    这正是幂等能成立的一半原因；另一半是 `prune_stale` 的删除。

    **会报进度**：本机全量 17366 chunk 要跑 3~5 小时（CPU 推理，SPEC §10 第 14 条）。
    一行不出的四小时和"已经卡死"在终端里长得一模一样，所以每 5% 打一条
    `ingest.progress`（带 `elapsed_ms` 与 `eta_ms`）—— 让人能判断该等还是该停。

    **分批顺序不等于传入顺序**，见 `_length_bucketed`。
    """
    total_chunks = len(chunks)
    progress_every = max(batch_size, total_chunks // _PROGRESS_REPORTS)
    started = time.perf_counter()
    reported_at = 0
    total = 0
    # 先数一遍 token，才好分批（`_length_bucketed`）。全量扫一遍约 3~4 秒，
    # 相对 6 小时的嵌入可忽略；**换来的 42% 不是**。数 token 的能力归 `embedder`（§2），
    # 这里只是拿它的结果。
    lengths = embedder.count_tokens([chunk.embed_text for chunk in chunks])
    for batch in _length_bucketed(chunks, batch_size, lengths):
        # `cast` 而不是 `list(...)`：`list[float]` 本来就**是** `Sequence[float]`，
        # 只是 list 的不变性让 mypy 不肯自动放行 `list[list[float]]` → `list[Sequence[float]]`。
        # 这是**视图变化，不是数据变化** —— 复制一份向量会在两万个 chunk 上白烧内存。
        embeddings = cast(
            "list[Sequence[float]]",
            embedder.embed_documents([chunk.embed_text for chunk in batch]),
        )
        collection.upsert(
            ids=[chunk.chunk_id for chunk in batch],
            embeddings=embeddings,
            documents=[chunk.body for chunk in batch],
            metadatas=[_chunk_metadata(chunk) for chunk in batch],
        )
        total += len(batch)
        if total - reported_at >= progress_every or total == total_chunks:
            reported_at = total
            _log_progress(total=total, total_chunks=total_chunks, started=started)
    return total


def _length_bucketed(
    chunks: Sequence[Chunk], batch_size: int, token_lengths: Sequence[int]
) -> Iterator[tuple[Chunk, ...]]:
    """按 **token 数**降序分批 —— 为的是填充，不是顺序（SPEC §10 第 14 条）。

    嵌入器把一批 padding 到**批内最长**的那条。文档顺序下，一批里混着 200 字符的正文和
    4000 字符的代码块，前者的注意力掩码就被白白撑成后者的长度：实测全量 4.08M 真实 token
    被算成 7.13M（1.75×，约 4.8 小时）。按长度分桶后同批长度相近 → 4.10M（1.01×），
    省掉 **42%** 的前向计算。

    **必须是 token 数，不是 `len(embed_text)`**。2026-10-13 先按字符数实现了一版，
    实测只省 **20.1%**（5,691,408 vs 4,115,980）—— 字符与 token 在这份语料上不是同一个序：
    中文正文一个字符约一个 token，而代码块里成串的缩进空格与三引号要摊到好几个 token。
    差的那 22 个百分点是**每次重建索引 1.3 小时**。`token_lengths` 由 `embedder.count_tokens`
    给出 —— 口径必须与 `embed_documents` 一致，见 `rag/embedder.py`。

    **降序而不是升序**：ETA 是拿"已完成批次的平均速度"外推的（`_log_progress`）。
    先跑最短的批次会让前几条进度给出过于乐观的 ETA，然后随着批次变长一路变坏 ——
    一个 6 小时的任务，看着"还剩 2 小时"慢慢变成"还剩 5 小时"，人会去 kill 它。
    倒序让 ETA 从偏悲观**单调收敛**到真值。两种方向的**分桶结果完全相同**（同一组批次，
    只是顺序相反），所以填充上的收益一分不少。

    **顺序本身没有意义，所以重排是安全的**：
    - `chunk_id` 是内容 sha256，写进库的就是它，与写入先后无关；
    - `ordinal` 由 `build_chunks` 定，读整页时按它还原文档顺序（`retriever._fetch_document`）；
    - `upsert` 是幂等替换（C8），批次边界不改变最终内容。
    唯一可见的变化是：**日志里的写入顺序不再等于文档顺序**。
    """
    if len(chunks) != len(token_lengths):
        # 靠 `zip` 静默截断会**丢 chunk** —— 索引少了几条，而 ingest 照样报成功。
        # 那正是 2026-10-13 那次事故的形状，宁可在这里炸。
        raise ValueError(f"chunk 与 token 长度数量不一致：{len(chunks)} vs {len(token_lengths)}")
    # `key=` 取元组首位，避免 `Chunk` 参与比较（dataclass 未定序，比到第二个字段就会
    # `TypeError`）。`sorted` 稳定，所以等长的 chunk 保持传入顺序 —— 可复现。
    pairs = sorted(zip(token_lengths, chunks, strict=True), key=_by_length, reverse=True)
    ordered = [chunk for _, chunk in pairs]
    return batched(ordered, batch_size)


def _by_length(pair: tuple[int, Chunk]) -> int:
    """`sorted(key=)` 用：只按 token 数比，不比 `Chunk`。降序由 `reverse=True` 给。"""
    return pair[0]


def _log_progress(*, total: int, total_chunks: int, started: float) -> None:
    """一条进度日志，含 ETA。

    ETA 按**已完成的平均速度**外推。它会在前几批抖动（头几批必然偏慢：模型刚热起来），
    所以它是个量级判断而不是承诺 —— 但"还有 4 小时"和"还有 20 分钟"的差别足够决定了
    要不要继续等。`total_chunks == 0` 到不了这里（空语料在 `ingest_corpus` 就抛了）。
    """
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    remaining = total_chunks - total
    eta_ms = int(elapsed_ms / total * remaining) if total else 0
    logger.info(
        "ingest.progress",
        upserted=total,
        total=total_chunks,
        percent=round(total / total_chunks * 100, 1),
        elapsed_ms=elapsed_ms,
        eta_ms=eta_ms,
    )


def _chunk_metadata(chunk: Chunk) -> dict[str, str | int | float | bool]:
    """落 metadata。**值类型只能是 `str / int / float / bool`**（C11），
    所以 `section` 为 `None` 时写空串、读回时再由检索层还原成 `None`。"""
    return {
        "doc_url": chunk.doc_url,
        "source_url": chunk.source_url,
        "title": chunk.title,
        "section": chunk.section or "",
        "ordinal": chunk.ordinal,
    }


def _relative_posix(path: Path, corpus_dir: Path) -> str:
    """`--paths` 的参数形态：repo 相对 posix 路径，与 `iter_corpus_documents` 的键一致。"""
    try:
        return path.relative_to(corpus_dir).as_posix()
    except ValueError:
        return path.as_posix()


# ---------- CLI ----------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pyassist.rag.ingest",
        description="把 data/corpus 下的语料建进向量库（幂等，可重跑）",
    )
    parser.add_argument("--fetch", action="store_true", help="先抓取官方文档与 PEP，再建索引")
    parser.add_argument("--corpus", type=Path, default=CORPUS_DIR, help="语料根目录")
    parser.add_argument(
        "--paths",
        nargs="+",
        default=None,
        metavar="REL",
        help="只重建这些文档（相对语料根的路径）；收敛范围随之收窄",
    )
    parser.add_argument("--model-id", default=None, help="默认取 settings.embedding_model_id")
    parser.add_argument("--dry-run", action="store_true", help="只报告数量，不写库")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。返回退出码 —— 阶段八的 `docker compose` 与 CI 都靠它。

    正常路径的固定顺序见 SPEC §5.5：先拿到 embedder 的 `dimension` 才能建索引元数据。
    `--dry-run` 是这个顺序的**只读捷径**：不载模型、不建集合、不写库 ——
    它的用途是"先看一眼将要动多少数据"，为此拉起 2GB 的模型是荒谬的。
    """
    args = _build_parser().parse_args(argv)
    configure_logging(settings.log_level, json_output=settings.log_json)

    model_id = args.model_id or settings.embedding_model_id
    corpus_dir: Path = args.corpus

    try:
        if args.dry_run:
            return _dry_run(corpus_dir, paths=args.paths)

        if args.fetch:
            reports = corpus_module.fetch_all(corpus_dir)
            summary = corpus_module.merge_fetch_reports(reports)
            logger.info(
                "corpus.fetch.done",
                documents=summary.documents,
                bytes_written=summary.bytes_written,
                source_url=summary.source_url,
            )

        # 1. embedder 先就位：第 2 步的索引元数据要它的 dimension
        embedder = get_embedder(model_id)
        # 2. 建/取集合（写路径）
        collection = store_module.get_or_create_collection(
            metadata=store_module.build_index_metadata(
                model_id=embedder.model_id, dimension=embedder.dimension
            )
        )
        # 3. 漂移校验：换模型忘了重建索引 → 这里就炸，而不是等检索静默给垃圾
        store_module.assert_index_compatible(collection, model_id=embedder.model_id)

        report = ingest_corpus(
            corpus_dir, collection=collection, embedder=embedder, paths=args.paths
        )
    except AppError as exc:
        # CLI 是终端而不是 HTTP 边界：这里打一行可读的错误就够，
        # 不要为它造第二条"异常 → 面"的映射链（§2：映射只在 api/errors.py）。
        logger.error("ingest.failed", code=str(exc.code), message=exc.message)
        return 1

    _print_report(report)

    # 写入完成 ≠ 入库完成。**这一关不能省**（2026-10-13）：那天 `ingest.done` 之后
    # 重开集合报 `Error loading hnsw index`，而 CLI 已经返回 0 —— 几小时的活白干，
    # 且没有任何信号。所以"成功"的判据改为：**关掉、重开、真读一遍**。
    try:
        verified = verify_index(
            # 全量 ingest 之后库里应当**正好**是这些 chunk；`--paths` 只重建一部分，
            # 库里还有别的文档，条数对不上是正常的，此时只验证"读得回来"。
            expected_chunks=report.chunks if args.paths is None else None
        )
    except AppError as exc:
        logger.error(
            "ingest.verify_failed", code=str(exc.code), message=exc.message, hint=_RECOVER_HINT
        )
        return 2

    logger.info("ingest.verified", count=verified)
    return 0


def verify_index(*, expected_chunks: int | None = None, path: Path | None = None) -> int:
    """**关掉再重开**，真读一遍库，返回检索到的条数。不通过就抛。

    三步都不能省：

    1. **先 `close_clients()`**：HNSW 索引由后台 compactor 异步落盘，`close()` 是
       "把待落盘的活干完"的唯一入口（见 `store.close_clients`）；
    2. **重开**：新 system 只能从磁盘（WAL + 段文件）重建 —— 这才叫"读得回来"。
       在原来那个 system 上 `count()` 等于自己给自己发合格证；
    3. **多要结果**（`retriever.probe_index_depth`）：`count()` 会说谎，见那里的注释。

    ⚠️ 它**不验证向量的正确性**（那要模型、要重算）。它验证的是
    "这个库现在能被一个全新客户端完整读出来"—— 也就是 2026-10-13 失守的那一条。

    `path` 与 `store.open_collection` 同义（测试把它指到 `tmp_path`，永不碰 `data/chroma`）；
    `close_clients()` 关的是**所有**缓存客户端，因为 CLI 只有这一个库。
    """
    store_module.close_clients()

    collection = store_module.open_collection(path=path)  # 空集合 → CorpusMissing
    count = collection.count()
    if expected_chunks is not None and count != expected_chunks:
        raise StoreError(
            f"库里是 {count} 条，语料算出来是 {expected_chunks} 条 —— 写入没落全",
            details={"count": count, "expected": expected_chunks},
        )
    if count == 0:  # open_collection 已经拦了，这里是给 expected_chunks=None 时的兜底
        raise StoreError("集合是空的", details={"count": 0})

    # 深度探针住在 `rag/retriever.py` —— §2 把"读向量库"整体划给了它，
    # 在这里自己 `collection.query()` 会开出第二个读入口（守护 #24）。
    probe = retriever_module.probe_index_depth(collection)
    # 判据是 `is_readable`（带 1% 容差），**不是 `readable == requested`** ——
    # HNSW 是近似结构，向它索要全集本来就可能少一两条（实测 5 个进程里短 0、0、1、0、0）。
    # 直接比相等会让验收以约 20% 的概率误报，而事故的形态是短 16383 条。
    logger.info(
        "ingest.probe",
        declared=probe.declared,
        requested=probe.requested,
        readable=probe.readable,
        shortfall=probe.shortfall,
        allowed_shortfall=probe.allowed_shortfall,
    )
    if not probe.is_readable:
        raise StoreError(
            f"要 {probe.requested} 条检索结果，只回来 {probe.readable} 条 —— "
            f"索引残了（库里有 {probe.declared} 条，容差内允许少 {probe.allowed_shortfall} 条）",
            details={"count": probe.declared, "requested": probe.requested, "hits": probe.readable},
        )
    return count


def _dry_run(corpus_dir: Path, *, paths: Sequence[str] | None) -> int:
    """只读地把"将要写入 / 将要删除"数出来。"""
    documents = corpus_module.iter_corpus_documents(corpus_dir)
    if paths is not None:
        wanted = {path.replace("\\", "/") for path in paths}
        documents = [doc for doc in documents if _relative_posix(doc.path, corpus_dir) in wanted]

    chunks = [
        chunk
        for document in documents
        for chunk in build_chunks(corpus_module.parse_document(document))
    ]
    desired_ids = {chunk.chunk_id for chunk in chunks}
    _warn_oversize(chunks)

    # "库还没建"要在**构造客户端之前**判掉：`PersistentClient` 一构造就把库文件建出来，
    # 那样一次只读预演会留下 `data/chroma/chroma.sqlite3`（§4 区 + §5.5 的只读承诺）。
    # 下面的 `except CorpusMissing` 仍然有用：库在、但集合被删掉是另一种"没建好"。
    if not store_module.index_exists():
        stale_count = 0
    else:
        try:
            collection = store_module.open_collection()
        except CorpusMissing:
            stale_count = 0
        else:
            doc_urls = [document.doc_url for document in documents] if paths is not None else None
            where = _doc_url_filter(doc_urls)
            stale_count = len(_existing_ids(collection, where=where) - desired_ids)

    print(f"[dry-run] 文档 {len(documents)} 篇 / 将写入 {len(chunks)} 块 / 将删除 {stale_count} 块")
    return 0


def _print_report(report: IngestReport) -> None:
    print(
        f"文档 {report.documents} 篇 / chunk {report.chunks} / "
        f"写入 {report.upserted} / 删除 {report.deleted} / {report.duration_ms}ms"
    )


if __name__ == "__main__":
    sys.exit(main())
