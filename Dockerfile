FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl passwd util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 flux \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin flux

# 依赖单列一层：改代码不重装依赖，NAS 上重建更快
COPY server/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY server/ ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 数据与 DSH 家目录都落在挂载卷里，容器重建不丢 Bot 与例程
ENV HOME=/data/home \
    FLUX_DATABASE_URL=sqlite+aiosqlite:////data/flux.db \
    FLUX_DSH_HOME=/data/dsh-home \
    FLUX_UPLOADS_DIR=/data/uploads \
    FLUX_DELIVERIES_DIR=/data/deliveries \
    FLUX_DSH_WORKSPACE=/data/dsh-ws \
    FLUX_WORKSPACE_ROOT=/data/workspace \
    FLUX_BOT_PORT=8600 \
    FLUX_DSH_MCP_URL=http://127.0.0.1:8600/mcp

VOLUME ["/data"]
EXPOSE 8600

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -fsS http://127.0.0.1:8600/api/v1/health || exit 1

# 不设 USER：entrypoint 以 root 修正 /data 属主后，用 setpriv 降权到 uid 1000 再启动服务。
ENTRYPOINT ["/entrypoint.sh"]
