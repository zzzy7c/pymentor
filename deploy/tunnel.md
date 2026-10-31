# 方案 B：本机 + Cloudflare Tunnel 部署手册

不租 VM，用这台 Windows 机器跑 compose，靠 cloudflared 的一条**出站**长连接拿到公网 https 地址。
本文是 `deploy/README.md` 的可选分支：那里描述的是"一台 Linux VM + Caddy 申请证书"，
这里是"没有公网 IP、没有域名、也不想开入站端口"时的做法。

配置在 `tunnel.compose.yml` / `Caddyfile.tunnel` / `seed_volume.sh`，**不要**改
`prod.compose.yml`。

---

## 1. 形态

```
 浏览器 ──https──> Cloudflare 边缘 ──隧道(出站)──> cloudflared
                                                        │ http（compose 内网）
                                                        ▼
                                                      caddy:80
                                        ┌───────────────┴───────────────┐
                                  /api/* → api:8000                * → web:8501
                                                    │
                                       /var/run/docker.sock → 宿主 daemon → sandbox 容器
```

与生产的**同**：同一份 `Dockerfile.api` / `Dockerfile.web`、同一套路由、同样挂 socket、
同样一份 `data`（语料 53M + 向量库 199M + 业务库 + 检查点）。

与生产的**异**，逐条都是代价：

| 差异 | 代价 | 补偿 |
|------|------|------|
| 没有 Automatic HTTPS | 本机不能签证书 | 证书由 Cloudflare 边缘终止，明文只在 compose 内网 |
| 数据在命名卷 `pyassist_data`，不在 `./data` | 容器数据与仓库里的 `./data` 是两份 | `seed_volume.sh` 灌入；导出命令见 §6 |
| 隧道地址每次重启都变（Quick Tunnel） | 不能对外长期挂一个固定地址 | 要固定就换 named tunnel（§7） |
| 机器关机/休眠 = 服务下线 | 不是 7×24 | 演示/自用足够 |

> 为什么数据不能直接用 `./data`：Windows 宿主目录经 9p 进容器后，WAL 模式的 SQLite
> 会 `disk I/O error`（同目录新建库却成功，所以只在你打开已有 `app.db` 时才现形）。
> 命名卷落在 Docker Desktop VM 的 ext4 上，WAL 正常 —— 这是实测结论，不是猜测。

## 2. 前置（一次性）

本机已核实的部分（2026-10-30）：

- Docker Desktop **29.8.1**，Compose **v5.5.1**，WSL 发行版 `Ubuntu` 运行中、Docker 集成已开
- 沙箱镜像 **`pyassist-sandbox:1` 已存在**（177 MB）—— 无需重新构建
- `./data` 已就绪：`app.db`（含 30 张表）+ `chroma` 199 MB + `corpus` 53 MB

需要你确认/执行的：

```bash
# 在 WSL 里（Windows 侧 Git Bash 也能跑，但 socket 组 GID 不一样，见下）
cd /mnt/c/Users/19077/Desktop/python助手

export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)   # 本机实测 = 1001
```

- 从 **WSL** 跑：socket 在容器内是 `root:docker`，`DOCKER_GID` 取上面的值（1001）。
- 从 **Windows** 跑（Git Bash/PowerShell）：socket 在容器内是 `gid 0`，
  需要 `export DOCKER_SOCKET=//var/run/docker.sock` 且 `DOCKER_GID=0`。
  **推荐前者**，与 `deploy/README.md` 的口径一致。

### 2.1 镜像拉取（本机网络实测，2026-10-30）

这台机器**直连 Docker Hub / ghcr.io 会卡死**（实测 `python:3.12-slim` 一个 25 MB 的层
在 2.10 MB 处停住，200 秒零进展）。能用的路子是走镜像站拉下来再打回原 tag：

```bash
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker tag  docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim

docker pull docker.1panel.live/library/caddy:2.8-alpine      # 这个站对 library/python 返回 403
docker tag  docker.1panel.live/library/caddy:2.8-alpine caddy:2.8-alpine

docker pull ghcr.nju.edu.cn/astral-sh/uv:0.11.16
docker tag  ghcr.nju.edu.cn/astral-sh/uv:0.11.16 ghcr.io/astral-sh/uv:0.11.16

docker pull docker.m.daocloud.io/cloudflare/cloudflared:latest
docker tag  docker.m.daocloud.io/cloudflare/cloudflared:latest cloudflare/cloudflared:latest
```

