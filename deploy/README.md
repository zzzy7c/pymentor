# 部署与运维手册

本文写三件事：这套东西为什么长这样、上线前必须确认什么、出事怎么退回去。
配置本身见 `prod.compose.yml` / `Caddyfile`，日常启动见仓库根目录 [README.md](../README.md)。

---

## 1. 形态：单台 Linux VM + Compose + Caddy

```
              80/443
  Internet ──────────> caddy
                        ├── /api/*  ──> api:8000   （FastAPI，SSE）
                        └── *       ──> web:8501   （Streamlit，WebSocket）

  api 容器 ── /var/run/docker.sock ──> 宿主 Docker daemon ──> sandbox 容器
                                                                （无网络/只读/非 root）
```

**为什么不是 Railway / Fly.io**：沙箱要创建容器，而托管平台不提供 Docker-in-Docker。
这不是偏好，是能力约束。也**不要**为了省事把沙箱并入 api 容器 —— 那等于把“执行任意用户代码”
和“持有密钥、数据库、Docker socket”放进同一个进程边界。

---

## 2. 威胁模型（必读）

### 2.1 `docker.sock` 暴露 ≈ 宿主 root

api 容器挂载 `/var/run/docker.sock` 之后，**容器内任何能调用 Docker API 的代码都可创建
特权容器、挂载宿主根目录、最终拿到宿主 root**。Docker socket 不是“一个方便的 API”，
它是一张宿主 root 的通行证。

因此边界必须这样理解：

| 组件 | 信任级别 | 能做什么 |
|------|----------|----------|
| `api` 进程 | **受信**（等同宿主 root） | 持有 API key、业务库、Docker socket |
| `sandbox` 容器 | **不受信**（执行用户/模型代码） | 无网络、只读根文件系统、非 root、无 capabilities、资源与时间上限 |
| `web` 容器 | 受信但最小 | 只装 streamlit + httpx，只经 HTTP 调 api |

**api 容器永不执行用户代码。** 所有用户/模型产出的代码只能经 `sandbox/runner.py`
用固定参数创建容器执行：`network_disabled`、`read_only`、`cap_drop=["ALL"]`、
`no-new-privileges`、`pids_limit`、`mem_limit`、`nano_cpus`、超时后 kill。
代码经容器 **stdin 流**送进去（`start()` 前 attach、`start()` 后写入），不依赖任何
宿主路径挂载 —— 只读根文件系统下 `put_archive` 会被 daemon 拒绝，见 ADR 0004「修订记录」。

降低风险的具体做法：

1. 不要在这台机器上跑别的业务；VM 只承载本项目。
2. 不给 api 容器额外 capabilities，不挂宿主目录（除 `data/`）。
3. Docker socket 的组权限只授予必要的 GID（`DOCKER_GID`），不要 `chmod 666`。
4. 保持 Docker Engine 与基础镜像更新；镜像漏洞逃逸后，socket 就是最后一道门。
5. 若未来加多用户/公网注册，**先拆掉 socket 依赖**（换远程沙箱服务或 gVisor），
   否则任何一处 RCE 都直接是宿主 root。

### 2.2 没有鉴权

`user_id` 固定为 `local`，没有登录、没有会话鉴权。**这套身份不是安全边界。**

- 只允许单人使用。
- 公网暴露前，至少在 Caddy 上加 `basic_auth` 或放到 VPN / IP allowlist 后面。
- 不要把这个地址发给别人，也不要让人把“上传代码”当成公共服务用。

### 2.3 密钥与数据

- `DEEPSEEK_API_KEY` 只经 `env_file: ../.env` 进入容器；`.env` 不进 Git、不进镜像
  （`.dockerignore` 已排除）。
- 日志不记录密钥、不记录完整用户代码（只记哈希与长度）。
- `data/` 里有对话检查点、提交代码、Token 账本 —— 属于个人数据，备份时按敏感数据处理。

### 2.4 沙箱仍然不是“绝对安全”

无网络 + 只读根文件系统 + 非 root + 资源上限能挡住常见的死循环、fork 炸弹、写宿主文件，
但**内核漏洞、Docker daemon 漏洞不在防护范围内**。这是单 VM 自托管的现实上限，
写在这里而不是假装它不存在。

---

## 3. 发布清单

按顺序执行；`[ ]` 全部打勾之前，不要对外声称“已上线”。

### 3.1 首次部署

- [ ] VM 安装 Docker Engine 与 Compose 插件（`docker compose version` 可用）
- [ ] 克隆仓库到 `/opt/pyassist`（路径随意，下文以此为例）
- [ ] `cp .env.example .env`，填写 `DEEPSEEK_API_KEY`；按需改 `DAILY_TOKEN_LIMIT`
- [ ] 在**宿主**构建沙箱镜像：
      `docker build -f src/pyassist/sandbox/Dockerfile.sandbox -t pyassist-sandbox:1 .`
      （镜像 tag 必须与 `config.SANDBOX_IMAGE` 一字不差）
- [ ] 准备数据目录并交给 uid 10001：
      `mkdir -p data/hf && sudo chown -R 10001:10001 data`
