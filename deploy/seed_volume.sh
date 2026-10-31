#!/usr/bin/env bash
# 把仓库里的 ./data（语料 / 向量库 / 业务库 / 检查点）灌进 Docker 命名卷 pyassist_data。
#
# 为什么需要这一步：方案 B 的数据放在命名卷里而不是宿主目录 ——
# Windows 宿主目录经 9p 进容器后，WAL 模式的 SQLite 打不开（见 tunnel.compose.yml 头注释）。
# 卷落在 Docker Desktop VM 的 ext4 上，WAL 正常。
#
# **幂等**：卷里已有 app.db 就只打印体积，不覆盖。
# 用法（WSL 或 Git Bash 均可）：
#   bash deploy/seed_volume.sh
#   PYASSIST_VOLUME=other bash deploy/seed_volume.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_ROOT}/data"
VOLUME="${PYASSIST_VOLUME:-pyassist_data}"
APP_UID="${PYASSIST_APP_UID:-10001}"

# 只需要一个带 sh/coreutils 的镜像做搬运。沙箱镜像本就是 python:3.12-slim 系，
# 它几乎一定已经在本机（阶段七构建过），省掉一次几百 MB 的拉取。
IMAGE="${PYASSIST_SEED_IMAGE:-}"
if [ -z "${IMAGE}" ]; then
    for candidate in pyassist-sandbox:1 python:3.12-slim; do
        if docker image inspect "${candidate}" >/dev/null 2>&1; then
            IMAGE="${candidate}"
            break
        fi
    done
fi
if [ -z "${IMAGE}" ]; then
    echo "缺少可用的搬运镜像：请先构建 pyassist-sandbox:1（deploy/tunnel.md §2）" >&2
    exit 2
fi

if [ ! -d "${SRC}" ]; then
    echo "源目录不存在：${SRC}" >&2
    exit 2
fi

docker volume inspect "${VOLUME}" >/dev/null 2>&1 || docker volume create "${VOLUME}" >/dev/null

if docker run --rm -v "${VOLUME}:/d" --entrypoint sh "${IMAGE}" -c 'test -f /d/app.db' >/dev/null 2>&1; then
    echo "卷 ${VOLUME} 里已有 app.db，跳过灌入（要重灌先删卷：docker volume rm ${VOLUME}）"
    docker run --rm -v "${VOLUME}:/d" --entrypoint sh "${IMAGE}" -c 'du -sh /d'
    exit 0
fi

echo "从 ${SRC} 灌入卷 ${VOLUME}（镜像 ${IMAGE}）…"
# 以 root 复制再整体 chown：源文件在 Windows 侧属主不可靠，直接把属主定死成容器的 app 用户。
docker run --rm -u 0:0 \
    -v "${VOLUME}:/dst" \
    -v "${SRC}:/src:ro" \
    --entrypoint sh "${IMAGE}" \
    -c "cp -a /src/. /dst/ && chown -R ${APP_UID}:${APP_UID} /dst && du -sh /dst && ls -1 /dst"

echo "完成。卷 ${VOLUME} 现在就是容器看到的 /app/data。"