**这四条已经在本机执行过了**，所以 `up --build` 不会再卡在拉取上。
换机器/清过镜像后要重做一遍，否则构建会在 `FROM python:3.12-slim`
或 `COPY --from=ghcr.io/astral-sh/uv:...` 那里无限等待（不报错，只是不动）。

可选的鉴权（`deploy/README.md` §2.2：没有登录、`user_id` 固定为 `local`）。
隧道地址是公开可访问的，哪怕它是随机的。要开的话：

```bash
export BASIC_AUTH_USER=pyassist
export BASIC_AUTH_HASH='粘贴下面命令的输出'     # 单引号！bcrypt 里的 $ 会被 shell 展开
docker run --rm caddy:2.8-alpine caddy hash-password --plaintext '你的密码'
export CADDYFILE=Caddyfile.tunnel-auth
```

## 3. 启动

```bash
cd /mnt/c/Users/19077/Desktop/python助手
export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
# 宿主端口可配：本机 8000/8501 常被"直跑的那套"占着，别去停它
export PYASSIST_API_PORT=18000 PYASSIST_WEB_PORT=18501 PYASSIST_HTTP_PORT=8080

# 1) 把 ./data 灌进命名卷（幂等：卷里已有 app.db 就跳过）
bash deploy/seed_volume.sh

# 2) 先验配置，再看有没有插值错误
docker compose -f deploy/tunnel.compose.yml config >/dev/null && echo "compose OK"

# 3) 起栈（首次要拉基础镜像 + 装 torch，耗时取决于网络）
docker compose -f deploy/tunnel.compose.yml up -d --build

# 4) 等 api 健康（web 会一直等到它 healthy 才启动，这是有意的）
docker compose -f deploy/tunnel.compose.yml ps
curl -s http://127.0.0.1:8000/health | python -m json.tool
```

`/health` 要看到 `status: "ok"`、`database_ok: true`、`sandbox_ok: true`。
`degraded` 先看 §5。

拿隧道地址：

```bash
docker compose -f deploy/tunnel.compose.yml logs cloudflared \
  | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1
```

浏览器打开它即可。**这个地址每次 `up` 都会变**。

> **本机连不上 HuggingFace，所以不会"下载"**（见 §4.5）：模型权重是从本机已有缓存
> 预置进卷里的，首次检索只是从磁盘加载，约 8–15 秒。

### 4.5 模型权重从哪来（本机无法下载，必须预置）

实测（2026-10-30/31）：Windows 宿主与容器内**都连不上** `huggingface.co` 和 `hf-mirror.com`
（8 秒超时）。而 `rag/embedder.py` 是惰性加载，第一次检索才去拿 `bge-m3` ——
不预置的话，**第一次提问会直接失败**，不是慢。

本机 Windows 上已有完整缓存（`%USERPROFILE%\.cache\huggingface\hub\models--BAAI--bge-m3`，
4.34 GB，含 `all-MiniLM-L6-v2`）。灌进卷：

```bash
docker run --rm -u 0:0 -v pyassist_data:/dst \
  -v "C:/Users/19077/.cache/huggingface/hub:/src:ro" --entrypoint sh pyassist-sandbox:1 \
  -c 'mkdir -p /dst/hf && cp -a /src /dst/hf/hub && chown -R 10001:10001 /dst/hf'
```

验证（应输出 `dim 1024`）：

```bash
docker compose -f deploy/tunnel.compose.yml exec -T api python -c "
import time
t=time.time()
from pyassist.config import settings
from pyassist.rag.embedder import get_embedder
print('load', round(time.time()-t,1), 'dim', get_embedder(settings.embedding_model_id).dimension)"
```

实测：默认 **15.1 s**（含一次失败的联网探测）；`HF_HUB_OFFLINE=1` 时 **7.8 s**。
所以跑分表里"首次 embedding 下载耗时"应记为"**不适用：无法访问 HF，模型预置；
容器内首次加载 7.8–15.1 s**"，不要填一个假的下载时长。

## 4. 验收清单

对照 `deploy/README.md` §3.1，逐条换成隧道形态：

