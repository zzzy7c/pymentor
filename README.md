# Python AI 学习助手

一个用 Python 帮自己学 Python 的应用：文档问答带引用、报错诊断并在沙箱里真的跑一遍修复、
按主题出题并自动批改、根据错题推荐下一步。

**产品原则：宁可回答“文档里没有”，也不编造 API。** 所有解释都必须能追溯到 Python 官方文档或 PEP。

| 版本 | 能力 | 状态 |
|------|------|------|
| v1 | 文档问答 + 引用（`POST /chat` SSE、`POST /knowledge`） | 已实现 |
| v2 | 错误诊断 + 沙箱验证（`POST /diagnose`） | 已实现；沙箱隔离验收见下方“已知限制” |
| v3 | 练习生成 + 自动批改（`POST /exercises`、`.../submit`） | 已实现 |
| v4 | 学习路径 + 记忆 + 配额 + Web UI（`POST /recommend`、`GET /history`、`GET /stats`） | 已实现 |

实现契约见 [AGENTS.md](AGENTS.md)（与 `SPEC.md` 冲突时以它为准），本阶段设计见 [SPEC.md](SPEC.md)。

---

## 架构

```
Streamlit ──HTTP/SSE──> FastAPI router ──> services/*.py
                                                │
                                                ▼
                                     agent/graphs/<版本>.py
                                      （节点，由 LangGraph 调度）
                                                │
                        ┌───────────────────────┼───────────────────────┐
                        ▼                       ▼                       ▼
                  rag/retriever           sandbox/runner        db/repositories
                        │                       │                       │
                    ChromaDB              Docker 容器             SQLite/PG
                                                                            ▲
                                          agent/checkpointer.py ────────────┘
                                          （检查点写同一个库）
```

几条不会妥协的边界：

- **两份状态，一个库**：对话轨迹只存在 LangGraph 检查点里；业务表存用户、提交、评分、错题、
  Token 账本。业务表里不存第二份 `messages`。
- **用户代码只在沙箱容器里执行**：无网络、只读根文件系统、非 root、CPU/内存/时间上限。
  api 进程只通过 Docker API 创建沙箱，从不 `exec()` 用户输入。
- **模型只有一个供应商**（DeepSeek），模型 ID 只在 `config.py` 出现一次；
  工具调用轮次的 `reasoning_content` 由 `llm/deepseek_chat.py` 统一回传。
- **前端不 import `pyassist`**：只经 HTTP 说话，契约由 `tests/test_frontend_contract.py` 守住。

---

## 环境要求

