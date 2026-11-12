FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Node 20 + Claude Code CLI：Bot 的 Agent 底座（npm 全局装 claude）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# 依赖单列一层：改代码不重装依赖，NAS 上重建更快
COPY server/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY server/ ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 数据与运行期目录都落在挂载卷里，容器重建不丢 Bot 与例程
ENV FLUX_DATABASE_URL=sqlite+aiosqlite:////data/flux.db \
    FLUX_RUNTIME_HOME=/data/runtime-home \
    FLUX_BOT_PORT=8600

VOLUME ["/data"]
EXPOSE 8600

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -fsS http://127.0.0.1:8600/api/v1/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
