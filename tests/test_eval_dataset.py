"""评估集 schema 守护（SPEC D11 / 判据 12）。

`qa.jsonl` 是 Recall@5 的 ground truth，与 `data/corpus/**` 同级的"事实来源"。
§4 规定它**只能新增、禁止修改**已有的用例 —— 为了提分改测试集是本项目最严重的作弊。

所以这里的断言分两类：

1. **形状**：schema 齐全、`id` 唯一、条数够 —— 防止有人在赶进度时把一个残缺的
   JSONL 提交进去，让评估在阶段六"跑得出数字但数字没意义"。
2. **可达性**：每条 `relevant_url` 都必须**指向语料里真实存在的页面**。
   这条才是真正有牙齿的：指向一页没被 ingest 的文档，Recall@5 永远不可能达标，
   而失败会归咎到检索器头上 —— 排查方向完全错了。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pyassist.config import (
    CORPUS_EXCLUDE_DIRS,
    CORPUS_EXCLUDE_FILES,
    DOCS_BASE_URL,
    PEP_NUMBERS,
    PEPS_BASE_URL,
    QA_DATASET_PATH,
)

REQUIRED_KEYS = {"id", "question", "relevant_urls", "expected_facts", "topic"}

EXPECTED_TOPICS = {
    "asyncio",
    "typing",
    "dataclasses",
    "pathlib",
    "json",
    "异常",
    "装饰器",
    "生成器",
}

MIN_RECORDS = 40


def _load() -> list[dict[str, Any]]:
    """按行解析 JSONL。**不用 `pytest.skip`**：文件缺失就是失败，不是"跳过"。"""
    path: Path = QA_DATASET_PATH
    assert path.exists(), f"评估集不存在：{path}"

    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - 失败时给出行号才可修
            raise AssertionError(f"{path} 第 {number} 行不是合法 JSON：{exc}") from exc
    return records


@pytest.fixture(scope="module")
def records() -> list[dict[str, Any]]:
    return _load()


def test_jsonl_parses_and_is_not_empty(records: list[dict[str, Any]]) -> None:
    assert records


def test_has_enough_records(records: list[dict[str, Any]]) -> None:
    """§5.2 要求评估集 ≥ 40 条。

    条数是**统计效力**问题：8 个主题共 40 条时，每条相当于 2.5 个百分点，
    再少下去 Recall@5 的 0.80 阈值就不再区分得开"好"和"碰巧"。
    """
    assert len(records) >= MIN_RECORDS, f"只有 {len(records)} 条，至少需要 {MIN_RECORDS} 条"


def test_ids_are_unique(records: list[dict[str, Any]]) -> None:
    ids = [record["id"] for record in records]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})

    assert not duplicates, f"重复的 id：{duplicates}"


def test_every_record_has_exactly_the_frozen_schema(records: list[dict[str, Any]]) -> None:
    """键集合**恰好相等**，不是"包含" —— 多出来的字段是无人消费的死重量，
    少一个则是评估跑不起来。schema 已冻结（D11），逐字对上最省事。"""
    for record in records:
        assert set(record) == REQUIRED_KEYS, f"{record.get('id')} 的字段是 {sorted(record)}"


def test_text_fields_are_non_empty_strings(records: list[dict[str, Any]]) -> None:
    for record in records:
        for key in ("id", "question", "topic"):
            value = record[key]
            assert isinstance(value, str), f"{record['id']} 的 {key} 不是字符串"
            assert value.strip(), f"{record['id']} 的 {key} 为空"


def test_expected_facts_are_non_empty(records: list[dict[str, Any]]) -> None:
    """`expected_facts` 供阶段六判"引用是否真的支撑结论"（D11）。

    空的 `expected_facts` 会让那条判据永远通过 —— 一个静默失效的检查点。
    """
    for record in records:
        facts = record["expected_facts"]
        assert isinstance(facts, list), f"{record['id']} 的 expected_facts 不是列表"
        assert facts, f"{record['id']} 的 expected_facts 为空"
        assert all(isinstance(fact, str) and fact.strip() for fact in facts), (
            f"{record['id']} 里有一条空的 expected_fact"
        )


def test_topics_cover_the_eight_frozen_themes(records: list[dict[str, Any]]) -> None:
    assert {record["topic"] for record in records} == EXPECTED_TOPICS


def test_relevant_urls_are_non_empty_and_unique(records: list[dict[str, Any]]) -> None:
    for record in records:
        urls = record["relevant_urls"]
        assert isinstance(urls, list), f"{record['id']} 的 relevant_urls 不是列表"
        assert urls, f"{record['id']} 没有 relevant_urls"
        assert len(urls) == len(set(urls)), f"{record['id']} 的 relevant_urls 有重复"


def test_relevant_urls_have_a_known_prefix(records: list[dict[str, Any]]) -> None:
    """D11：以 `DOCS_BASE_URL` 或 `PEPS_BASE_URL` 开头。"""
    for record in records:
        for url in record["relevant_urls"]:
            assert url.startswith(DOCS_BASE_URL) or url.startswith(PEPS_BASE_URL), (
                f"{record['id']} 的 URL 前缀不认识：{url}"
            )


def test_relevant_urls_are_page_level_without_fragments(records: list[dict[str, Any]]) -> None:
    """ground truth **按页面路径匹配、忽略锚点**（D11）。

    带 `#fragment` 的 URL 永远不等于 `RetrievedChunk.doc_url`（那个字段刻意不带锚点，D4），
    于是那条用例的 Recall 恒为 0 —— 而且看起来像检索器的问题。
    """
    for record in records:
        for url in record["relevant_urls"]:
            assert "#" not in url, f"{record['id']} 的 URL 带了锚点：{url}"


def test_docs_urls_point_at_pages_the_corpus_will_actually_hold(
    records: list[dict[str, Any]],
) -> None:
    """官方文档 URL 不能落在抓取时被排除的目录 / 文件名上（SPEC §2.5）。

    排除清单是 ingest 的行为，不是评估集的自由。指向 `_static/` 或 `genindex.html`
    等于给检索器出一道**不可能答对**的题。
    """
    for record in records:
        for url in record["relevant_urls"]:
            if not url.startswith(DOCS_BASE_URL):
                continue
            relative = url[len(DOCS_BASE_URL) :]
            parts = relative.split("/")
            name = parts[-1]

            assert not (set(parts[:-1]) & CORPUS_EXCLUDE_DIRS), (
                f"{record['id']} 指向被排除的目录：{url}"
            )
            assert name not in CORPUS_EXCLUDE_FILES, f"{record['id']} 指向被排除的文件：{url}"
            assert name.endswith(".html"), f"{record['id']} 指向的不是文档页：{url}"


def test_pep_urls_are_inside_the_ingested_pep_set(records: list[dict[str, Any]]) -> None:
    """PEP URL 的编号必须在 `PEP_NUMBERS` 里 —— 语料只抓这几个 PEP。

    这条把 `qa.jsonl` 与语料覆盖**绑在一起**：改了 `PEP_NUMBERS`（加抓或减抓）
    而忘了同步评估集，这里会立刻红。
    """
    allowed = {f"pep-{number:04d}/" for number in PEP_NUMBERS}

    for record in records:
        for url in record["relevant_urls"]:
            if not url.startswith(PEPS_BASE_URL):
                continue
            tail = url[len(PEPS_BASE_URL) :]
            assert tail in allowed, (
                f"{record['id']} 指向未抓取的 PEP：{url}（已抓：{sorted(allowed)}）"
            )


def test_question_ids_are_grouped_by_topic(records: list[dict[str, Any]]) -> None:
    """id 形如 `<topic>-<序号>`，便于按主题看分。

    不必强求 id 前缀等于 `topic`（中文主题名没法当 id 前缀），
    但要保证**每个主题下都是独立编号**，否则排查某一条失败用例时会找不到它。
    """
    per_topic: dict[str, list[str]] = {}
    for record in records:
        per_topic.setdefault(record["topic"], []).append(record["id"])

    for topic, ids in per_topic.items():
        assert len(ids) >= 5, f"主题 {topic} 只有 {len(ids)} 条，覆盖太薄"
        assert len(set(ids)) == len(ids), f"主题 {topic} 下的 id 有重复"
