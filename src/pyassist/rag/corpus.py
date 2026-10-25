"""★ 唯一语料抓取与解析（§2 单点所有权，2026-10-13 新增，见 SPEC §5.12）。

**本模块是纯分层的**：不得 import `chromadb` / `rag.embedder` / `rag.store` / `rag.ingest`
（守护 #21 / #26 拦这个）。理由不是洁癖 —— 而是 `parse_document` 因此成为**纯函数**
（入参是已读好的 HTML 字符串，不出网），这是 §5.1「测试不依赖真实 LLM 或网络」能成立的关键。

职责分三段，边界各不同：

| 函数 | 出网 | 读盘 | 可离线单测 |
|---|---|---|---|
| `parse_document` | 否 | 否 | ✅ 纯函数 |
| `iter_corpus_documents` | 否 | 是 | ✅ 夹具目录即可 |
| `fetch_official_docs` / `fetch_peps` | **是** | 是 | ❌ 靠 `respx` mock |

两个 URL 字段的分工（SPEC D4），**不要合并**：
- `doc_url` 是**页面** URL（无锚点）。用于按页归组、`read_doc`、以及 `qa.jsonl` 的
  `relevant_urls` 匹配（同一页会派生成百上千个带不同锚点的 chunk，只有页面级才可比）。
- `source_url` 是**带锚点**的可点击 URL，用于引用。建块时会被锚点改写。
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from pyassist.config import (
    CORPUS_EXCLUDE_DIRS,
    CORPUS_EXCLUDE_FILES,
    CORPUS_FETCH_TIMEOUT_SECONDS,
    DOCS_ARCHIVE_URL_FALLBACK,
    DOCS_BASE_URL,
    DOCS_DOWNLOAD_PAGE_URL,
    PEP_INDEX_URL,
    PEP_NUMBERS,
    PEPS_BASE_URL,
)
from pyassist.logging import get_logger

logger = get_logger(__name__)

# 只认这两个前缀，其余目录整个跳过（SPEC §2.5）。跳过不是报错 ——
# `data/corpus/` 里放人格测试文档或被编辑器留下的临时文件都不该让 ingest 崩。
_DOCS_PREFIX = "docs"
_PEPS_PREFIX = "peps"

_HEADING_TAGS = ("h1", "h2", "h3")
_CONTENT_TAGS = ("p", "li", "dt", "dd", "pre")
_BLOCK_TAGS = _HEADING_TAGS + _CONTENT_TAGS

# `<title>` 的尾巴，如 " — Python 3.12.7 documentation"。版本号会变，所以是模式不是字面量。
_TITLE_SUFFIX_RE = re.compile(r"\s*[—–-]\s*Python\s+[\d.]+[a-z]*\s+documentation\s*$")

_SECTION_SEPARATOR = " › "


@dataclass(frozen=True)
class CorpusDocument:
    """一篇已读入内存的语料。`html` 是原文，解析交给 `parse_document`（纯函数）。"""

    path: Path
    doc_url: str
    source_url: str  # 建块后会被锚点改写；此处 == doc_url
    html: str


@dataclass(frozen=True)
class Block:
    """解析出的最小可索引单位。`kind` 区分正文与代码块 —— ingest 对二者的处理不同。"""

    text: str
    section: str | None
    anchor: str | None
    kind: Literal["para", "code"]


@dataclass(frozen=True)
class ParsedDocument:
    doc_url: str
    title: str
    blocks: list[Block]


@dataclass(frozen=True)
class FetchReport:
    documents: int
    bytes_written: int
    source_url: str


# ---------- 扫描与 URL 映射 ----------


def iter_corpus_documents(corpus_dir: Path) -> list[CorpusDocument]:
    """扫 `corpus_dir/docs/**/*.html` 与 `corpus_dir/peps/**/*.html`。

    返回 `list` 而非 `Iterator` 是**刻意的**：确定性是幂等的前提之一（SPEC D9）——
    ingest 按这个顺序生成 desired 集合，集合本身无序，但日志与"变更全序"要有序。
    按 repo 相对 posix 路径排序，跨平台稳定（不随 `\\` 与 `/` 的差异漂移）。
    """
    found: list[tuple[str, Path]] = []
    for prefix in (_DOCS_PREFIX, _PEPS_PREFIX):
        base = corpus_dir / prefix
        if not base.is_dir():
            continue
        for path in base.rglob("*.html"):
            relative = path.relative_to(corpus_dir).as_posix()
            if _is_excluded(relative):
                continue
            found.append((relative, path))

    found.sort(key=lambda item: item[0])
    return [
        CorpusDocument(
            path=path,
            doc_url=_doc_url(relative),
            source_url=_doc_url(relative),
            html=path.read_text(encoding="utf-8", errors="replace"),
        )
        for relative, path in found
    ]


def _is_excluded(relative_posix: str) -> bool:
    """排除清单在 `config.CORPUS_EXCLUDE_*` 各一处（SPEC §2.5）。"""
    parts = PurePosixPath(relative_posix).parts
    if set(parts) & CORPUS_EXCLUDE_DIRS:
        return True
    return parts[-1] in CORPUS_EXCLUDE_FILES


def _doc_url(relative_posix: str) -> str:
    """落盘相对路径 → **页面** URL（无锚点）。映射表见 SPEC §2.5。

    | 落盘相对路径 | `doc_url` |
    |---|---|
    | `docs/library/asyncio-task.html` | `https://docs.python.org/3.12/library/asyncio-task.html` |
    | `peps/pep-0008.html` | `https://peps.python.org/pep-0008/` |

    注意 PEP 的差别：落盘带 `.html`，而**上游 URL 没有**后缀（`peps.python.org/pep-0008/`）。
    按落盘路径拼 URL 会得到 404 的链接 —— 这正是这层映射非存在不可的原因，
    也是 `qa.jsonl` 的 `relevant_urls` 能与检索结果直接比对的前提（SPEC D11）。
    """
    path = PurePosixPath(relative_posix)
    prefix, *rest = path.parts
    tail = PurePosixPath(*rest).as_posix()
    if prefix == _PEPS_PREFIX:
        return f"{PEPS_BASE_URL}{tail.removesuffix('.html')}/"
    return f"{DOCS_BASE_URL}{tail}"


# ---------- 解析（纯函数）----------


def parse_document(document: CorpusDocument) -> ParsedDocument:
    """HTML 字符串 → 标题 + 有序块（带 section / anchor）。**纯函数，不出网。**

    分块不按字符数硬切（SPEC D5）：纯字符切会把 `<pre>` 代码块、`<dt>/<dd>` 定义
    从签名处劈开，直接压低 Recall@5 与引用准确率。这里按 HTML 结构产出**语义完整的块**，
    字符级聚合留给 `rag/ingest.py`。

    必须剥掉的东西：
    - `.headerlink`（Sphinx 的 ¶ 锚符号）—— 不剥的话每个块尾巴都挂一个 `¶`；
    - `script` / `style` / `nav` —— 它们会被 `get_text()` 混进正文。
    """
    soup = BeautifulSoup(document.html, "html.parser")
    for unwanted in soup(["script", "style", "nav"]):
        unwanted.decompose()
    for headerlink in soup.select("a.headerlink"):
        headerlink.decompose()

    body = _document_body(soup)
    title = _document_title(soup, fallback=_heading_text(body))

    return ParsedDocument(
        doc_url=document.doc_url,
        title=title,
        blocks=_extract_blocks(body),
    )


def _document_title(soup: Any, *, fallback: str) -> str:
    """取 `<title>` 并剥掉 " — Python 3.12.7 documentation" 尾巴（SPEC §5.4）。"""
    raw = _fold(getattr(soup.find("title"), "get_text", lambda: "")())
    stripped = _TITLE_SUFFIX_RE.sub("", raw).strip()
    return stripped or fallback


def _heading_text(body: Any) -> str:
    heading = body.find(_HEADING_TAGS[0]) if body is not None else None
    return _fold(getattr(heading, "get_text", lambda: "")())


def _document_body(soup: Any) -> Any:
    """文档容器：`div.body`（Python 官方文档）→ `article` → `body` → 整个 soup。

    这条降级链抗的是"上游换了主题"：Python 官方文档用 Sphinx 的 `div.body`，
    但 PEP 页面、或第三方镜像站的模板各不相同。拿不到理想容器时退到更宽的容器，
    而不是让整页解析失败 —— 解析出噪块也比一篇都进不了库好。
    """
    candidate = soup.find("div", class_="body")
    if candidate is None:
        candidate = soup.find("article")
    if candidate is None:
        candidate = soup.find("body")
    return soup if candidate is None else candidate


def _extract_blocks(body: Any) -> list[Block]:
    """按文档顺序走一遍块级元素，维护标题栈，产出 `Block` 列表。

    标题本身**不产出块** —— 它只更新 `(level, id, text)` 栈，成为后续块的 section 上下文。
    这避免了"每个标题单独成一个内容单薄的 chunk"。

    嵌套判定：`<li><p>…</p></li>` 这类结构里，`li` 与 `p` 都会被 `find_all` 命中，
    不去重就会产出两条近似重复的块。规则是**只取最外层** —— 任一块级祖先在 body 之内则跳过。
    """
    stack: list[tuple[int, str | None, str]] = []
    blocks: list[Block] = []

    if body is None:
        return blocks

    for tag in body.find_all(_BLOCK_TAGS):
        name = _tag_name(tag)
        if name in _HEADING_TAGS:
            level = _HEADING_TAGS.index(name) + 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, _attr_str(tag, "id"), _fold(tag.get_text(" "))))
            continue

        if _has_block_ancestor(tag, body):
            continue

        text = _code_text(tag) if name == "pre" else _fold(tag.get_text(" "))
        if not text:
            continue

        anchor = stack[-1][1] if stack else None
        section = _SECTION_SEPARATOR.join(item[2] for item in stack) if stack else None
        blocks.append(
            Block(
                text=text,
                section=section,
                anchor=anchor,
                kind="code" if name == "pre" else "para",
            )
        )

    return blocks


def _has_block_ancestor(tag: Any, body: Any) -> bool:
    parent = getattr(tag, "parent", None)
    while parent is not None and parent is not body:
        if _tag_name(parent) in _BLOCK_TAGS:
            return True
        parent = getattr(parent, "parent", None)
    return False


def _tag_name(tag: Any) -> str:
    name = getattr(tag, "name", None)
    return name if isinstance(name, str) else ""


def _attr_str(tag: Any, key: str) -> str | None:
    value = getattr(tag, "attrs", {}).get(key)
    if isinstance(value, str):
        return value or None
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return None


def _fold(text: Any) -> str:
    """折叠空白。正文用 —— **代码块不能用**（见 `_code_text`）。"""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _code_text(tag: Any) -> str:
    """代码块取原文，**只清行尾空白与首尾空行**。

    折叠空白会把缩进这个 Python 语义的一部分抹掉 —— 一段 `def` 缩进没了，
    检索能命中，引用却给出错误代码。所以代码与正文走两条不同的清洗路径。
    """
    raw = tag.get_text()
    if not isinstance(raw, str):
        return ""
    lines = [line.rstrip() for line in raw.splitlines()]
    return "\n".join(lines).strip("\n")


# ---------- 抓取 ----------


def resolve_docs_archive_url(client: httpx.Client) -> str:
    """两步解析（SPEC §2.4 **已核实**）：先读 `download.html` 抽 zip 链接，再回退常量。

    抗的是两件事：**URL 模式记错**（常量写错）与**上游改版**（归档页结构调整）。
    只写常量则前者必炸；只抓页面则后者会静默改道。
    """
    try:
        response = client.get(DOCS_DOWNLOAD_PAGE_URL, timeout=CORPUS_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("corpus.download_page_unreachable", error=str(exc))
        return DOCS_ARCHIVE_URL_FALLBACK

    soup = BeautifulSoup(response.text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = _attr_str(anchor, "href")
        if href is not None and href.endswith("docs-html.zip"):
            return urljoin(DOCS_DOWNLOAD_PAGE_URL, href)

    logger.warning("corpus.archive_link_not_found", page=DOCS_DOWNLOAD_PAGE_URL)
    return DOCS_ARCHIVE_URL_FALLBACK


def fetch_official_docs(target_dir: Path) -> FetchReport:
    """下载官方文档 HTML 归档并镜像进 `target_dir/docs/`。

    **手工逐个成员解压，不用 `extractall`** —— 那是 zip-slip 的标准入口
    （恶意 / 被篡改的归档里一个 `../../` 就能写到 `data/corpus` 之外）。
    逐个校验：拒绝绝对路径、拒绝 `..` 分量、拒绝符号链接，最后再断言目标落在
    `target_dir` 之内。这不是 lint 要求，是安全要求（SPEC D4）。

    **同名覆盖，不删目录**：§4 只允许抓取流程写 `data/corpus/**`，
    不允许它顺手清空用户手工放进去的东西。
    """
    with httpx.Client(timeout=CORPUS_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
        archive_url = resolve_docs_archive_url(client)
        response = client.get(archive_url)
        response.raise_for_status()
        payload = response.content

    docs_dir = target_dir / _DOCS_PREFIX
    docs_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as staging_name:
        staging = Path(staging_name)
        written, extracted_root = _extract_docs_archive(payload, staging)
        source_root = extracted_root or staging
        _mirror_into(source_root, docs_dir)

    logger.info(
        "corpus.fetched.official_docs",
        documents=written,
        bytes_written=len(payload),
        source_url=archive_url,
    )
    return FetchReport(documents=written, bytes_written=len(payload), source_url=archive_url)


def _extract_docs_archive(payload: bytes, staging: Path) -> tuple[int, Path | None]:
    """解压到 staging，返回（写入文件数，归档的单一顶层目录）。

    只解 `.html`，且跳过排除目录 —— `_static/` 里是图片与 CSS，
    解出来几十 MB 却一页都不索引。**代码层过滤与 `iter_corpus_documents` 的过滤
    用同一个 `_is_excluded()`**，两处规则不会漂移。
    """
    archive = staging / "docs.zip"
    archive.write_bytes(payload)

    written = 0
    roots: set[str] = set()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            relative = _safe_archive_member(info, staging)
            if relative is None:
                continue
            roots.add(relative.parts[0])
            if not relative.name.endswith(".html") or _is_excluded(relative.as_posix()):
                continue
            destination = staging / "extracted" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(zf.read(info))
            written += 1

    extracted = staging / "extracted"
    if roots and len(roots) == 1:
        single = extracted / next(iter(roots))
        if single.is_dir():
            return written, single
    return written, extracted if extracted.is_dir() else None


def _safe_archive_member(info: zipfile.ZipInfo, staging: Path) -> PurePosixPath | None:
    """校验一个 zip 成员是否可安全落盘。不安全 → `None`（跳过并记一笔）。

    四道闸（SPEC D4）：绝对路径 / `..` 分量 / 符号链接 / 目标逃逸出 staging。
    目录成员（以 `/` 结尾）返回 `None`：它们的父目录由写文件时的 `mkdir` 负责。
    """
    name = info.filename.replace("\\", "/")
    if name.endswith("/"):
        return None

    relative = PurePosixPath(name)
    unix_mode = info.external_attr >> 16
    if (
        relative.is_absolute()
        or any(part == ".." for part in relative.parts)
        or stat.S_ISLNK(unix_mode)
    ):
        logger.warning("corpus.archive_member_rejected", member=name)
        return None

    resolved = (staging / "extracted" / Path(*relative.parts)).resolve()
    if not resolved.is_relative_to(staging.resolve()):
        logger.warning("corpus.archive_member_escapes", member=name)
        return None
    return relative


def _mirror_into(source_root: Path, docs_dir: Path) -> None:
    """把解压出的顶层目录**内容**移进 `docs/`，同名覆盖。"""
    if not source_root.is_dir():
        return
    for child in sorted(source_root.iterdir()):
        destination = docs_dir / child.name
        if child.is_dir():
            shutil.copytree(child, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(child, destination)


def fetch_peps(target_dir: Path) -> FetchReport:
    """抓固定清单的 PEP 正文（`config.PEP_NUMBERS`）落成 `target_dir/peps/pep-XXXX.html`。

    先取 `peps.json` 索引**校验清单里的 PEP 确实存在**：数量不符只 `error` 记日志、
    不中断 —— 上游删了一个冷门 PEP 不该让整个 ingest 停摆，但这事必须留痕。
    正文只抓固定清单，不抓全量：可复现是评估基线的前提（ROADMAP 待定项 3）。
    """
    peps_dir = target_dir / _PEPS_PREFIX
    peps_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    total_bytes = 0
    with httpx.Client(timeout=CORPUS_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
        _check_pep_index(client)

        for number in PEP_NUMBERS:
            url = f"{PEPS_BASE_URL}pep-{number:04d}/"
            response = client.get(url)
            response.raise_for_status()
            destination = peps_dir / f"pep-{number:04d}.html"
            destination.write_bytes(response.content)
            written += 1
            total_bytes += len(response.content)

    logger.info(
        "corpus.fetched.peps",
        documents=written,
        bytes_written=total_bytes,
        source_url=PEP_INDEX_URL,
    )
    return FetchReport(documents=written, bytes_written=total_bytes, source_url=PEP_INDEX_URL)


def _check_pep_index(client: httpx.Client) -> None:
    """校验 `PEP_NUMBERS` 都在上游索引里。**不抛** —— 见 `fetch_peps` 的说明。"""
    try:
        response = client.get(PEP_INDEX_URL, timeout=CORPUS_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        index = json.loads(response.text)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        logger.error("corpus.pep_index_unavailable", error=str(exc))
        return

    if not isinstance(index, dict):
        logger.error("corpus.pep_index_unexpected_shape", shape=type(index).__name__)
        return

    missing = [number for number in PEP_NUMBERS if str(number) not in index]
    if missing:
        logger.error("corpus.pep_numbers_missing", missing=missing)


def fetch_all(target_dir: Path) -> list[FetchReport]:
    """`--fetch` 的入口：官方文档 + PEP。返回两份报告供 CLI 汇总打印。"""
    return [fetch_official_docs(target_dir), fetch_peps(target_dir)]


def merge_fetch_reports(reports: Iterable[FetchReport]) -> FetchReport:
    """把多份报告合成一份，便于 CLI 打一行总结。"""
    items = list(reports)
    if not items:
        return FetchReport(documents=0, bytes_written=0, source_url="")
    return FetchReport(
        documents=sum(item.documents for item in items),
        bytes_written=sum(item.bytes_written for item in items),
        source_url="; ".join(item.source_url for item in items),
    )