- [ ] `docker compose -f deploy/tunnel.compose.yml config` 无报错
- [ ] `up -d --build` 后 `ps` 显示 api/web/caddy/cloudflared 全 running，api healthy
- [ ] `/health` → `status=ok`、`database_ok=true`、`sandbox_ok=true`
- [ ] **不需要**跑 `rag/ingest.py --fetch`：卷里已有索引。只有要刷新语料时才跑
      （且要记得 `--fetch` 会重写 `data/corpus`，见 `AGENTS.md` §4 禁区）
- [ ] 公网地址可访问：`curl -s https://<隧道域名>/api/health`
- [ ] 走完"提问 → 引用 → 贴报错 → 沙箱 → 做题 → 批改 → 推荐 → 统计"
- [ ] **SSE 逐字上屏**（不是转圈 30 秒后一次性吐出）。这一步必须实测并写下结论：
      ```bash
      curl -N -X POST https://<隧道域名>/api/chat \
        -H 'content-type: application/json' \
        -d '{"question":"asyncio.gather 的异常怎么处理？"}'
      ```
      终端里应看到 `event:` / `data:` 一行行**陆续**出现；若十几秒后整段一起刷出来，
      就是被隧道缓冲了 —— 记成"未通过"，不要写成"看起来没问题"
- [ ] 记下 `docker image ls pyassist-api pyassist-web` 的实际体积
- [ ] 记下首次 embedding 下载耗时（首次提问的墙钟时间，或从日志时间戳算）
- [ ] 把时间、地址、是否成功写进 `docs/evals/阶段八-跑分.md` §9

## 5. 故障排查

| 现象 | 先看什么 |
|------|----------|
| `cloudflared` 反复重启、日志 `failed to request quick Tunnel` | 本机到 Cloudflare 的出站被拦。试 `command` 里加 `--protocol http2`；仍不行就换 ngrok/另一张网络 |
| 隧道地址打开是 502/1033 | `docker compose ... logs caddy`：caddy 没起来或 web 还没 healthy |
| 页面一直 "Connecting…" / 白屏 | Streamlit 的 WebSocket 没打通。先确认 `http://127.0.0.1:8501` 本机可用，再查 caddy 的 `flush_interval` |
| `/health` 里 `sandbox_ok=false` | `DOCKER_GID` 不对，或 `DOCKER_SOCKET` 没设（从 Windows 跑时必须是 `//var/run/docker.sock`） |
| `/health` 里 `database_ok=false` | 卷没灌（`bash deploy/seed_volume.sh`）或卷属主不是 10001 |
| 所有模型请求 401 | `.env` 没被 `env_file` 读到，或 `environment:` 把 `DEEPSEEK_API_KEY` 覆盖成空串 |
| 报错 `disk I/O error` | 用了宿主目录而不是命名卷 —— 回到 `tunnel.compose.yml` 的卷定义 |
| 第一次提问超时 | 正在下 `bge-m3`，看 api 日志；前端超时是 600 s，够 |
| `up` 报 `ports are not available: ... /forwards/expose returned unexpected status: 500` | 宿主端口被占用（本机 8000/8501 是直跑那套）。用 `PYASSIST_API_PORT` / `PYASSIST_WEB_PORT` 换端口，别去杀进程 |
| 构建时在下 2.5 GB 的 `nvidia-*` / CUDA 轮子 | **已知缺陷**：`uv.lock` 里 `torch` 的来源仍是 `pypi.org`（CUDA 版），`pyproject.toml` 的 `[tool.uv.sources] torch = { index = "pytorch-cpu" }` 没有体现在锁文件里，`uv sync --frozen` 只认锁文件。镜像因此大出约 4 GB。修法是重锁：`uv lock --upgrade-package torch` 后核对 `uv.lock` 里 torch 的 `source` 变成 `pytorch-cpu`。**本次部署没有动锁文件**（改锁会连带重解析其它版本，属于独立变更），记在这里而不是"看起来没毛病" |

## 6. 数据备份与导出

容器数据在卷 `pyassist_data` 里。**热备**用 SQLite 自带的 `.backup`，不要 `cp` 正在写的库：

