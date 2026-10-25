"""Streamlit 前端：只经 HTTP 与后端说话（SPEC D11 / 守护 #38）。

五页：问答 / 诊断 / 练习 / 历史与进度 / 统计。

**状态放 URL，不放 `session_state`**：`session_id` 写进 `st.query_params["sid"]`，
刷新后从 URL 读回，历史与进度用 `GET /history` 重新拉 —— 这样"刷新后还在"
不依赖进程内存，也符合 §5.5 的验收口径。

本文件**不 import `pyassist`**：前端拿到的永远是契约里的 dict，不是服务端的模型。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import streamlit as st
from api_client import ApiClient, ApiError, ChatEvent

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
_API_BASE_URL = os.environ.get("PYASSIST_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")

_TOPICS = {"列表": "list", "字典": "dict", "装饰器": "decorator", "异步": "async"}

_PAGES = ("问答", "诊断", "练习", "历史与进度", "统计")


@st.cache_resource
def _client(base_url: str) -> ApiClient:
    return ApiClient(base_url)


@st.cache_resource
def _health_client(base_url: str) -> ApiClient:
    """探活专用短超时客户端：后端没起时不该让页面转十分钟。"""
    return ApiClient(base_url, timeout=3.0)


def main() -> None:
    st.set_page_config(page_title="Python 学习助手", layout="wide")
    client = _client(_API_BASE_URL)
    session_id = _session_id()

    st.sidebar.title("Python 学习助手")
    st.sidebar.caption(f"后端：{_API_BASE_URL}")
    st.sidebar.caption(f"会话：`{session_id[:8]}…`")
    _health_badge()
    page = st.sidebar.radio("页面", _PAGES)

    if page == "问答":
        _page_qa(client, session_id)
    elif page == "诊断":
        _page_diagnose(client, session_id)
    elif page == "练习":
        _page_exercise(client, session_id)
    elif page == "历史与进度":
        _page_history(client, session_id)
    else:
        _page_stats(client)


def _session_id() -> str:
    """`st.query_params` 是刷新后仍然存在的那份状态（SPEC D11）。"""
    raw = st.query_params.get("sid")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not raw:
        raw = uuid.uuid4().hex
        st.query_params["sid"] = raw
    return str(raw)


def _health_badge() -> None:
    try:
        data = _health_client(_API_BASE_URL).health()
    except ApiError as exc:
        st.sidebar.error(f"API 不可用：{exc.message}")
        return
    status = str(data.get("status", "unknown"))
    if status == "ok":
        st.sidebar.success("API 正常")
    else:
        st.sidebar.warning(f"API 降级（status={status}）")
    st.sidebar.caption(
        f"数据库 {'✓' if data.get('database_ok') else '✗'} · "
        f"沙箱 {'✓' if data.get('sandbox_ok') else '✗'}"
    )


def _page_qa(client: ApiClient, session_id: str) -> None:
    st.header("文档问答")
    st.caption("答案只来自 Python 官方文档与 PEP；检索不到时会直说“文档中没有”。")
    question = st.text_area("问题", height=120, placeholder="例如：asyncio.gather 的异常怎么处理？")
    if st.button("提问", type="primary", disabled=not question.strip()):
        citations: list[dict[str, Any]] = []

        def tokens() -> Iterator[str]:
            try:
                for event in client.chat_stream(question.strip(), session_id):
                    yield from _consume_chat_event(event, citations)
            except ApiError as exc:
                _show_error(exc)

        st.write_stream(tokens())
        _render_citations(citations)


def _consume_chat_event(event: ChatEvent, citations: list[dict[str, Any]]) -> Iterator[str]:
    """把 SSE 事件拆成"要显示的 token"与"要攒起来的引用"。"""
    if event.type == "token":
        text = event.payload.get("text")
        if isinstance(text, str) and text:
            yield text
    elif event.type == "citations":
        raw = event.payload.get("citations")
        if isinstance(raw, list):
            citations.extend(item for item in raw if isinstance(item, dict))
    elif event.type == "tool":
        phase = event.payload.get("phase")
        name = event.payload.get("name", "tool")
        if phase == "start":
            st.caption(f"调用工具：{name}…")
    elif event.type == "error":
        st.error(str(event.payload.get("message") or "生成失败"))


def _page_diagnose(client: ApiClient, session_id: str) -> None:
    st.header("错误诊断")
    st.caption("贴报错 → 检索文档 → 解释 → 修复 → 在沙箱里真的跑一遍。")
    error_text = st.text_area(
        "报错文本", height=200, placeholder="Traceback (most recent call last): ..."
    )
    code = st.text_area("原始代码（可选）", height=220, key="diagnose_code")
    if st.button("诊断", type="primary", disabled=not error_text.strip()):
        try:
            with st.spinner("诊断中：检索文档并跑沙箱验证，可能需要几十秒…"):
                data = client.diagnose(error_text.strip(), code.strip() or None, session_id)
        except ApiError as exc:
            _show_error(exc)
            return
        _render_diagnosis(data)


def _render_diagnosis(data: dict[str, Any]) -> None:
    st.subheader("解释")
    st.markdown(str(data.get("explanation") or "（无）"))
    if data.get("error_type"):
        st.caption(f"错误类型：`{data['error_type']}`")

    fixed = data.get("fixed_code")
    if fixed:
        st.subheader("修复后的代码")
        st.code(str(fixed), language="python")

    summary = data.get("change_summary") or []
    if summary:
        st.subheader("改了什么")
        for item in summary:
            st.markdown(f"- {item}")

    st.subheader("沙箱验证")
    _render_sandbox(data.get("verification"))
    _render_citations(data.get("citations") or [])


def _render_sandbox(verification: Any) -> None:
    if not isinstance(verification, dict):
        st.info("本次没有沙箱验证结果（可能沙箱不可用）。这不等于修复失败。")
        return
    status = str(verification.get("status"))
    label = {
        "ok": "✅ 通过",
        "error": "❌ 运行报错",
        "timeout": "⏱ 超时",
        "rejected": "⛔ 被安全策略拦截",
    }.get(status, status)
    st.markdown(
        f"**{label}** · 退出码 `{verification.get('exit_code')}` · "
        f"{verification.get('duration_ms')} ms"
    )
    stdout = str(verification.get("stdout") or "")
    stderr = str(verification.get("stderr") or "")
    if stdout:
        st.code(stdout, language="text")
    if stderr:
        st.code(stderr, language="text")
    if verification.get("is_truncated"):
        st.caption("输出已截断（保留头尾）。")


def _page_exercise(client: ApiClient, session_id: str) -> None:
    st.header("练习")
    left, right = st.columns(2)
    topic_label = left.selectbox("主题", list(_TOPICS))
    right.write("")
    right.write("")
    if right.button("出题", type="primary"):
        try:
            with st.spinner("出题并自校验中…"):
                st.session_state["exercise"] = client.create_exercise(
                    _TOPICS[topic_label], session_id
                )
            st.session_state.pop("submission", None)
        except ApiError as exc:
            _show_error(exc)
            return

    exercise = st.session_state.get("exercise")
    if not isinstance(exercise, dict):
        st.info("选择主题后点“出题”。生成的题会在沙箱里先用参考答案跑通测试。")
        return

    st.subheader(str(exercise.get("title") or "练习"))
    st.markdown(str(exercise.get("prompt") or ""))
    exercise_id = str(exercise.get("exercise_id") or "")
    code = st.text_area(
        "你的代码",
        value=str(exercise.get("starter_code") or ""),
        height=320,
        key=f"code_{exercise_id}",
    )
    if st.button("提交", type="primary", disabled=not code.strip()):
        try:
            with st.spinner("在沙箱里跑测试并批改中…"):
                st.session_state["submission"] = client.submit(exercise_id, code, session_id)
        except ApiError as exc:
            _show_error(exc)
            return

    submission = st.session_state.get("submission")
    if isinstance(submission, dict):
        _render_submission(submission)


def _render_submission(data: dict[str, Any]) -> None:
    passed = bool(data.get("is_passed"))
    score = data.get("score", 0)
    if passed:
        st.success(f"通过 · {score} 分")
    else:
        st.error(f"未通过 · {score} 分")

    if data.get("summary"):
        st.markdown(str(data["summary"]))

    tests = data.get("tests") or []
    if tests:
        st.subheader("测试用例")
        st.dataframe(
            [
                {
                    "用例": item.get("name"),
                    "状态": item.get("status"),
                    "耗时(ms)": item.get("duration_ms"),
                    "消息": item.get("message"),
                }
                for item in tests
                if isinstance(item, dict)
            ],
            use_container_width=True,
        )

    feedback = data.get("feedback") or []
    if feedback:
        st.subheader("反馈")
        for item in feedback:
            if not isinstance(item, dict):
                continue
            st.markdown(
                f"- **{item.get('where')}**：{item.get('why')}  \n  建议：{item.get('hint')}"
            )

    if data.get("stdout"):
        st.subheader("标准输出")
        st.code(str(data["stdout"]), language="text")
    if data.get("stderr"):
        st.subheader("标准错误")
        st.code(str(data["stderr"]), language="text")
    _render_citations(data.get("citations") or [])


def _page_history(client: ApiClient, session_id: str) -> None:
    st.header("历史与进度")
    _page_recommendation(client, session_id)

    st.subheader("提交记录")
    if st.button("刷新"):
        st.session_state.pop("history_items", None)
        st.session_state.pop("history_cursor", None)

    try:
        if "history_items" not in st.session_state:
            _load_history(client, session_id, reset=True)
        items = st.session_state.get("history_items") or []
        if not items:
            st.info("这个会话还没有提交记录。")
        for item in items:
            _render_history_item(item)
        cursor = st.session_state.get("history_cursor")
        if cursor and st.button("加载更多"):
            _load_history(client, session_id, reset=False)
            # 上面的列表已经渲染过；重跑一次才能把新一页画出来。
            st.rerun()
    except ApiError as exc:
        _show_error(exc)


def _load_history(client: ApiClient, session_id: str, *, reset: bool) -> None:
    cursor = None if reset else st.session_state.get("history_cursor")
    page = client.history(session_id, limit=20, cursor=cursor)
    items = st.session_state.setdefault("history_items", []) if not reset else []
    if reset:
        st.session_state["history_items"] = items
    raw = page.get("items") or []
    items.extend(item for item in raw if isinstance(item, dict))
    st.session_state["history_cursor"] = page.get("next_cursor")


def _render_history_item(item: dict[str, Any]) -> None:
    status = "✅" if item.get("is_passed") else "❌"
    title = (
        f"{status} {item.get('exercise_id')} · {item.get('topic')} · "
        f"{item.get('score')} 分 · {item.get('created_at')}"
    )
    with st.expander(title):
        code = str(item.get("submitted_code") or "")
        if code:
            st.code(code, language="python")
        if item.get("stdout"):
            st.markdown("**stdout**")
            st.code(str(item["stdout"]), language="text")
        if item.get("stderr"):
            st.markdown("**stderr**")
            st.code(str(item["stderr"]), language="text")


def _page_recommendation(client: ApiClient, session_id: str) -> None:
    st.subheader("下一步推荐")
    if st.button("给我推荐", type="primary"):
        try:
            with st.spinner("读取错题并生成推荐…"):
                data = client.recommend(session_id)
        except ApiError as exc:
            _show_error(exc)
            return
        st.session_state["recommendation"] = data

    data = st.session_state.get("recommendation")
    if not isinstance(data, dict):
        st.caption("推荐只依据你**实际做错**的题目，模型无权编造题目 id。")
        return

    st.markdown(f"### 下一步：{data.get('topic')}")
    st.markdown(str(data.get("reason") or ""))
    steps = data.get("next_steps") or []
    if steps:
        st.markdown("**具体动作**")
        for step in steps:
            st.markdown(f"- {step}")
    ids = data.get("exercise_ids") or []
    if ids:
        st.caption("引用的错题：" + "、".join(str(item) for item in ids))
    _render_citations(data.get("citations") or [])


def _page_stats(client: ApiClient) -> None:
    st.header("统计")
    try:
        data = client.stats()
    except ApiError as exc:
        _show_error(exc)
        return

    first, second, third, fourth = st.columns(4)
    first.metric("今日输入 Token", f"{int(data.get('today_prompt_tokens', 0)):,}")
    second.metric("今日输出 Token", f"{int(data.get('today_completion_tokens', 0)):,}")
    third.metric("今日成本", f"${float(data.get('today_cost_usd', 0)):.6f}")
    fourth.metric("通过率", f"{float(data.get('pass_rate', 0)) * 100:.1f}%")

    st.caption(
        f"缓存命中输入：{int(data.get('today_cached_tokens', 0)):,} tokens · "
        f"提交 {data.get('passed_attempts', 0)}/{data.get('total_attempts', 0)} 通过 · "
        f"未解决错题 {data.get('unresolved_mistakes', 0)}/{data.get('mistake_total', 0)}"
    )

    st.subheader("今日配额")
    limit = data.get("quota_limit")
    used = int(data.get("quota_used", 0))
    if limit is None:
        st.info(f"未启用配额。今日已用 {used:,} tokens。")
    else:
        limit = int(limit)
        st.progress(min(1.0, used / limit) if limit else 1.0)
        remaining = data.get("quota_remaining")
        leftover = "—" if remaining is None else f"{int(remaining):,}"
        st.caption(f"已用 {used:,} / {limit:,} tokens · 剩余 {leftover}")
        if data.get("is_exhausted"):
            st.error("今日配额已用尽：下一次会调模型的请求将返回 429（RATE_LIMITED）。")

    st.subheader("错题分布")
    topics = data.get("mistakes_by_topic") or []
    if not topics:
        st.info("还没有错题记录。")
    else:
        st.dataframe(
            [{"主题": item.get("topic"), "错题数": item.get("count")} for item in topics],
            use_container_width=True,
        )


def _render_citations(citations: Any) -> None:
    if not isinstance(citations, list) or not citations:
        return
    st.markdown("**来源**")
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        title = citation.get("title") or citation.get("url") or "文档"
        section = citation.get("section")
        suffix = f" — {section}" if section else ""
        st.markdown(f"{citation.get('index')}. [{title}]({citation.get('url')}){suffix}")


def _show_error(exc: ApiError) -> None:
    """错误只展示 `code + message`，不展示 traceback（SPEC D11）。"""
    st.error(f"{exc.code}：{exc.message}")


if __name__ == "__main__":
    main()
