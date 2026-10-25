"""实测嵌入期的峰值内存，验证 `EMBEDDING_MAX_SEQ_TOKENS` + batch 这两个数够不够。

用法（**注意：会真下载 2.27GB 的 bge-m3 权重**，且是 CPU 推理，慢）：

    uv run python experiments/verify_embedder_memory.py [--chunks 256] [--batch 64]

它回答的是 2026-10-13 那次 OOM 之后留下的问题：
"把 `max_seq_length` 从 8192 压到 1024、batch 从 256 压到 64，峰值到底是多少？"

判据三条，任一不成立就说明那两个数得重算：
1. `max_seq_length` 确实是配置里的值（没被模型自带的值覆盖回去）；
2. **截断条数为 0** —— 闸门只该挡事故，不该挡正常 chunk；
3. 峰值 RSS 与本机可用内存之间有可见余量。

这些都是**机器相关**的实测值而不是断言，所以放 `experiments/` 而不是 `tests/`
（§3.5：只有这里允许 `print`）。换模型或改分块后重跑一次，把数字更新到本文件顶部。

2026-10-13 本机（16GiB RAM）实测：分块修复后全量 17366 chunk 的 token 分布
p50=254 / p90=336 / max=860；读取 `data/corpus` 全量、batch=64 跑完不 OOM。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyassist.config import CORPUS_DIR, EMBEDDING_MAX_SEQ_TOKENS
from pyassist.rag import corpus as corpus_module
from pyassist.rag.embedder import (
    SentenceTransformerEmbedder,
    count_truncated,
)
from pyassist.rag.ingest import build_chunks


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def peak_rss_mb() -> float:
    """本进程峰值 RSS（MiB）。两个平台都要，因为开发在 Windows、部署在 Linux。

    ⚠️ Windows 上**必须声明 argtypes/restype**：不声明时 `GetCurrentProcess()` 的
    伪句柄 `-1` 会被 ctypes 截成 32 位，调用直接失败并返回 0 —— 而读数会安静地印出
    "峰值 RSS 0 MiB"。一个撒谎的度量比没有度量更坏，所以这个坑写在这里。
    """
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        ok = kernel32.K32GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "K32GetProcessMemoryInfo 失败")
        return counters.PeakWorkingSetSize / 2**20

    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux 给 KiB，macOS 给字节 —— 混为一谈会差 1024 倍。
    return usage / 2**10 if sys.platform.startswith("linux") else usage / 2**20


def available_ram_mb() -> float:
    """本机当前可用物理内存（MiB）。判断"峰值是否留有可见余量"需要它。"""
    if sys.platform != "win32":
        return float("nan")  # 实测脚本的目标机器是 Windows；Linux 上按 `/proc/meminfo` 读

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullAvailPhys / 2**20


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="嵌入峰值内存实测")
    parser.add_argument("--chunks", type=int, default=256, help="取多少条 chunk 来嵌入")
    parser.add_argument("--batch", type=int, default=64, help="每批条数（对齐 ingest 的默认值）")
    parser.add_argument("--model-id", default=None)
    args = parser.parse_args(argv)

    from pyassist.config import DEFAULT_EMBEDDING_MODEL_ID

    model_id = args.model_id or DEFAULT_EMBEDDING_MODEL_ID
    print(f"模型 {model_id} / 期望 max_seq_length={EMBEDDING_MAX_SEQ_TOKENS} / batch={args.batch}")

    # 先把全量 chunk 建出来（纯 CPU、秒级）：截断统计要覆盖**全部** chunk，
    # 只看抽样的话，"有 3 条超长"这种事正好会落在抽样之外。
    all_texts: list[str] = []
    for document in corpus_module.iter_corpus_documents(CORPUS_DIR):
        all_texts.extend(
            chunk.embed_text for chunk in build_chunks(corpus_module.parse_document(document))
        )
    print(f"全量 chunk {len(all_texts)} 条，最长正文 {max(len(t) for t in all_texts)} 字符")

    started = time.perf_counter()
    embedder = SentenceTransformerEmbedder(model_id)
    print(f"加载完成 {time.perf_counter() - started:.1f}s，峰值 RSS {peak_rss_mb():.0f} MiB")

    actual = embedder._model.max_seq_length
    print(f"实际 max_seq_length = {actual}")
    if actual != EMBEDDING_MAX_SEQ_TOKENS:
        print("✗ 闸门没生效：模型自带的值覆盖了配置")
        return 1

    truncated = count_truncated(embedder._model.tokenizer, all_texts, EMBEDDING_MAX_SEQ_TOKENS)
    print(f"会被截断的条数 = {truncated} / {len(all_texts)}")
    if truncated:
        print(f"✗ 有 {truncated} 条超上限 —— 闸门开始挡正常数据了，该调大 EMBEDDING_MAX_SEQ_TOKENS")

    # **取最长的那些来嵌入**：padding 到批内最长，所以峰值由"最长的那批"决定，
    # 拿前 N 条来测只会测出 300 token 的轻松局面，测不到闸门该挡的那个场景。
    texts = sorted(all_texts, key=len, reverse=True)[: args.chunks]
    print(f"取最长 {len(texts)} 条（{len(texts[0])} 字符）嵌入，batch={args.batch}")

    started = time.perf_counter()
    for start in range(0, len(texts), args.batch):
        embedder.embed_documents(texts[start : start + args.batch])
    elapsed = time.perf_counter() - started
    rate = len(texts) / elapsed
    peak = peak_rss_mb()
    print(f"嵌入完成 {elapsed:.1f}s（{rate:.1f} chunk/s）")
    print(f"峰值 RSS {peak:.0f} MiB；本机可用内存 {available_ram_mb():.0f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