| 项 | 要求 |
|----|------|
| Python | 3.12+ |
| 包管理 | [uv](https://docs.astral.sh/uv/)（`pyproject.toml` + `uv.lock` 是唯一依赖来源） |
| Docker | 跑诊断/练习的沙箱需要 Docker daemon；没有它其余功能仍可用，`/health` 会报 `sandbox_ok=false` |
| 模型密钥 | `DEEPSEEK_API_KEY`（唯一供应商） |

---

## 环境变量

完整清单与注释见 [.env.example](.env.example)。复制后填写：

```bash
cp .env.example .env
```

| 变量 | 默认 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | 空 | 必填，否则调模型的接口返回 `CONFIG_ERROR` |
| `DEEPSEEK_API_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `DATABASE_URL` | `sqlite+aiosqlite:///data/app.db` | 容器内相对 `/app`，落到 `/app/data/app.db`；生产可换 PostgreSQL |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | `structlog` JSON 日志 |
| `EMBEDDING_MODEL_ID` | `config.DEFAULT_EMBEDDING_MODEL_ID`（BAAI/bge-m3） | 换模型必须删 `data/chroma` 重跑 ingest |
| `DAILY_TOKEN_LIMIT` | 空 = `config.DEFAULT_DAILY_TOKEN_LIMIT`（500,000） | `0` = 关闭熔断，仅本地调试；这是**软上限**，并发下可能略超 |
| `PYASSIST_API_BASE_URL` | `http://127.0.0.1:8000` | 前端调后端的地址；compose 里是 `http://api:8000` |
| `DOCKER_GID` | `0` | 仅 compose：宿主 Docker socket 的组 GID，非 root 容器用户需要它 |
| `DOMAIN` | 无（必填） | 仅生产 compose：Caddy 申请证书用的域名 |

---

## 开发启动（本机）

```bash
# 1. 依赖：web 组是前端专用（streamlit + httpx），不装会把前端依赖漏掉
uv sync --group web

# 2. 配置
cp .env.example .env    # 填 DEEPSEEK_API_KEY

# 3. 建库（首次；幂等，可重跑）。业务表 + LangGraph 检查点/Store 表都在这一步建
uv run alembic upgrade head

# 4. 语料与索引（首次；幂等，可重跑）
#    先抓官方文档与 PEP，再向量化。全量 ingest 耗时较长，务必看日志确认完成。
uv run python -m pyassist.rag.ingest --fetch

# 5. 沙箱镜像（宿主构建，compose 不负责）
docker build -f src/pyassist/sandbox/Dockerfile.sandbox -t pyassist-sandbox:1 .

# 6. 后端
uv run uvicorn pyassist.api.main:app --reload

# 7. 前端（另一个终端）
uv run streamlit run frontend/streamlit_app.py
```

打开 <http://127.0.0.1:8501>。后端接口文档在 <http://127.0.0.1:8000/docs>。

只用容器跑开发环境：

```bash
# 先按上面第 5 步构建沙箱镜像
export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)   # Linux；Docker Desktop 可省略
docker compose up --build
# 首次还要建库（幂等）
docker compose exec api alembic upgrade head
```

---

## 生产部署

单台 Linux VM + Docker Compose + Caddy。**不用 Railway/Fly 之类 PaaS**：沙箱需要宿主
Docker daemon，托管平台不提供“容器里再跑容器”。

```bash
# 宿主：构建沙箱镜像
docker build -f src/pyassist/sandbox/Dockerfile.sandbox -t pyassist-sandbox:1 .

# 让非 root 的 api 容器（uid 10001）能写数据卷、连 Docker socket
sudo chown -R 10001:10001 data
export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)

# 启动 api + web + caddy
export DOMAIN=your.domain
docker compose -f deploy/prod.compose.yml up -d --build
```

发布前检查、威胁模型、回滚与演示脚本见 [deploy/README.md](deploy/README.md)。

> **“公网已上线”不予承诺**：本仓库交付配置与清单，是否真的能从外网访问需要人工验证，
> 结果应记录在 `docs/evals/` 中并写明时间与地址。

---

## 演示脚本（约 3 分钟）

| 时间 | 动作 | 要让人看到什么 |
|------|------|----------------|
| 0:00–0:25 | 提问“`asyncio.gather` 的异常怎么处理？” | 逐字输出 + 可点击的官方文档链接；检索不到时会直说“文档中没有” |
| 0:25–1:00 | 贴一个真实 `AttributeError` Traceback | 错误类型、解释、修复代码，以及沙箱里的实际运行结果 |
| 1:00–1:50 | 选“装饰器”出题，先提交错误版本，再提交正确版本 | 失败用例与可解释反馈；通过后分数由测试决定 |
| 1:50–2:30 | 打开“历史与进度”，点“给我推荐” | 推荐引用刚才做错的题目 id，并给出下一步主题、理由与文档链接 |
| 2:30–3:00 | 打开统计 | 今日 token、缓存命中、成本、配额与错题分布 |

录屏细节（每步停顿、说什么）见 [deploy/README.md](deploy/README.md) §5。

---

## 评估跑分

评估集是回归基线，**只能新增，不能修改已有用例**（`AGENTS.md` §4）。

```bash
# v1：Recall@5 + 引用准确率（需要真实索引与 API key）
uv run python -m pyassist.evals.run_rag_eval

# v2：错误类型识别 + 修复后沙箱执行通过率
uv run python -m pyassist.evals.run_v2_eval

# v3：参考答案自洽率（需要 Docker；离线 CI 不跑）
uv run python -m pyassist.evals.run_v3_eval --self-consistency

# v3：真实生成 + 自校验（需要 key 与 Docker）
uv run python -m pyassist.evals.run_v3_eval --generate --per-topic 5
```

评估属于批量任务，**安排在 DeepSeek 非峰值时段**（UTC 周一至周五 01:00–04:00 与
06:00–10:00 之外半价，见 `AGENTS.md` §1）。

离线回归（不触网、不需要 key）：

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src/pyassist
uv run pytest                      # 默认 -m 'not docker'
```

需要 Docker 的验收单独跑：`uv run pytest -m docker -v`。

---

## 已知限制

1. **单用户**。`user_id` 固定为 `local`，没有鉴权。公网部署只允许自己使用；
   上传到公网前请加访问控制（Caddy `basic_auth` 或 VPN）。
2. **配额是软上限**。用量账本在请求结束后写入，并发请求可能略超日限额；
   这不是计费系统，只用于个人成本控制。
3. **沙箱需要 Docker**。开发机上没有 Docker 时，诊断/练习会降级为
   `sandbox_ok=false`，修复结果标为“未验证”，而不是假装通过。
4. **沙箱隔离验收仍未完成**（SPEC R1）：恶意用例（死循环、fork 炸弹、读 `/etc/passwd`、
   `os.system("rm -rf /")`）的拦截结果需在具备 Docker 的机器上用 `-m docker` 验证。
   在验证完成前，相关通过率一律标记“未验证”，不填数字。
5. **Caddy 之后的 SSE 未实测**（SPEC R8）：`flush_interval -1` 与
   `X-Accel-Buffering: no` 已配置，但仍需一次真实反代验证。
6. **单机部署**。检查点与业务库都在同一台机器的一个 SQLite 文件里；
   要水平扩容得先迁移到 PostgreSQL 并处理检查点共享。
7. **API 镜像较大**：`sentence-transformers` 会带入 CPU 版 torch；
   模型权重挂 `data/hf` 卷，重建镜像不重下。
8. **没有 CI**。回归靠本地命令；`.github/workflows/ci.yml` 是待定项（SPEC 待定项 10）。

---

## 目录

```
src/pyassist/
├── config.py              # 唯一配置入口（含模型 ID、价格表）
├── llm/                   # 模型工厂、reasoning_content 修复、Trace 记账、prompt 模板
├── rag/                   # 语料抓取、ingest、检索、引用
├── agent/                 # 图状态、检查点、Store、工具、四张图
├── sandbox/               # 用户代码执行（唯一出口）与沙箱镜像
├── db/                    # SQLAlchemy 模型与唯一 SQL 出口
├── services/              # 业务流程（qa / diagnose / exercise / progress / memory / usage）
├── api/                   # FastAPI 应用、路由、响应契约、异常映射
└── evals/                 # 评估集与跑分器
frontend/                  # Streamlit（只调 HTTP）
deploy/                    # 生产 compose、Caddyfile、runbook
scripts/                   # 运维脚本（检查点清理、密钥扫描）
tests/                     # 离线测试；-m docker 单独分组
```
