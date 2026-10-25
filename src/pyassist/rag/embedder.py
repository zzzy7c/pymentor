"""★ 唯一向量化出口（§2 单点所有权）。ingest 与 retriever 都只经这里拿向量。

**为什么是同步 Protocol，不自己开线程（SPEC D2）**：

1. `SentenceTransformer.encode` 本就是同步 CPU 调用。Protocol 若做成 `async`，
   ingest 这个 **CLI 同步路径**反而要裹一层 `asyncio.run`。
2. `to_thread` 放在这里，会让 ingest 白付一次线程跳；线程跳只放在 retriever 的 async
   边界（`rag/retriever.py`）。**这是"谁拥有并发"的分工，不是疏漏。**
3. `model_id` / `dimension` 是索引元数据（`rag/store.py` 的 `build_index_metadata`）
   与漂移校验的输入，必须是 Protocol 的一等成员 —— 换模型忘了重建索引是最典型的
   静默事故，这两个属性就是捕捉它的钩子。
4. `count_tokens` 也在 Protocol 上，尽管它不算"向量化"。原因是**只有这里知道 tokenizer**：
   填充开销（SPEC §10 第 14 条）由批内最长的那条按 **token** 数决定，不是按字符数 ——
   实测按字符排序只省 20.1%，按 token 排序省 42.2%。谁能数 token 谁才能正确分批，
   而那个能力天然长在本模块里。让它留在 Protocol 上，测试替身就必须实现它，
   于是"分桶"这件事在离线测试里走的是**和生产同一条判据**。

DeepSeek **不提供** embedding 接口（§1），所以本模块是项目的第二个模型供应商接入点，
且它是**唯一**一个。模型 id 只写在 `config.DEFAULT_EMBEDDING_MODEL_ID` 一处。
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from typing import Protocol, runtime_checkable

from pyassist.config import EMBEDDING_MAX_SEQ_TOKENS
from pyassist.errors import ConfigError
from pyassist.logging import get_logger

logger = get_logger(__name__)

EmbeddingVector = list[float]

# `count_tokens` 内部切片大小。只影响**分词**的内存峰值，不影响结果，
# 所以不进 `config.py` —— 它不是一个可调的业务参数，是本地内存纪律。
_TOKENIZE_BATCH_SIZE = 512


@runtime_checkable
class TokenizerLike(Protocol):
    """`count_truncated` / `token_lengths` 需要的那一小片分词器接口。

    单独立一个协议是为了让截断统计**能离线单测**：`tests/test_embedder.py` 注入一个假分词器，
    不下载模型、不碰 torch（§5.1）。真 `PreTrainedTokenizerBase` 天然满足它。
    """

    def __call__(
        self, texts: list[str], *, add_special_tokens: bool, truncation: bool
    ) -> dict[str, list[list[int]]]: ...


def count_truncated(tokenizer: TokenizerLike, texts: Sequence[str], max_tokens: int) -> int:
    """数出有多少条文本会被截断到 `max_tokens`。纯函数。

    **为什么非要知道这件事**：截断是静默的 —— 向量照样算得出来，只是它没看过后半段。
    于是"检索莫名其妙变差"变成一桩无头案。宁可每次 ingest 多花一遍分词的代价，
    也要让它在日志里出现一次（`embed.truncated`）。
    """
    if not texts:
        return 0
    encoded = tokenizer(list(texts), add_special_tokens=True, truncation=False)["input_ids"]
    return sum(1 for token_ids in encoded if len(token_ids) > max_tokens)


def token_lengths(tokenizer: TokenizerLike, texts: Sequence[str], max_tokens: int) -> list[int]:
    """每条文本**实际参与计算**的 token 数（按 `max_tokens` 截断之后）。纯函数。

    **必须截断，不能拿原始长度**：`embed_documents` 走 `encode`，而 `encode` 是按
    `max_seq_length` 截断的（见类 docstring 的内存闸门）。所以一条 5000 token 的文本
    和一条 1024 token 的文本，前向开销**完全相同**。拿未截断的长度去排序，
    会把这两条分进不同的桶 —— 那是假的精度，换不来任何节省。
    """
    if not texts:
        return []
    encoded = tokenizer(list(texts), add_special_tokens=True, truncation=False)["input_ids"]
    return [min(len(token_ids), max_tokens) for token_ids in encoded]


@runtime_checkable
class Embedder(Protocol):
    """向量化器的形状。测试用的 `tests/fakes/embedder.py` 与生产实现都满足它。"""

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        """批量嵌入**入库文本**。某些模型族（如 e5）此处与 `embed_query` 前缀不同。"""
        ...

    def embed_query(self, text: str) -> EmbeddingVector:
        """嵌入**查询文本**。单列一个方法正是为非对称模型族留的（SPEC D2）。"""
        ...

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        """每条文本实际占用的 token 数，口径与 `embed_documents` 一致（截断后）。

        存在的理由只有一条：`ingest._length_bucketed` 靠它把长度相近的凑成一批。
        填充开销由批内**最长**的成员决定 —— 按字符数排序只能省 20.1%，
        按 token 数排序省 42.2%（SPEC §10 第 14 条，真实语料 17366 条实测）。
        """
        ...


class SentenceTransformerEmbedder:
    """本地 `sentence-transformers` 实现。**惰性 import**：import 本模块不拖起 torch。

    `torch` 的 import 是秒级开销且占内存，而阶段四的 API 进程在**不用 RAG 的请求**上
    不该付这笔钱。所以 `SentenceTransformer` 的 import 放在 `__init__` 里。

    **`max_seq_length` 被显式改成 `EMBEDDING_MAX_SEQ_TOKENS`**，不继承模型自带的 8192。
    这是内存闸门：注意力掩码 O(batch × seq²)，8192 token 在 ingest 的 batch 下是几十 GB，
    2026-10-13 就是崩在这一步（`DefaultCPUAllocator: not enough memory: 4899708032 bytes`）。
    闸门放在**这里**而不是调用方，因为本模块是向量化的唯一出口（§2）——
    调用方给什么批量、什么长度的文本都不该能把它撑爆。
    """

    def __init__(self, model_id: str) -> None:
        from sentence_transformers import SentenceTransformer  # 惰性：import 期不碰 torch

        self._model_id = model_id
        self._model = SentenceTransformer(model_id)
        self._model.max_seq_length = EMBEDDING_MAX_SEQ_TOKENS

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        """走模型自己的声明，**不是**硬编码 1024。

        换 `intfloat/multilingual-e5-small` 时它是 384；硬编码会让写进索引的
        `dimension` 元数据撒谎，而漂移校验正是靠它。

        上游签名是 `int | None`（模型未加载完时可能拿不到）。拿到 `None` 说明这个模型
        对象处于不可用状态 —— 那是配置/依赖问题，抛 `ConfigError`（500），
        而不是让 `dimension=0` 悄悄写进索引元数据，把下一次漂移校验也一起废掉。
        """
        dimension = self._model.get_sentence_embedding_dimension()
        if dimension is None:
            raise ConfigError(
                f"模型 {self._model_id!r} 没有报告向量维度，它可能未正确加载",
                details={"embedding_model_id": self._model_id},
            )
        return int(dimension)

    def embed_documents(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        self._warn_if_truncating(texts)
        encoded = self._model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        # `encoded` 是 Any（上游无 stub）。先落到**显式注解的局部变量**再返回，
        # 否则 mypy --strict 的 no-any-return 会红。别"化简"成直接 return（SPEC R7）。
        vectors: list[EmbeddingVector] = [[float(x) for x in row] for row in encoded]
        return vectors

    def _warn_if_truncating(self, texts: Sequence[str]) -> None:
        """截断发生在 `encode` 内部（`truncation="longest_first"`），默认一声不吭。

        这里在下手之前先数一遍，把"有多少条被砍了尾巴"变成一个可检索的日志字段。
        代价是每个 batch 多一遍分词 —— 相对前向计算可以忽略，相对一条静默变差的
        召回曲线则完全值。
        """
        truncated = count_truncated(self._model.tokenizer, texts, EMBEDDING_MAX_SEQ_TOKENS)
        if truncated:
            logger.warning(
                "embed.truncated",
                model_id=self._model_id,
                truncated=truncated,
                total=len(texts),
                max_seq_tokens=EMBEDDING_MAX_SEQ_TOKENS,
            )

    def embed_query(self, text: str) -> EmbeddingVector:
        return self.embed_documents([text])[0]

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        """走模型自带 tokenizer，与 `encode` 内部用的是同一个对象、同一套规则。

        全量 17366 条扫一遍约 3~4 秒，相对 6 小时的嵌入可以忽略（SPEC §10 第 14 条）。

        **切片而不是一次性喂**：`truncation=False` 会把每条文本的 input_ids 原样吐回来，
        全量一次调用就是 400 万个 int 同时驻留（约 150MB 瞬时）。内存纪律归本模块 ——
        与 `max_seq_length` 同理，调用方给多大的 `texts` 都不该能把它撑爆。
        """
        lengths: list[int] = []
        for start in range(0, len(texts), _TOKENIZE_BATCH_SIZE):
            lengths.extend(
                token_lengths(
                    self._model.tokenizer,
                    texts[start : start + _TOKENIZE_BATCH_SIZE],
                    EMBEDDING_MAX_SEQ_TOKENS,
                )
            )
        return lengths


@cache
def get_embedder(model_id: str) -> Embedder:
    """唯一模型实例工厂。`functools.cache`：同一进程内一个模型只加载一次。

    入参是**必填** `str`，不接受 `None`：调用方一律显式传 `settings.embedding_model_id`，
    避免"有的地方传 None、有的地方传常量"两种语义并存（SPEC §5.2）。
    """
    return SentenceTransformerEmbedder(model_id)


def warm_up(model_id: str) -> None:
    """显式预热（下载 + torch 初始化）。ingest CLI 与阶段六的 lifespan 用。

    放在这里而不是让人"顺手调一次 get_embedder"，是为了让调用点读得懂意图：
    这一行就是**已知的重阻塞**，别把它放进事件循环。
    """
    get_embedder(model_id)


def reset_embedder_cache() -> None:
    """换模型 / 测试收尾。唯一清缓存的地方 —— 别在别处 `cache_clear()`。"""
    get_embedder.cache_clear()
