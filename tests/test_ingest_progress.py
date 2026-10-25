"""`ingest.progress`：长任务的可观测性（SPEC §3.5 / §10 第 14 条）。

为什么这值得测：CPU 上全量 ingest 要跑 3~5 小时。没有进度行时，
"还在算"和"卡死在第十批"在终端里**完全同形** —— 人会去 kill 一个健康的任务，
或者对着一个死掉的任务等两小时。进度行就是这两种情况的唯一区分手段。

反面同样要测：不能每批一条。273 个批次刷 273 行会把 warn/error 顶出屏幕，
那就又变成没人看了。
"""

from __future__ import annotations

from chromadb.api.models.Collection import Collection
from structlog.testing import capture_logs

from fakes.embedder import HashingEmbedder
from pyassist.rag.ingest import _PROGRESS_REPORTS, Chunk, upsert_chunks


def _chunks(count: int) -> list[Chunk]:
    """`count` 条互不相同的假 chunk。`chunk_id` 必须唯一，否则 upsert 会互相覆盖。"""
    return [
        Chunk(
            chunk_id=f"c{i:05d}",
            body=f"body {i}",
            embed_text=f"Title\nSection\nbody {i}",
            doc_url="https://example.invalid/page.html",
            source_url=f"https://example.invalid/page.html#s{i}",
            title="Title",
            section="Section",
            ordinal=i,
        )
        for i in range(count)
    ]


def _progress(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == "ingest.progress"]


def test_progress_is_reported_in_bounded_steps(
    collection: Collection, hashing_embedder: HashingEmbedder
) -> None:
    """进度条数有上界（约 `_PROGRESS_REPORTS` 条），且最后一次必须**正好等于总量**。

    最后一条等于总量是判据的关键：它证明这一轮真的写完了，而不是"看起来跑完了"。
    """
    chunks = _chunks(200)

    with capture_logs() as logs:
        written = upsert_chunks(collection, chunks, hashing_embedder, batch_size=8)

    entries = _progress(logs)

    assert written == 200
    assert 1 <= len(entries) <= _PROGRESS_REPORTS + 1, f"进度条数失控：{len(entries)}"
    assert entries[-1]["upserted"] == 200
    assert entries[-1]["total"] == 200
    assert entries[-1]["percent"] == 100.0


def test_progress_counts_are_monotonic(
    collection: Collection, hashing_embedder: HashingEmbedder
) -> None:
    """计数只增不减，且每一条都带 `elapsed_ms` / `eta_ms`。

    没有 ETA 的进度百分比等于没说：99% 到底还剩 3 秒还是 40 分钟，只能靠它。
    """
    with capture_logs() as logs:
        upsert_chunks(collection, _chunks(150), hashing_embedder, batch_size=8)

    entries = _progress(logs)
    counts = [entry["upserted"] for entry in entries]

    assert counts == sorted(counts), "进度计数回退了"
    assert all(isinstance(entry["elapsed_ms"], int) for entry in entries)
    assert all(isinstance(entry["eta_ms"], int) for entry in entries)
    assert entries[-1]["eta_ms"] == 0, "跑完之后 ETA 必须是 0"


def test_small_corpus_reports_only_the_final_line(
    collection: Collection, hashing_embedder: HashingEmbedder
) -> None:
    """夹具语料那种规模（几块）只出最后一条 —— 小跑一次不该被进度行淹没。"""
    with capture_logs() as logs:
        upsert_chunks(collection, _chunks(7), hashing_embedder)

    entries = _progress(logs)

    assert len(entries) == 1
    assert entries[0]["upserted"] == 7
