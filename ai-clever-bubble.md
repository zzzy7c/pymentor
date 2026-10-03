# Python LLM 应用开发学习路线

**前提**：有开发经验，每天 3h+，目标是用 Python 构建 LLM 应用（Chatbot / Agent / RAG）。

---

## 阶段一：Python 速通 + LLM 初体验（第 1 周）

> 有 Python 基础可快速过，零基础重点补。

### 必会知识点
- 虚拟环境（venv / conda）、pip 包管理
- 类型注解（Type Hints）— LLM 库大量使用
- 异步编程（async/await）— 所有 LLM SDK 底层都是异步
- Pydantic 数据模型 — 结构化输出的核心
- Jupyter Notebook 使用

### 第一个 LLM 调用
```python
# 装上就能跑
pip install anthropic openai python-dotenv
```

**练习清单**：
- [ ] 用 python-dotenv 管理 API Key
- [ ] 分别调用 Anthropic 和 OpenAI 的 Chat API
- [ ] 实现 streaming 输出（一行一行打印）
- [ ] 封装一个简单的 Chat 类，支持多轮对话

### 学习资源
- [Anthropic Python SDK 文档](https://github.com/anthropics/anthropic-sdk-python)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- Real Python 的 async/await 教程

---

## 阶段二：Prompt Engineering 实战（第 2 周）

### 核心技巧
| 技巧 | 用途 | 示例场景 |
|------|------|----------|
| System Prompt 设计 | 设定角色、规则、输出格式 | 客服机器人 |
| Few-shot | 给模型看示例再回答问题 | 文本分类 |
| Chain of Thought | 让模型分步推理 | 数学/逻辑题 |
| 结构化输出 | 要求返回 JSON/YAML | 信息提取 |
| Prompt 模板 | Jinja2 / f-string 组合 prompt | 批量生成 |

### 项目：命令行日记助手
功能：
- 写日记 → 自动生成标题和标签
- 查日记 → 按日期/标签/关键词检索
- 周报 → 汇总一周日记，生成摘要
- 本地 JSON 文件存储，无需数据库

**用到的技术**：Prompt 设计、结构化输出（Pydantic）、简单的文件 I/O。

---

## 阶段三：Tool Use / Function Calling（第 3 周）

### 这周要搞懂的东西
- Tool 的 JSON Schema 定义（name、description、parameters）
- Anthropic 的 `tool_use` + `tool_result` 消息流
- OpenAI 的 function calling 流程
- 并行工具调用
- 工具调用中的异常处理与重试

### 标准 Agent 循环
```
用户输入 → LLM 决策（回复 or 调用工具）
                ↓
         工具执行 → 结果回传 → LLM 再决策
                ↓
         最终回复用户
```
实现一个 `while True` 循环驱动上述流程。

### 项目：智能文件管家 Agent
- 工具 1：列出目录文件
- 工具 2：读取文件内容
- 工具 3：搜索文件（grep）
- 工具 4：移动/重命名文件（安全模式：先预览再执行）
- Agent 自己决定工具调用顺序

---

## 阶段四：RAG 检索增强生成（第 4-5 周）

### RAG 全流程
```
文档加载 → 文本切分 → 向量化 → 存入向量库 → 查询时检索 → 注入 Prompt
```

### 分步实战
**第 4 周 — RAG 基础**：
- 文本加载：PyPDF2 / pdfplumber（PDF）、python-docx（Word）
- 文本切分：LangChain TextSplitter 或自己写 `RecursiveCharacterTextSplitter`
- Embedding：OpenAI `text-embedding-3-small` 或本地模型
- 向量库：ChromaDB（零配置，入门首选）
- 检索：相似度搜索 + Top-K

**第 5 周 — RAG 进阶**：
- 混合检索（关键词 + 向量）
- 重排序（Cohere Rerank / Cross-encoder）
- 引用溯源（返回答案 + 出处片段）
- Agentic RAG：Agent 自主判断是否需要检索

### 项目：个人知识库问答系统
- 支持上传 PDF / Markdown / TXT
- 自动索引到 ChromaDB
- 对话式问答，答案带引用
- Web UI（Gradio 或 Streamlit）

### 学习资源
- [ChromaDB 文档](https://docs.trychroma.com/)
- [LlamaIndex 文档](https://docs.llamaindex.ai/) — RAG 框架，按需学习
- [LangChain RAG 教程](https://python.langchain.com/docs/tutorials/rag/)

---

## 阶段五：Memory 系统（第 6 周）

### 三种记忆类型

| 类型 | 存储位置 | 用途 |
|------|----------|------|
| 短期记忆 | 对话历史列表 | 多轮对话上下文 |
| 长期记忆 | 向量数据库 | 跨会话的知识保留 |
| 摘要记忆 | 对话摘要文本 | 超长对话压缩 |

### 实现要点
- 滑动窗口：保留最近 N 轮对话
- 摘要压缩：当对话超过 Token 上限，自动总结早期内容
- 用户画像：从对话历史中提取用户偏好，持久化存储
- 记忆检索：用向量库搜索与当前问题相关的历史记忆

### 项目：升级知识库助手
- 在阶段四的项目上加入 Memory 系统
- 记住用户的提问偏好、知识背景
- 跨会话保持上下文

---

## 阶段六：Agent 框架（第 7 周）

> 原则：先裸写理解原理，再用框架提效。

### 重点学这两个
1. **Anthropic Claude Agent SDK** — 官方套件，与 Claude 深度集成
2. **LangGraph** — 有状态 Agent 编排，比 LangChain 的 Agent 更灵活

### LangGraph 核心概念
- StateGraph：定义 Agent 的状态流转
- Node：每个处理步骤（LLM 调用、工具执行、条件判断）
- Edge：步骤间的连接（普通边 / 条件边）
- Checkpointer：自动持久化状态，支持暂停/恢复

### 对比练习
把阶段三的"文件管家 Agent"分别用裸 SDK 和 LangGraph 实现一遍，对比代码量和灵活性。

### 学习资源
- [LangGraph 官方教程](https://langchain-ai.github.io/langgraph/tutorials/)
- [Anthropic Agent SDK 文档](https://docs.anthropic.com/en/docs/agent-sdk)

---

## 阶段七：工程化实践（第 8-9 周）

### 第 8 周 — 后端架构
- **FastAPI** 构建 API 服务（异步、类型安全、自动文档）
- Streaming 响应（SSE / WebSocket）
- 中间件：认证、限流、日志
- 数据库：SQLite/PostgreSQL 存储用户数据和对话记录
- 任务队列：后台处理耗时操作

### 第 9 周 — 生产必备
- **Token 用量追踪**：每次 API 调用的输入/输出 Token 统计
- **成本控制**：设置单日 Token 上限
- **速率限制**：slowapi 或自实现 Token Bucket
- **Prompt Injection 防护**：输入清洗、角色隔离、内容过滤
- **日志与监控**：structlog + 关键节点日志
- **错误处理**：API 超时重试、降级策略、熔断

### 项目：把前面的 Agent 包装成 API 服务
```
POST /chat          → 对话接口（SSE 流式）
POST /knowledge     → 上传文档
GET  /history       → 对话历史
GET  /stats         → Token 用量统计
```

---

## 阶段八：全栈实战项目（第 10-12 周）

### 终极项目：Python AI 学习助手

一个帮你学 Python 的 AI 应用，功能：

1. **对话问答**：解答 Python 相关问题
2. **代码执行**：Agent 写代码 → 在沙箱中运行 → 返回结果
3. **知识库**：上传 Python 教材/文档，基于资料回答问题
4. **错题本**：自动记录你问过的问题，生成复习卡片
5. **学习路线追踪**：根据你的进度推荐下一步学什么

### 技术栈
```
前端：Streamlit 或 Next.js（如果会前端）
后端：FastAPI + SQLAlchemy
LLM：Anthropic Claude API
向量库：ChromaDB / Qdrant
部署：Railway / Vercel / 自建服务器
```

---

## 核心技术栈总结

```
核心 SDK        anthropic + openai
Web 框架        FastAPI
数据验证        Pydantic v2
向量数据库      ChromaDB（入门）→ Qdrant（生产）
Agent 框架      LangGraph
RAG 框架        LlamaIndex（可选）
前端（轻量）    Streamlit / Gradio
前端（完整）    Next.js / React（如果会 JS）
数据库          SQLite（开发）→ PostgreSQL（生产）
部署            Docker + Railway / Fly.io
```

---

## 学习资源索引

### 官方文档（必读）
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)
- [Anthropic Cookbook](https://github.com/anthropics/anthropic-cookbook) — 大量 Python 示例
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [FastAPI 文档](https://fastapi.tiangolo.com/)
- [Pydantic v2 文档](https://docs.pydantic.dev/latest/)

### 必读文章
- [Building Effective Agents (Anthropic)](https://www.anthropic.com/research/building-effective-agents)
- [LLM Powered Autonomous Agents (Lilian Weng)](https://lilianweng.github.io/posts/2023-06-23-agent/)
- [A Guide to LLM Abstractions](https://www.anthropic.com/research/building-effective-agents#workflow-and-agent)

### 课程
- [DeepLearning.AI — Building Systems with ChatGPT API](https://www.deeplearning.ai/short-courses/building-systems-with-chatgpt/)
- [DeepLearning.AI — LangChain for LLM App Development](https://www.deeplearning.ai/short-courses/langchain-for-llm-application-development/)
- [DeepLearning.AI — Functions, Tools and Agents with LangChain](https://www.deeplearning.ai/short-courses/functions-tools-agents-langchain/)
- [DeepLearning.AI — Building Agentic RAG with LlamaIndex](https://www.deeplearning.ai/short-courses/building-agentic-rag-with-llamaindex/)

### GitHub 项目（必看源码）
- [anthropic-cookbook](https://github.com/anthropics/anthropic-cookbook)
- [openai-cookbook](https://github.com/openai/openai-cookbook)
- [LangGraph examples](https://github.com/langchain-ai/langgraph/tree/main/examples)

---

## 验证标准

| 阶段 | 你应该能独立完成 |
|------|------------------|
| 一 | 从零调用 API，封装多轮对话类 |
| 二 | 用 Prompt 完成分类/提取/摘要，写出日记助手 |
| 三 | 实现 Agent 循环，Agent 能自主调用工具完成任务 |
| 四 | 搭建完整 RAG 管线：文档→向量化→检索→问答 |
| 五 | Agent 跨会话记住用户信息，长对话自动压缩 |
| 六 | 用 LangGraph 复现手写的 Agent 循环 |
| 七 | FastAPI + SSE 流式接口 + Token 追踪上线 |
| 八 | 完整全栈 AI 产品，部署到公网可访问 |
