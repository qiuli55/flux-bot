#!/bin/sh
# 容器启动：先把库迁到最新，再起 Bot 服务。迁移幂等，重复启动无副作用。
set -e

python -m alembic -x db_url="$FLUX_DATABASE_URL" upgrade head
exec uvicorn flux.bot_node.main:app \
    --host 0.0.0.0 \
    --port "${FLUX_BOT_PORT:-8600}" \
    --proxy-headers \
    --forwarded-allow-ips='*'
