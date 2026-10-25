"""复现 2026-10-13 的索引不可读事故：**进程退出时 HNSW 段没落盘**。

背景：一次 4.8 小时的全量 ingest 打印了 `ingest.done`，但重开集合报
`Error loading hnsw index`。落盘现场的证据是：段目录里只有 `index_metadata.pickle`
（`total_elements_added: 16384`），HNSW 的 `*.bin` 一个都没有；而 WAL 已经被清到
16384 之前 —— 也就是说前 16384 条向量只存在于那个已退出进程的内存里。

本脚本用**假嵌入**（`HashingEmbedder`，秒级）把同一个写入路径跑一遍，回答两个问题：

1. 小规模写入后**不 close 直接退出**，重开还能不能读？（能 → 事故与规模/时长相关，
   小规模测不出；不能 → 这是每条 ingest 都会踩的确定性 bug）
2. 退出前 `client.close()` 能不能救回来？（这就是候选修复）

用法：

    uv run python experiments/repro_index_durability.py write --dir /tmp/rc --chunks 50   [--close]
    uv run python experiments/repro_index_durability.py read  --dir /tmp/rc

两个子命令**必须分开进程跑** —— 同一个进程里读得通不代表落盘了（句柄还在内存里活着）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

from fakes.embedder import HashingEmbedder
from pyassist.rag import store as store_module
from pyassist.rag.ingest import Chunk, upsert_chunks


def _chunks(count: int) -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"chunk{i:05d}",
            body=f"body {i}",
            embed_text=f"Title\nSection\nbody {i}",
            doc_url="https://example.invalid/doc.html",
            source_url=f"https://example.invalid/doc.html#s{i}",
            title="Title",
            section="Section",
            ordinal=i,
        )
        for i in range(count)
    ]


def _write(
    path: Path, count: int, *, close: bool, sync_threshold: int | None, hard_exit: bool
) -> int:
    embedder = HashingEmbedder()
    hnsw: dict[str, object] = {"space": "cosine"}
    if sync_threshold is not None:
        # batch_size 必须 ≤ sync_threshold（Chroma 会校验），这里固定 100 = 上游默认。
        hnsw |= {"sync_threshold": sync_threshold, "batch_size": 100}
    collection = store_module.get_client(path).get_or_create_collection(
        name=store_module.COLLECTION_NAME,
        embedding_function=None,
        configuration={"hnsw": hnsw},
        metadata=store_module.build_index_metadata(
            model_id=embedder.model_id, dimension=embedder.dimension
        ),
    )
    print("集合 hnsw 配置:", collection.configuration_json)
    written = upsert_chunks(collection, _chunks(count), embedder, batch_size=16)
    print(f"写入 {written} 条（close={close}）")

    if close:
        store_module.get_client(path).close()
        print("已 close()")

    if hard_exit:
        # os._exit 不走解释器收尾，Rust 的 compactor 线程直接被掐断 —— 等价于崩溃。
        sys.stdout.flush()
        os._exit(0)

    # 刻意不做任何收尾：模拟 `sys.exit(main())`。
    print("退出")
    return 0


def _read(path: Path) -> int:
    client = store_module.get_client(path)
    try:
        collection = client.get_collection(name=store_module.COLLECTION_NAME)
    except Exception as exc:  # —— 这是复现脚本，要看到真实异常类型
        print(f"✗ 打不开集合：{type(exc).__name__}: {exc}")
        return 2

    try:
        print(f"count() = {collection.count()}")
    except Exception as exc:  # 实验脚本：把失败原因打出来就行
        print(f"✗ count() 失败：{type(exc).__name__}: {exc}")
        return 3

    # 多要一点：索引"少了一截"这件事，n_results=3 是看不出来的（3 条命中可能来自
    # 一条只装了几十条的残索引，也可能来自完整的 20000 条）。要它 1000 条，
    # 返回多少就是索引里真正有多少。
    wanted = min(1000, collection.count())
    try:
        hits = collection.query(query_embeddings=[[1.0] * 64], n_results=wanted)
        print(f"要 {wanted} 条，命中 {len(hits['ids'][0])} 条")
    except Exception as exc:  # 实验脚本：把失败原因打出来就行
        print(f"✗ 查询失败：{type(exc).__name__}: {exc}")
        return 4
    return 0


def _wal(path: Path) -> int:
    """看落盘现场：WAL 还剩几条（带不带向量）、段目录里有哪些文件。

    "WAL 里还有向量" 是整个恢复策略的前提 —— 只要它在，索引坏了也能重建，
    不必重新嵌入。所以这个子命令是判断"要不要重跑 4.8 小时"的唯一依据。
    """
    import sqlite3

    db = path / "chroma.sqlite3"
    if not db.is_file():
        print(f"✗ 没有 {db}")
        return 2
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    rows = con.execute("select count(*) from embeddings_queue").fetchone()[0]
    with_vector = con.execute(
        "select count(*) from embeddings_queue where vector is not null and length(vector) > 0"
    ).fetchone()[0]
    print(f"WAL: {rows} 条，其中带向量的 {with_vector} 条")
    low, high = con.execute("select min(seq_id), max(seq_id) from embeddings_queue").fetchone()
    print(f"WAL seq 区间: {low}..{high}")
    print("各段 max_seq_id:", con.execute("select * from max_seq_id").fetchall())

    for segment in sorted(path.iterdir()):
        if segment.is_dir():
            files = sorted(p.name for p in segment.iterdir())
            print(f"段目录 {segment.name}: {files}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HNSW 段落盘复现")
    sub = parser.add_subparsers(dest="mode", required=True)
    write = sub.add_parser("write")
    write.add_argument("--dir", type=Path, required=True)
    write.add_argument("--chunks", type=int, default=50)
    write.add_argument("--close", action="store_true", help="退出前显式 close 客户端")
    write.add_argument("--sync-threshold", type=int, default=None, help="hnsw.sync_threshold")
    write.add_argument(
        "--hard-exit",
        action="store_true",
        help="用 os._exit(0) 退出，模拟崩溃（任何 Rust 线程都来不及收尾）",
    )
    write.add_argument(
        "--starve",
        type=int,
        default=0,
        metavar="N",
        help="开 N 个烧 CPU 的线程模拟 torch 占满核，把 compactor 的落盘饿死",
    )
    read = sub.add_parser("read")
    read.add_argument("--dir", type=Path, required=True)
    wal = sub.add_parser("wal")
    wal.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.mode == "wal":
        return _wal(args.dir)

    if args.mode == "write":
        if args.starve:
            _start_starvation(args.starve)
        return _write(
            args.dir,
            args.chunks,
            close=args.close,
            sync_threshold=args.sync_threshold,
            hard_exit=args.hard_exit,
        )
    return _read(args.dir)


_WORK = 0


def _start_starvation(threads: int) -> None:
    """开 `threads` 个死循环线程抢 GIL/CPU。

    GIL 会让"算术死循环"也间接拖慢 Rust 侧的线程吗？不一定 —— Rust 的 compactor
    跑在不持 GIL 的原生线程上，所以这里抢的是**真正的 CPU 核数**，正是 torch 在真实
    事故里干的事（4.8 小时满载）。
    """
    import threading

    global _WORK

    def burn() -> None:
        global _WORK
        while True:
            _WORK = (_WORK * 1103515245 + 12345) % 2**31

    for _ in range(threads):
        threading.Thread(target=burn, daemon=True).start()
    print(f"已开启 {threads} 个饿死线程")


if __name__ == "__main__":
    raise SystemExit(main())
