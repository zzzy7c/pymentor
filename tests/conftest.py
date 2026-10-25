"""本项目的第一个 conftest。只放"所有测试可能用到"的夹具。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from chromadb.api.models.Collection import Collection
from fastapi.testclient import TestClient

# pytest 默认的 `prepend` 导入模式会把 `tests/` 放进 sys.path，所以 `fakes` 是顶层包名。
# 前提是 `tests/` 下**没有** `__init__.py` —— 加了它这行就得改成 `tests.fakes.embedder`。
from fakes.embedder import HashingEmbedder
from fakes.llm import ScriptedChatModel
from fakes.sandbox import FakeSandboxRunner
from pyassist.agent.tools.docs import Fetcher, Searcher
from pyassist.api.main import create_app
from pyassist.db import models
from pyassist.db.session import open_engine
from pyassist.rag import retriever
from pyassist.rag import store as store_module
from pyassist.rag.ingest import ingest_corpus
from pyassist.rag.retriever import RetrievedChunk

RagDeps = tuple[Searcher, Fetcher, Callable[[], Awaitable[None]]]

FIXTURE_CORPUS_DIR = Path(__file__).resolve().parent / "fixtures" / "corpus"


@pytest.fixture
def tmp_database_url(tmp_path: Path) -> str:
    """临时库。测试绝不碰 data/app.db —— 那是开发者的真实数据。"""
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


def create_business_tables(database_url: str) -> None:
    """测试启动 app 前建业务表。生产走 Alembic，测试与生产用**同一份** metadata。"""

    async def run() -> None:
        async with open_engine(database_url) as engine, engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

    asyncio.run(run())


@pytest.fixture
def client(tmp_database_url: str) -> Iterator[TestClient]:
    """**必须当上下文管理器用**：否则 lifespan 不运行，app.state.engine 不存在。

    且 raise_server_exceptions=False：我们**刻意**测兜底 500 的信封，
    而 ServerErrorMiddleware 在生成响应后会把异常再抛出去（M1）。
    """
    create_business_tables(tmp_database_url)
    app = create_app(database_url=tmp_database_url, rate_limit_enabled=False)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------- 阶段五：RAG 夹具（SPEC §5.9）----------


@pytest.fixture
def hashing_embedder() -> HashingEmbedder:
    """64 维确定性哈希嵌入（见 `tests/fakes/embedder.py`）。"""
    return HashingEmbedder()


@pytest.fixture
def fixture_corpus_dir() -> Path:
    """夹具语料根目录。布局与生产一致，因此可以直接当 `--corpus` 参数用 ——
    测试路径与生产路径**走同一段代码**，这正是"测试过了生产却崩"的解药。"""
    return FIXTURE_CORPUS_DIR


@pytest.fixture
def chroma_path(tmp_path: Path) -> Path:
    """临时向量库目录。

    **每个测试一个独立目录**是硬要求：注入的 64 维 fake 与生产 1024 维的 bge-m3
    不能共用一个集合，否则 `InvalidDimensionException`（SPEC D10 / C10）。
    指到 `tmp_path` 同时保证测试永不写 `data/chroma`（那是 §4 禁区）。
    """
    return tmp_path / "chroma"


@pytest.fixture
def collection(chroma_path: Path, hashing_embedder: HashingEmbedder) -> Collection:
    """已建好夹具语料的向量库集合。

    **走生产的 `ingest_corpus`**，只是喂了夹具语料与 fake embedder ——
    不是一个"测试专用建库函数"。后者会让测试通过而生产崩。
    """
    coll = store_module.get_or_create_collection(
        path=chroma_path,
        metadata=store_module.build_index_metadata(
            model_id=hashing_embedder.model_id, dimension=hashing_embedder.dimension
        ),
    )
    ingest_corpus(FIXTURE_CORPUS_DIR, collection=coll, embedder=hashing_embedder)
    return coll


# ---------- 阶段六：问答图与 SSE（SPEC §5.14）----------


@pytest.fixture
def scripted_model() -> ScriptedChatModel:
    """空的脚本模型。用例自己往 `.script` 里塞 `AIMessage`。"""
    return ScriptedChatModel()


@pytest.fixture
def rag_deps(collection: Collection, hashing_embedder: HashingEmbedder) -> RagDeps:
    """把**已经配对好**的三个可调用对象交给 `create_app`（D11）。

    配对发生在这里、不在 `create_app` 里：图与预检必须共用同一份夹具集合 +
    同一个哈希 embedder，否则"预检说索引兼容、检索却漂移"只在测试里发生。
    v1 与 v2 两张图共用这一份 —— 它们本来就走同一条检索路径。
    """

    async def search(query: str, top_k: int) -> list[RetrievedChunk]:
        return await retriever.retrieve(
            query, top_k=top_k, collection=collection, embedder=hashing_embedder
        )

    async def fetch(doc_url: str) -> list[RetrievedChunk]:
        return await retriever.fetch_document(doc_url, collection=collection)

    async def preflight() -> None:
        await retriever.assert_index_ready(collection=collection, embedder=hashing_embedder)

    return search, fetch, preflight


@pytest.fixture
def qa_app(
    tmp_database_url: str,
    rag_deps: RagDeps,
    scripted_model: ScriptedChatModel,
):
    search, fetch, preflight = rag_deps
    create_business_tables(tmp_database_url)
    return create_app(
        database_url=tmp_database_url,
        rate_limit_enabled=False,
        chat_model=scripted_model,
        search=search,
        fetch=fetch,
        preflight=preflight,
    )


@pytest.fixture
def qa_client(qa_app) -> Iterator[TestClient]:
    """**上下文管理器**：lifespan 必须跑，否则没有 checkpointer、也没有图。"""
    with TestClient(qa_app, raise_server_exceptions=False) as c:
        yield c


# ---------- 阶段七：诊断图与 API（SPEC §5.15）----------


@pytest.fixture
def fake_sandbox() -> FakeSandboxRunner:
    """空的假沙箱。用例往 `.script` 里塞 `SandboxResult`，绝不执行任何代码。"""
    return FakeSandboxRunner()


@pytest.fixture
def diagnose_app(
    tmp_database_url: str,
    rag_deps: RagDeps,
    scripted_model: ScriptedChatModel,
    fake_sandbox: FakeSandboxRunner,
):
    """诊断专用 app。`diagnose_model` 与 `chat_model` 共用同一个脚本模型 ——
    本阶段测试只跑 v2，v1 的图编译出来但不会被调用。"""
    search, fetch, preflight = rag_deps
    create_business_tables(tmp_database_url)
    return create_app(
        database_url=tmp_database_url,
        rate_limit_enabled=False,
        chat_model=scripted_model,
        diagnose_model=scripted_model,
        sandbox_runner=fake_sandbox,
        search=search,
        fetch=fetch,
        preflight=preflight,
    )


@pytest.fixture
def diagnose_client(diagnose_app) -> Iterator[TestClient]:
    with TestClient(diagnose_app, raise_server_exceptions=False) as c:
        yield c
