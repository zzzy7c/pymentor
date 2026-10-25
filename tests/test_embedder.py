"""嵌入器的**截断上报**与**长度口径**（`rag/embedder.py`）。

为什么这两件事值得一组测试：

- `count_truncated`：截断是**静默**的。`SentenceTransformer.encode` 内部
  `truncation="longest_first"`，超长文本被砍掉尾巴，向量照算 —— 只是它没看过后半段。
  于是召回率悄悄变差，而没有任何东西报错。唯一能把它变成可查事实的，是嵌入前那次计数。
- `token_lengths`：这个数**决定哪几条 chunk 进同一批**（§10 第 14 条）。
  它错成 `len(text)` 时，分桶照样"能跑"，只是省下的是 20.1% 而不是 42.2% ——
  一次 6 小时的重建白扔 1.3 小时，而没有任何东西报错。

真模型（bge-m3）在这里是拿不到的：测试必须离线（§5.1）。所以被测的是**纯函数**
`count_truncated` / `token_lengths`，分词器由测试注入。
"""

from __future__ import annotations

from typing import Any

from pyassist.rag.embedder import count_truncated, token_lengths


class FakeTokenizer:
    """按**空白**切词的假分词器。形状对齐 `TokenizerLike`，行为足以验证计数逻辑。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, texts: list[str], *, add_special_tokens: bool, truncation: bool
    ) -> dict[str, list[list[int]]]:
        self.calls.append(
            {
                "texts": list(texts),
                "add_special_tokens": add_special_tokens,
                "truncation": truncation,
            }
        )
        return {"input_ids": [[i for i, _ in enumerate(text.split())] for text in texts]}


def test_only_texts_over_the_cap_are_counted() -> None:
    """边界取**严格大于**：长度正好等于上限的文本不会被 `encode` 截断。"""
    tokenizer = FakeTokenizer()
    texts = ["a b c", "a b c d", "a b c d e"]

    assert count_truncated(tokenizer, texts, max_tokens=3) == 2


def test_no_text_exceeds_the_cap() -> None:
    tokenizer = FakeTokenizer()

    assert count_truncated(tokenizer, ["a b", "a b c"], max_tokens=10) == 0


def test_empty_input_does_not_tokenize_at_all() -> None:
    """空 batch 不调分词器。`ingest` 会传进空批（比如语料里只有空块），
    而一个空 `input_ids` 的返回形状会让下游 `sum` 之外的任何写法都难写对。"""
    tokenizer = FakeTokenizer()

    assert count_truncated(tokenizer, [], max_tokens=10) == 0
    assert tokenizer.calls == []


def test_counting_must_not_truncate_while_measuring() -> None:
    """**量长度时绝不能顺手截断。**

    这是最容易写错、且错了完全看不出来的一行：`truncation=True` 时返回的 token 数
    永远 ≤ 上限，计数恒为 0 —— 于是上报机制看着还在，实际永远沉默。
    """
    tokenizer = FakeTokenizer()

    count_truncated(tokenizer, ["a b c d"], max_tokens=2)

    assert tokenizer.calls[0]["truncation"] is False


def test_token_lengths_reports_each_texts_own_length() -> None:
    tokenizer = FakeTokenizer()

    assert token_lengths(tokenizer, ["a b", "a b c", "a"], max_tokens=10) == [2, 3, 1]


def test_token_lengths_caps_at_the_same_limit_embed_documents_uses() -> None:
    """超限的文本报**上限**，不报它自己的真实长度 —— 这是这一条测试的全部意义。

    `embed_documents` 走 `encode`，而 `encode` 按 `max_seq_length`
    （= `EMBEDDING_MAX_SEQ_TOKENS`）截断。所以一条 5000 token 与一条 1024 token 的文本，
    前向开销**完全一样**。若这里返回 5000 与 1024，分桶会把它们分进不同的批 ——
    本来是同一种大小。那是**假精度**：多切一刀，省下的算力为零。
    """
    tokenizer = FakeTokenizer()
    texts = ["a b c d e", "a b c d e f g h"]  # 5 与 8 个 token

    assert token_lengths(tokenizer, texts, max_tokens=6) == [5, 6]


def test_token_lengths_truncates_nothing_while_measuring() -> None:
    """与 `count_truncated` 同一条纪律：自己按 `max_tokens` 截，**不叫分词器截**。

    让分词器截（`truncation=True`）也能得到 ≤ 上限的数，但那同时抹掉了"本来多长"，
    而 `min(len, max_tokens)` 两件事都做对了。
    """
    tokenizer = FakeTokenizer()

    token_lengths(tokenizer, ["a b c d"], max_tokens=2)

    assert tokenizer.calls[0]["truncation"] is False
    assert tokenizer.calls[0]["add_special_tokens"] is True, (
        "口径必须与 `encode` 相同：特殊 token 也算进长度，否则分桶用的长度比"
        "真实前向的小 2 个 token，边界处的批次就分错了"
    )
    assert tokenizer.calls[0]["add_special_tokens"] is True, (
        "特殊 token（<s>/</s>）要算进长度，否则会差 2 个 token 才报截断"
    )


def test_token_lengths_of_nothing_tokenizes_nothing() -> None:
    """空输入不碰分词器 —— 形状对齐 `count_truncated` 的那条（`ingest` 会传空批）。"""
    tokenizer = FakeTokenizer()

    assert token_lengths(tokenizer, [], max_tokens=10) == []
    assert tokenizer.calls == []
