"""实测分批策略省下多少填充，以及嵌入吞吐（SPEC §10 第 14 条的对照实验）。

2026-10-13 那次全量 ingest 用了 **5.96 小时**（4.08M 真实 token）。
其中很大一块是**纯浪费**：嵌入器把一批 padding 到批内最长的那条，
而真实语料是"大量短正文 + 少数长代码块"，于是每批里的长块都把同批其余七条一起撑大。

**本脚本量的是 `ingest._length_bucketed` 这个已上线的函数**，不是它的一份副本。
原先这里有一份独立的升序实现，那意味着"实测结论"与"实际行为"可以悄悄分叉 ——
现在两者是同一个函数，改了就一起变。长度也走 `embedder.count_tokens`（同理）。

跑三行对照，第三行是**已被推翻的那一版**，留着是为了让"为什么必须是 token 不是字符"
有据可查 —— 它曾以"省 42%"的名义上线，实测只有 20.1%。

默认只跑 **tokenizer**（不做前向计算），因为它量的那个量（padding 前后的 token 比）
不需要嵌入，于是可以在**全量**上跑完。要顺带量墙钟时间才加 `--chunks N`。

⚠️ **要带 `HF_HUB_OFFLINE=1` 跑。** 模型（4.3G）早就在本地缓存里，但
`sentence-transformers` 构造时会去 `huggingface.co` 问一次版本；网络不通时它
**先干等几分钟、再抛 `ConnectTimeout`**，看起来像"脚本卡死了"。当前结果：

    文档顺序      4,082,001 / 7,127,044（1.75×）
    token 分桶    4,082,001 / 4,115,980（1.01×）  省 42.2%   ← 现行为
    字符分桶      4,082,001 / 5,691,408（1.39×）  省 20.1%   ← 已废弃

用法：

    HF_HUB_OFFLINE=1 uv run python experiments/bench_embed_speed.py            # 全量，只算 token
    HF_HUB_OFFLINE=1 uv run python experiments/bench_embed_speed.py --chunks 64
    HF_HUB_OFFLINE=1 uv run python experiments/bench_embed_speed.py --batch 128
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterator, Sequence
from itertools import batched
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from pyassist.config import CORPUS_DIR, DEFAULT_EMBEDDING_MODEL_ID, EMBEDDING_MAX_SEQ_TOKENS
from pyassist.rag import corpus as corpus_module
from pyassist.rag.embedder import get_embedder
from pyassist.rag.ingest import Chunk, _length_bucketed, build_chunks


def _doc_order(chunks: Sequence[Chunk], batch_size: int) -> Iterator[tuple[Chunk, ...]]:
    """**优化前的行为**，作为对照组。没有对照组的"省了"证明不了任何事。"""
    return batched(chunks, batch_size)


def _char_bucketed(chunks: Sequence[Chunk], batch_size: int) -> Iterator[tuple[Chunk, ...]]:
    """2026-10-13 上过线又被推翻的那一版：按 `len(embed_text)` 分桶。

    留在对照表里，因为"字符序 ≈ token 序"是个**看起来显然、实际不成立**的假设。
    把它的真实成绩钉在这儿，下次再有人想省掉那 3 秒分词，先看这一行。
    """
    ordered = sorted(chunks, key=lambda chunk: len(chunk.embed_text), reverse=True)
    return batched(ordered, batch_size)


def _token_counts(batches: Iterator[tuple[Chunk, ...]], tokenizer: Any) -> tuple[int, int]:
    """返回 (真实 token, padding 后 token)。

    `padding=True` 让 tokenizer 把整批补齐到批内最长 —— 这正是被计入前向计算的量；
    拿掉 `padding` 就是这批真正需要的量。两者的差就是纯粹的浪费。

    **`max_length` 显式传 `EMBEDDING_MAX_SEQ_TOKENS`**：`truncation=True` 单独给是不够的，
    它会截到模型自带的 8192，而真正跑前向时用的是嵌入器设的 1024（内存闸门）。
    不传就会量到一个比现实更宽松的数。
    """
    real = 0
    padded = 0
    for batch in batches:
        texts = [chunk.embed_text for chunk in batch]
        padded += sum(
            len(ids)
            for ids in tokenizer(
                texts,
                add_special_tokens=True,
                truncation=True,
                max_length=EMBEDDING_MAX_SEQ_TOKENS,
                padding=True,
            )["input_ids"]
        )
        real += sum(
            len(ids)
            for ids in tokenizer(
                texts,
                add_special_tokens=True,
                truncation=True,
                max_length=EMBEDDING_MAX_SEQ_TOKENS,
            )["input_ids"]
        )
    return real, padded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分批策略的填充开销与嵌入吞吐实测")
    parser.add_argument("--batch", type=int, default=64, help="批大小（默认 = _UPSERT_BATCH_SIZE）")
    parser.add_argument(
        "--chunks", type=int, default=0, help="额外实测这么多条的墙钟时间（0 = 跳过，只算 token）"
    )
    args = parser.parse_args(argv)

    import torch

    print(f"torch {torch.__version__} / get_num_threads() = {torch.get_num_threads()}")

    chunks: list[Chunk] = []
    for document in corpus_module.iter_corpus_documents(CORPUS_DIR):
        chunks.extend(build_chunks(corpus_module.parse_document(document)))
    print(f"全量 {len(chunks)} 条 chunk，batch={args.batch}")

    embedder = get_embedder(DEFAULT_EMBEDDING_MODEL_ID)
    tokenizer = embedder._model.tokenizer  # 实验脚本，允许摸内部（不走 Embedder 的公开面）

    # 长度来自**生产路径**，不是这里另算一份。全量扫一遍几秒钟，打印出来是为了
    # 让人知道那 42% 的成本是几秒换来的。
    started = time.perf_counter()
    lengths = embedder.count_tokens([chunk.embed_text for chunk in chunks])
    print(f"count_tokens 扫完 {len(chunks)} 条用了 {time.perf_counter() - started:.1f}s")

    strategies: tuple[tuple[str, Iterator[tuple[Chunk, ...]]], ...] = (
        ("① 文档顺序", _doc_order(chunks, args.batch)),
        ("② token 分桶（现行为）", _length_bucketed(chunks, args.batch, lengths)),
        ("③ 字符分桶（已废弃）", _char_bucketed(chunks, args.batch)),
    )

    results: dict[str, tuple[int, int]] = {}
    for label, batches in strategies:
        started = time.perf_counter()
        real, padded = _token_counts(batches, tokenizer)
        results[label] = (real, padded)
        print(
            f"{label:22s} 真实 {real:>9,} / padding 后 {padded:>9,}"
            f"（{padded / max(real, 1):.2f}×）  扫描 {time.perf_counter() - started:.0f}s"
        )

    baseline = results["① 文档顺序"][1]
    for label in ("② token 分桶（现行为）", "③ 字符分桶（已废弃）"):
        padded = results[label][1]
        print(f"\n{label} 相对文档顺序省掉 {(1 - padded / baseline) * 100:.1f}%")

    if args.chunks:
        sample = chunks[: args.chunks]
        sample_lengths = lengths[: args.chunks]
        for label, batches in (
            ("① 文档顺序", _doc_order(sample, args.batch)),
            ("② token 分桶（现行为）", _length_bucketed(sample, args.batch, sample_lengths)),
        ):
            started = time.perf_counter()
            for batch in batches:
                embedder.embed_documents([chunk.embed_text for chunk in batch])
            elapsed = time.perf_counter() - started
            rate = len(sample) / elapsed
            print(f"{label:22s} 嵌入 {len(sample)} 条 {elapsed:.1f}s → {rate:.2f} chunk/s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