- [ ] 取得 socket 的组 GID：`export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)`
- [ ] 域名 A/AAAA 记录已指向本机；`export DOMAIN=your.domain`
- [ ] `docker compose -f deploy/prod.compose.yml config` 无报错
- [ ] `docker compose -f deploy/prod.compose.yml up -d --build`
- [ ] `/health` → `status=ok`、`database_ok=true`、`sandbox_ok=true`
      （生产 compose **不发布** 8000，只能从容器内探：
      `docker compose -f deploy/prod.compose.yml exec api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"`）
- [ ] 首次语料 index 已建好：`docker compose -f deploy/prod.compose.yml exec api python -m pyassist.rag.ingest --fetch`
- [ ] 公网访问 `https://$DOMAIN`，走完“提问 → 引用 → 报错 → 沙箱 → 做题 → 批改 → 推荐 → 统计”
- [ ] 把访问结果（时间、地址、是否成功）写进 `docs/evals/阶段八-跑分.md`

### 3.2 每次更新

- [ ] `uv lock` 与 `pyproject.toml` 同步提交；镜像构建用 `--frozen`
- [ ] 离线回归：`uv run ruff check . && uv run mypy --strict src/pyassist && uv run pytest`
- [ ] `docker compose -f deploy/prod.compose.yml up -d --build`
- [ ] 观察 `/health` 与日志至少 10 分钟，再结束维护窗口

### 3.3 明确“未验证”的项目

以下结果**没有**在具备 Docker 的机器上实测过，验收时必须保留“未验证”字样，
不得填估算值：

- [ ] 沙箱恶意用例拦截率（死循环、fork 炸弹、读 `/etc/passwd`、`os.system("rm -rf /")`）
      —— 跑 `uv run pytest -m docker tests/test_sandbox_docker.py -v`
- [ ] 修复后代码在沙箱内的实际通过率（v2 目标 ≥70%）
- [ ] 生成练习的参考答案自洽率 100%（v3）
- [ ] Caddy 之后 SSE 是否逐字上屏（目标：不缓冲）
- [ ] api 镜像实际体积与首次模型下载耗时
- [ ] 公网真实可达性

---

## 4. 运维

### 4.1 备份

只需要 `data/`：

```bash
sudo tar czf pyassist-data-$(date +%F).tar.gz data/
```

`data/app.db` 同时是业务库与 LangGraph 检查点（WAL 模式）；热备请用
`sqlite3 data/app.db ".backup 'backup.db'"`，不要直接 `cp` 正在写的库。

### 4.2 检查点膨胀

带大工具结果的检查点会持续增长。按 `SPEC` 待定项 11，本阶段**不自动清理**，提供手动脚本：

```bash
docker compose -f deploy/prod.compose.yml exec api python scripts/prune_threads.py --days 30
```

先不带 `--apply` 看将删除多少，再加 `--apply`。

### 4.3 日志与成本

- 日志是 JSON（`structlog`），按 `request_id` 串一次调用。
- `GET /stats` 可看今日 token、缓存命中、成本、配额与错题分布。
- 成本异常先看 `cached_tokens`：前缀被时间戳/随机 id 污染会让缓存命中率归零，
  输入单价差 50 倍。

### 4.4 回滚

```bash
git checkout <上一个可用提交>
docker compose -f deploy/prod.compose.yml up -d --build
```

数据库迁移只向前；本项目当前不新增迁移。若未来有迁移，回滚前先备份 `data/`。

---

## 5. 演示脚本（约 3 分钟）

录屏时按这个顺序走，每一步都停下来让观众看清结果：

1. **0:00–0:25 提问与引用**：问“`asyncio.gather` 的异常怎么处理？”，展示逐字输出，
   结束时出现可点击的官方文档链接；补一句“检索不到时它会直说文档中没有”。
2. **0:25–1:00 报错诊断**：贴一个真实的 `AttributeError` Traceback，展示错误类型、
   解释、修复后的代码，以及沙箱里的实际运行结果（通过/超时/被拦截）。
3. **1:00–1:50 做练习**：选“装饰器”，出题；故意写错一版提交，展示失败的测试用例与
   可解释反馈（错在哪、为什么、看哪份文档）；改成正确版本再提交，展示通过。
4. **1:50–2:30 推荐**：进入“历史与进度”，点“给我推荐”，展示它引用了刚才做错的题目 id，
   并给出下一步主题、理由与文档链接。
5. **2:30–3:00 统计**：展示今日 token、缓存命中、成本、配额与错题分布，
   说明评估安排在非峰值时段。

---

## 6. 故障排查

| 现象 | 先看什么 |
|------|----------|
| `/health` 里 `database_ok=false` | `data/` 属主是否 10001；`DATABASE_URL` 是否指向 `/app/data/app.db` |
| `sandbox_ok=false` | 宿主是否构建了 `pyassist-sandbox:1`；容器用户是否有 socket 权限（`DOCKER_GID`） |
| 所有模型请求 401/402 | `.env` 是否真的被 `env_file` 读到；DeepSeek 余额 |
| 前端显示“API 不可用” | `web` 的 `PYASSIST_API_BASE_URL` 是否为 `http://api:8000`；api 是否 healthy |
| 流式变成一次性吐出 | Caddy 是否加载了 `flush_interval -1`；后端是否设置了 `X-Accel-Buffering: no` |
| 成本突然上升 | 日志里 `cached_tokens` 是否接近 0；prompt 前缀是否被动态内容污染 |
