"""`POST /knowledge` —— 非流式的同一条问答路径（SPEC D10）。

本文件**只做协议翻译**：把 `QAResult` 套进 `Envelope[QAData]`。业务判断一律在
`services/qa.py`，错误 → 状态码一律在 `api/errors.py`（§2 单点所有权）。

空检索是**正常业务结果**：`raise RetrievalEmpty` 之后由 `api/errors.py` 映射成
200 + `ok=true` + `data.status="empty"`，不是 500（§3.2）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from pyassist.api.deps import (
    build_meta,
    get_qa_graph,
    get_request_context,
    get_session,
    require_quota,
)
from pyassist.api.schemas import (
    CitationData,
    Envelope,
    KnowledgeRequest,
    QAData,
    envelope_success,
    usage_to_schema,
)
from pyassist.config import DEFAULT_USER_ID
from pyassist.errors import RetrievalEmpty
from pyassist.services.qa import QAResult, answer
from pyassist.services.usage import record_request_usage

router = APIRouter(dependencies=[Depends(require_quota)])


@router.post("/knowledge", response_model=Envelope[QAData])
async def knowledge(
    request: Request,
    body: KnowledgeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Envelope[QAData]:
    qa = get_qa_graph(request)
    # 非流式也要预检：否则 `CorpusMissing` 会在图里才冒出来，而那时
    # `/knowledge` 已经进了 `stream_answer` 的异常翻译路径（多绕一圈、日志更糊）。
    await qa.preflight()
    request_id = get_request_context(request).request_id
    thread_id = _thread_id(request, body)
    result = await answer(
        body.question,
        graph=qa.graph,
        thread_id=thread_id,
        request_id=request_id,
    )
    await record_request_usage(
        session,
        request_id=request_id,
        raw_session_id=thread_id,
        user_id=DEFAULT_USER_ID,
        model_id=qa.model_id,
        usage=result.usage,
    )
    if result.status == "empty":
        # 唯一业务分支，且**不碰 HTTP**：状态码是 api/errors.py 的事（§4）。
        raise RetrievalEmpty(query=body.question)
    return envelope_success(
        _to_data(result), meta=build_meta(request, usage=usage_to_schema(result.usage))
    )


def _thread_id(request: Request, body: KnowledgeRequest) -> str:
    """与 `/chat` 逐字相同的口径：`session_id` 缺省用 `request_id`（SPEC §5.7）。

    刻意每个 router 各写一行而不是抽到 `services/`：那会让 `services/` import fastapi，
    `_thread_id` 也就成了跨层依赖（§5.7 末段）。
    """
    ctx = get_request_context(request)
    return body.session_id or ctx.request_id


def _to_data(result: QAResult) -> QAData:
    """`QAResult` → 响应契约。引用**按原序、不重排**：编号就是 `Citation.index`（D8）。"""
    return QAData(
        answer=result.answer,
        citations=[
            CitationData(
                index=citation.index,
                title=citation.title,
                section=citation.section,
                url=citation.url,
                snippet=citation.snippet,
            )
            for citation in result.citations
        ],
        status=result.status,
    )
