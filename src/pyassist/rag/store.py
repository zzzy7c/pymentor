"""★ 唯一向量库连接点（§2 单点所有权）。Chroma → Qdrant 的切换只改本文件。

本模块做四件事，每件都有非做不可的理由：

1. **`anonymized_telemetry=False`**（SPEC C2）。chromadb 的 `Settings.anonymized_telemetry`
   默认 `True`，不关掉测试就不保证离线（§5.1），CI 会莫名去外网。
2. **`embedding_function=None`**（SPEC C3）。`get_or_create_collection` 的默认值是
   `DefaultEmbeddingFunction()`，省略它 Chroma 会自行下载 ONNX 模型算向量 ——
   那是**第二套嵌入**，直接破 §2 单点所有权。显式传 `None` 掐死它，
   且忘带向量时会立刻 `ValueError` 而非静默算错（C4）。
3. **显式 cosine**（SPEC C5）。默认度量是 l2，而 `rag/retriever.py` 的
   `score = 1 - distance` **只在 cosine 下成立**（C7）—— 不指定就是静默给垃圾排序。
4. **读路径绝不 `get_or_create`**（SPEC D3）。读路径若也用 `get_or_create`，
   它会静默建一个空集合、永远返回 `[]` → 200，§3.2 的 `CorpusMissing → 503` 就成死路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.client import Client as ChromaClient
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings

from pyassist.config import (
    CHROMA_DIR,
    CHUNKER_VERSION,
    COLLECTION_NAME,
    DISTANCE_SPACE,
    HNSW_BATCH_SIZE,
    HNSW_SYNC_THRESHOLD,
)
from pyassist.errors import ConfigError, CorpusMissing
from pyassist.logging import get_logger

logger = get_logger(__name__)

_REBUILD_HINT = "删掉 data/chroma 目录后重跑 python -m pyassist.rag.ingest"

# Chroma 在持久化目录里落的主库文件名。**这是上游的实现细节**，所以它只被用来回答
# "索引建过没有"这一个问题，且由 `test_ingest_idempotent` 盯着 —— 哪天上游改名，
# 测试会红，而不是让 `index_exists()` 静默地永远返回 False。
_INDEX_FILENAME = "chroma.sqlite3"


def get_client(path: Path | None = None) -> ClientAPI:
    """取向量库客户端。`path=None` 走 `config.CHROMA_DIR`（生产默认）。

    `path` 是**唯一**的测试缝：夹具把 path 指到 `tmp_path`，永不碰 `data/chroma`
    （SPEC C1：`PersistentClient` 在构造时就建目录，所以不开口子的话单测一跑就违规）。
    **不加 `set_client_for_test()` 之类的后门** —— 参数注入比全局状态干净（D10）。
    """
    return _client_for(path or CHROMA_DIR)


_clients: dict[Path, ClientAPI] = {}


def _client_for(path: Path) -> ClientAPI:
    """客户端缓存。键是 `Path` 而非 `str` —— 否则 `Path("a/b")` 与 `"a/b"` 会分裂成两个客户端。
    不缓存的话每次 `retrieve()` 都新开一个 sqlite 连接 + 遥测线程，纯浪费。

    **不用 `functools.cache`**：`cache` 列不出已缓存的条目，于是 `close_clients()`
    写不出来 —— 而"关掉客户端"是 2026-10-13 事故的修复点之一（见该函数）。
    """
    client = _clients.get(path)
    if client is None:
        client = chromadb.PersistentClient(
            path=str(path),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        _clients[path] = client
    return client


def close_clients() -> None:
    """关闭所有缓存客户端并丢弃它们 —— **唯一的关闭点**。

    非有它不可（2026-10-13）：Chroma 的 HNSW 索引由后台 compactor 异步落盘，
    `close()` 是"停掉这个 system、把待落盘的活干完"的唯一入口。ingest 的 CLI 原来跑完
    就 `sys.exit()`，compactor 被连人带活掐断 —— 于是磁盘上留下一个只有元数据、
    没有数据文件的段（`Error loading hnsw index`），而 WAL 已被清空，几小时的嵌入全废。

    `close()` 之后 `_clients` 里那些句柄就失效了，所以必须一起丢掉：
    留着它们等于留着一堆"看着能用、一调就炸"的对象。
    """
    for client in _clients.values():
        # `ClientAPI`（抽象基类）**没有**声明 `close()`，只有磁盘实现 `ChromaClient` 有；
        # 上游把 `PersistentClient()` 的返回类型标成 `ClientAPI` 是欠准确的。
        # `cast` 是收窄视图，不是抹平类型错误 —— 对象的 `type()` 实测就是 `ChromaClient`。
        cast("ChromaClient", client).close()
    _clients.clear()


def index_exists(path: Path | None = None) -> bool:
    """索引建过没有。**只读盘，绝不构造客户端。**

    非有它不可的理由（C1）：`PersistentClient` 的构造函数会**立刻建目录和库文件**。
    所以"打开看看里面有没有东西"这个动作本身带副作用 ——
    `ingest --dry-run` 的承诺是只读预演（SPEC §5.5），却会因此留下一个
    `data/chroma/chroma.sqlite3`，把"索引没建"变成"索引建了个空的"。
    §4 又把 `data/chroma/**` 划为禁区，一次预演不该在里面留下任何东西。
    """
    return ((path or CHROMA_DIR) / _INDEX_FILENAME).is_file()


def build_index_metadata(*, model_id: str, dimension: int) -> dict[str, str | int | float | bool]:
    """索引元数据（SPEC D7）。收录进 `collection.metadata`，供漂移校验读回。

    类型只能取 `str / int / float / bool` —— Chroma 的 `collection.metadata` 不接受别的（C11）。
    """
    return {
        "embedding_model_id": model_id,
        "dimension": dimension,
        "space": DISTANCE_SPACE,
        "chunker_version": CHUNKER_VERSION,
    }


def get_or_create_collection(
    *,
    client: ClientAPI | None = None,
    path: Path | None = None,
    metadata: dict[str, str | int | float | bool] | None = None,
) -> Collection:
    """**写路径**（`rag/ingest.py` 用）。读路径一律用 `open_collection()`。

    ⚠️ 对**已存在**的集合，Chroma 会**忽略**传入的 `metadata` / `configuration`
    （`chromadb/api/client.py` 原文：*"If the collection already exists, the schema,
    configuration, and metadata arguments will be ignored."*，SPEC C9）。
    所以漂移校验只能**读** `collection.metadata`，绝不能指望"再写一次就对了"。
    **同理：改了 `HNSW_SYNC_THRESHOLD` 不会作用于已有集合 —— 必须删库重建。**
    """
    return (client or get_client(path)).get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=None,  # C3：掐死 Chroma 自带的 ONNX 嵌入
        configuration={
            "hnsw": {
                "space": DISTANCE_SPACE,  # C5：默认是 l2
                "sync_threshold": HNSW_SYNC_THRESHOLD,
                "batch_size": HNSW_BATCH_SIZE,
            }
        },
        metadata=metadata,
    )


def open_collection(*, client: ClientAPI | None = None, path: Path | None = None) -> Collection:
    """**读路径**（`rag/retriever.py` 用）。集合不存在**或为空** → `CorpusMissing`。

    "不存在"与"存在但 `count() == 0`"同因同果：都是语料没建好。合并处理让
    §3.2 的 `CorpusMissing → 503` 成为**可达**路径 —— 否则它只是个摆设的错误码。
    """
    target = client or get_client(path)
    try:
        collection = target.get_collection(name=COLLECTION_NAME)
    except chromadb.errors.ChromaError as exc:
        raise CorpusMissing(
            f"向量库中没有集合 {COLLECTION_NAME!r}，{_REBUILD_HINT}",
            details={"collection": COLLECTION_NAME},
        ) from exc

    assert_not_empty(collection)
    return collection


def assert_not_empty(collection: Collection) -> None:
    """空集合 = 语料没建好，与"集合不存在"同因同果（判据 7）。

    单独成函数是为了让**注入 collection 的调用方**也能拿到同一个判定 ——
    `rag/retriever.py` 的 `collection=` 参数是给测试与长期句柄用的，
    若只在 `open_collection()` 里查空，一个被清库后仍被持有的句柄会静默返回 `[]`，
    于是"文档里没有"取代了"服务没准备好"，§3.2 的 503 又变成死路径。

    **`count()` 本身也会抛**（2026-10-13，§10 第 15 条）：索引段损坏时它抛
    `InternalError: ... Error loading hnsw index` —— 而 `InternalError` 是 `ChromaError`
    的子类，不是"没数据"的另一种说法。这里把它一并转成 `CorpusMissing`：

    1. 语义确实相同 —— 库在磁盘上但读不出来，就是"引擎没准备好"，不是 500；
    2. §5.3 明令**不允许裸 `ChromaError` 冒到路由层**（错误码枚举里没有它，
       冒上去只能落 `INTERNAL_ERROR`，而 500 会把接手的人引向排查代码而不是重建索引）。

    实测（真实事故库）：`metadata` 与 `configuration_json` 读得出来（它们走 sqlite 目录表），
    只有 `count()` / `get()` / `query()` 会抛 —— 所以这里是唯一的修复点。
    """
    try:
        count = collection.count()
    except chromadb.errors.ChromaError as exc:
        raise CorpusMissing(
            f"集合 {COLLECTION_NAME!r} 读不出来（索引段损坏），{_REBUILD_HINT}",
            details={"collection": COLLECTION_NAME},
        ) from exc

    if count == 0:
        raise CorpusMissing(
            f"集合 {COLLECTION_NAME!r} 为空，{_REBUILD_HINT}",
            details={"collection": COLLECTION_NAME},
        )


def assert_index_compatible(collection: Collection, *, model_id: str) -> None:
    """索引元数据与当前配置的一致性校验（SPEC D7）。不一致 → `ConfigError`。

    守的是「换模型忘了重建索引」这类**静默给垃圾排序**的经典事故 ——
    它不报错，只是答案悄悄变差。这是本项目里最难靠肉眼发现的一类故障，
    所以必须由代码主动拦。

    校验顺序与严格度：
    1. `metadata` 为 `None`（不是本模块建的集合）→ 抛，不能假定它的来历；
    2. `embedding_model_id` 不等 → 抛（这是**向量空间变了**，检索结果无意义）；
    3. `space` 必须 cosine（**主证** metadata + **佐证** `configuration_json`）→ 抛。
       `score = 1 - distance` 只在 cosine 下成立（C7）。佐证走递归找 key，
       因为 `configuration_json` 的内部形状跨版本不保证（C12）；
    4. `chunker_version` 不一致 → **只 warn 不抛**：分块改了不会让相似度失真，
       只是库里混着两代切法。重建是期望动作，但不该阻塞检索。
    """
    metadata = collection.metadata
    if metadata is None:
        raise ConfigError(
            f"集合 {COLLECTION_NAME!r} 缺少索引元数据，无法确认它是用什么模型建的；{_REBUILD_HINT}",
            details={"collection": COLLECTION_NAME},
        )

    indexed_model = metadata.get("embedding_model_id")
    if indexed_model != model_id:
        raise ConfigError(
            f"索引是用 {indexed_model!r} 建的，当前配置是 {model_id!r}；"
            f"两者向量空间不同，{_REBUILD_HINT}",
            details={
                "indexed_embedding_model_id": indexed_model,
                "expected_embedding_model_id": model_id,
            },
        )

    _assert_cosine_space(collection, metadata)

    indexed_chunker = metadata.get("chunker_version")
    if indexed_chunker != CHUNKER_VERSION:
        logger.warning(
            "rag.index.chunker_version_mismatch",
            indexed_chunker_version=indexed_chunker,
            current_chunker_version=CHUNKER_VERSION,
            hint=_REBUILD_HINT,
        )

    _warn_if_legacy_sync_threshold(collection)


def _warn_if_legacy_sync_threshold(collection: Collection) -> None:
    """索引是用旧的 `sync_threshold`（Chroma 默认 1000）建的 → warn，不抛。

    这是 2026-10-13 事故的**前哨**：小阈值意味着写入期间每 1000 条就把整个索引重写一遍，
    每次都是一次"可能写坏且不可恢复"的机会（详见 `config.HNSW_SYNC_THRESHOLD`）。
    已有集合不会因为改了配置就变好（Chroma 忽略重复 `get_or_create` 的 configuration，C9），
    所以这里只能提醒。

    不抛的理由：库是**能读**的，而且重建要几小时 —— 该由人决定什么时候付这笔钱。
    """
    indexed = _find_int_value(_configuration_json(collection), "sync_threshold")
    if indexed is None or indexed == HNSW_SYNC_THRESHOLD:
        return
    logger.warning(
        "rag.index.sync_threshold_stale",
        indexed_sync_threshold=indexed,
        current_sync_threshold=HNSW_SYNC_THRESHOLD,
        hint=_REBUILD_HINT,
    )


def reset_client_cache() -> None:
    """测试收尾：**丢弃**缓存的客户端，但不关闭它们（持有旧句柄的调用方不受影响）。

    与 `close_clients()` 的分工：这里只是让下一个 `get_client()` 新建一个，
    用于测试之间隔离；要"关掉并收尾"用 `close_clients()`。
    """
    _clients.clear()


def _assert_cosine_space(collection: Collection, metadata: dict[str, Any]) -> None:
    """space 校验：metadata 是主证，`configuration_json` 只作佐证（SPEC D7 第 2 条）。"""
    declared = metadata.get("space")
    if declared != DISTANCE_SPACE:
        raise ConfigError(
            f"索引的向量度量是 {declared!r}，本项目要求 {DISTANCE_SPACE!r} —— "
            f"`score = 1 - distance` 只在 cosine 下成立；{_REBUILD_HINT}",
            details={"indexed_space": declared, "expected_space": DISTANCE_SPACE},
        )

    corroborated = _find_string_value(_configuration_json(collection), "space")
    if corroborated is None:
        # 找不到不是错误：configuration_json 的形状跨版本不保证（C12）。
        # metadata 是我们自己写的、形状可控，以它为准。
        logger.debug("rag.index.space_corroboration_missing", collection=COLLECTION_NAME)
        return
    if corroborated != DISTANCE_SPACE:
        raise ConfigError(
            f"索引的 configuration 里 space={corroborated!r}，与元数据声明的 "
            f"{DISTANCE_SPACE!r} 不符；{_REBUILD_HINT}",
            details={"configuration_space": corroborated, "expected_space": DISTANCE_SPACE},
        )


def _configuration_json(collection: Collection) -> Any:
    """读 `configuration_json`，读不到就返回 `None` 让上层退化到只信 metadata。

    Chroma 1.5.9 上它是 `CollectionCommon` 的属性（`CollectionCommon.py:158`，
    返回 `Dict[str, Any]`），但属性本身不在保证的稳定面上，所以用 `getattr`
    并吞掉 `AttributeError` —— **吞的是"属性不存在"，不是"值不对"**，后者仍然会抛。
    """
    try:
        return collection.configuration_json
    except AttributeError:
        return None


def _find_int_value(obj: Any, key: str) -> int | None:
    """在嵌套字典里递归找 `key`，只认整数值。找不到返回 `None`。

    与 `_find_string_value` 同一条理由：`configuration_json` 的形状跨版本不保证（C12）。
    `bool` 要排掉 —— 它是 `int` 的子类，`True` 会被当成 1 静默混进来。
    """
    if isinstance(obj, dict):
        for k, value in obj.items():
            if k == key and isinstance(value, int) and not isinstance(value, bool):
                return value
            found = _find_int_value(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list | tuple):
        for item in obj:
            found = _find_int_value(item, key)
            if found is not None:
                return found
    return None


def _find_string_value(obj: Any, key: str) -> str | None:
    """在嵌套字典里递归找 `key`，只认字符串值。找不到返回 `None`。

    防御式取值：`configuration_json` 的内部形状跨版本不保证（SPEC C12 / R6），
    硬编码 `["hnsw"]["space"]` 会在上游调整结构时静默失效。
    """
    if isinstance(obj, dict):
        for k, value in obj.items():
            if k == key and isinstance(value, str):
                return value
            found = _find_string_value(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list | tuple):
        for item in obj:
            found = _find_string_value(item, key)
            if found is not None:
                return found
    return None