```bash
# 1) 库内备份（api 镜像里有 python + sqlite3）
docker compose -f deploy/tunnel.compose.yml exec api python -c \
  "import sqlite3; s=sqlite3.connect('/app/data/app.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close()"

# 2) 连同语料/向量库一起导出到 Windows 侧的 ./backups
mkdir -p backups
docker run --rm -v pyassist_data:/d -v "$PWD/backups:/b" --entrypoint sh pyassist-sandbox:1 \
  -c 'cp -a /d/. /b/ && du -sh /b'
```

要把卷写回仓库的 `./data`（换回本机直跑时用）：把上面两行里的 `/b` 换成 `/backup`、
`-v "$PWD/data:/backup"` 即可。

## 7. 固定域名（可选）

Quick Tunnel 的地址每次重启都变。要固定的地址需要 Cloudflare 账号 + 一个域名
（域名不必托管在 Cloudflare，但要走它的 DNS）：

```bash
cloudflared tunnel login                       # 浏览器里授权
cloudflared tunnel create pyassist
cloudflared tunnel route dns pyassist pyassist.example.com
```

然后把 compose 里 `cloudflared` 的 `command` 换成带配置的形态
（`tunnel --config /etc/cloudflared/config.yml run pyassist`，并把 `config.yml`
与凭据 JSON 挂进容器，`ingress` 指向 `http://caddy:80`）。
**这条路没在本机验证过**，写在这里是为了不让你以为"Quick Tunnel 就是全部"。

## 8. 关闭与清理

```bash
# 停栈，保留数据卷（下次 up 回来历史还在）
docker compose -f deploy/tunnel.compose.yml down

# 连数据一起删 —— 不可恢复，先做 §6 的备份
docker compose -f deploy/tunnel.compose.yml down -v
docker volume rm pyassist_data
```

关机/休眠 = 隧道断开、地址失效。要长期在线就别用方案 B。

## 9. 分工：已经做完的 / 需要你做的

**已经做完并实测通过（2026-10-30）**：

- 新增 `deploy/tunnel.compose.yml`、`deploy/Caddyfile.tunnel`、`deploy/Caddyfile.tunnel-auth`、
  `deploy/seed_volume.sh`、`deploy/tunnel.md`；`deploy/README.md` §1 加了指向
- 两份 Caddyfile 都过 `caddy validate`；`docker compose config` 无报错
- 镜像：`pyassist-tunnel-api` **19.6 GB**（CUDA torch，见故障表）、
  `pyassist-tunnel-web` 1.35 GB、`pyassist-sandbox:1` 177 MB
- 数据卷 `pyassist_data` 已从 `./data` 灌入（251 MB，`app.db` 30 张表、chroma、corpus 齐全）
- 栈已跑起来：api healthy；`/health` 在**本机 18000、Caddy 8080、公网隧道**三处都返回
  `status=ok`、`database_ok=true`、`sandbox_ok=true`、`model_check_ok=true`
- api 容器能经 socket 连到宿主 daemon 并取到沙箱镜像（`run_code` / `run_tests` 的前提）
- 宿主 8000/8501 已被本机直跑的服务占用，因此端口改为 `18000 / 18501 / 8080`

**发生过的一次事故（已处置，记录在此）**：
构建 `uv.lock` 里的 CUDA 版 torch 时 C: 盘被写满（0 字节可用），Docker Desktop 的
`docker_data.vhdx`（33 GB）报 `EXT4-fs ... potential data loss` 并起不来。处置：停 Docker →
删损坏的 vhdx → 把 `%LOCALAPPDATA%\Docker` 迁到 `D:\DockerData`（junction，
C: 回收约 33 GB，D: 还剩 100 GB 以上）→ 重建全部镜像与数据卷。
**下次重建前先确认目标盘有 20 GB 以上余量。**

**需要你手动做的（只有这些）**：

1. 打开隧道地址，走完 §4 的验收链路（**首次 `/chat` 会先下 ~2.2 GB 的 `bge-m3`，
   卡几分钟是正常的**，那正是要记的"首次下载耗时"）
2. 实测 SSE 是否逐字上屏，结论写进 §4 与跑分表
3. 把隧道地址与时间、SSE 结论、首次模型下载耗时填进 `docs/evals/阶段八-跑分.md` §9
   （镜像体积和 `/health` 那几行已经填好了）
4. 决定要不要开 §2 的 basic_auth（隧道地址是公开的）
5. 用完 `docker compose -f deploy/tunnel.compose.yml down` 停掉，别让这台机器长期对外
