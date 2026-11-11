#!/bin/sh
# 容器启动：先把库迁到最新，再起 Bot 服务。迁移幂等，重复启动无副作用。
set -e

# 所有持久化目录都落在 /data 卷里；~/.flux（密钥库主密钥等）也随 HOME 一起进卷。
mkdir -p "${FLUX_UPLOADS_DIR:-/data/uploads}" \
    "${FLUX_DELIVERIES_DIR:-/data/deliveries}" \
    "${FLUX_DSH_WORKSPACE:-/data/dsh-ws}" \
    "${FLUX_WORKSPACE_ROOT:-/data/workspace}" \
    "$HOME/.flux"
chmod 700 "$HOME/.flux"

python -m alembic -x db_url="$FLUX_DATABASE_URL" upgrade head
exec uvicorn flux.bot_node.main:app \
    --host 0.0.0.0 \
    --port "${FLUX_BOT_PORT:-8600}" \
    --proxy-headers \
    --forwarded-allow-ips='*'
