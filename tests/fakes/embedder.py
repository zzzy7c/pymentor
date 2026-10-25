"""确定性**哈希**嵌入：离线、跨进程稳定的 `Embedder` 替身（SPEC D10）。

为什么非要用哈希嵌入而不是"返回固定向量"或随机向量：

- **固定向量**：所有文本的相似度完全相同，检索测试变成纯噪声 —— 断言"命中正确的文档"
  永远为真，等于没测。
- **随机向量**：每次运行结果不同，测试 flaky。
- **特征哈希**：共享词汇的两段文本余弦相似度更高。于是"用一段包含 `gather` 的查询
  能不能召回讲 `gather` 的那一页"是一个**有意义的**问题，测试因此真的在测检索。

⚠️ **绝不能用 Python 内建 `hash()`**：它对 `str` 是按进程加盐的（`PYTHONHASHSEED`），
会让测试**跨进程 flaky** —— 本地过、CI 挂，且极难排查。`hashlib.sha256` 才是稳定的。
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

_TOKEN_RE = re.compile(r"[a-z0-9_]+")

_SIGN_MASK = 1
_BUCKET_BYTES = 4


class HashingEmbedder:
    """64 维特征哈希嵌入。维度小是刻意的 —— 测试要的是**可判定**，不是高保真。"""

    def __init__(self, dimension: int = 64, model_id: str = "test-hashing-64") -> None:
        self._dimension = dimension
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        """`_TOKEN_RE` 切出来的词数，与 `_vector` 用的是**同一个切法**。

        刻意**不**返回 `len(text)`：替身与真身的 token 口径可以不同（64 维 vs bge-m3），
        但必须**同源** —— 都以"嵌入器自己怎么切文本"为输入。若这里按字符数而生产按 token 数，
        测试就在验证一个生产不存在的分批行为，绿得毫无意义。
        """
        return [len(_TOKEN_RE.findall(text.lower())) for text in texts]

    def _vector(self, text: str) -> list[float]:
        """token → sha256 → 特征哈希到 `dimension` 维 → L2 归一化。

        空文本（或没有 token 的文本）返回**全零向量而不抛**：检索路径不该因为
        库里混进一条空 chunk 就整体炸掉。

        但要注意后果：全零向量与任何东西的余弦距离都退化为 1.0，
        所以**夹具语料里不要放空文档** —— 那会让它的排序看起来像巧合。
        """
        vector = [0.0] * self._dimension
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:_BUCKET_BYTES], "big") % self._dimension
            sign = 1.0 if digest[_BUCKET_BYTES] & _SIGN_MASK else -1.0
            vector[index] += sign
        return _normalize(vector)


def _normalize(vector: list[float]) -> list[float]:
    """L2 归一化。零向量原样返回 —— 除以 0 会得到 `nan`，那会污染整次排序。"""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]
