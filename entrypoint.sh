#!/bin/sh
# 容器启动：先把库迁到最新，再起 Bot 服务。迁移幂等，重复启动无副作用。
set -e

# 所有持久化目录都落在 /data 卷里；~/.flux（密钥库主密钥等）也随 HOME 一起进卷。
DATA_DIRS="${FLUX_UPLOADS_DIR:-/data/uploads} ${FLUX_DELIVERIES_DIR:-/data/deliveries} ${FLUX_RUNTIME_WORKSPACE:-/data/runtime-ws} ${FLUX_WORKSPACE_ROOT:-/data/workspace} ${FLUX_RUNTIME_HOME:-/data/runtime-home}"

mkdir -p $DATA_DIRS "$HOME" "$HOME/.flux"
chmod 700 "$HOME/.flux"

# 镜像里的服务账号：uid/gid 均为 1000。以 root 启动时先修正卷属主，再降权运行；
# 已经以 1000 启动时不再 chown 整个卷（大工作区会很慢，也超出调用方预期）。
if [ "$(id -u)" = "0" ]; then
    # /data 下的数据库文件等历史状态也交给服务账号，避免旧卷升级后不可写。
    if [ -d /data ]; then
        chown -R 1000:1000 /data
    fi
    # 目录若被配置到 /data 之外，也一并修正属主。
    for dir in $DATA_DIRS "$HOME"; do
        case "$dir" in
            /data|/data/*) ;;
            *) chown -R 1000:1000 "$dir" ;;
        esac
    done
    set -- setpriv --reuid=1000 --regid=1000 --init-groups
else
    set --
fi

"$@" python -m alembic -x db_url="$FLUX_DATABASE_URL" upgrade head
exec "$@" uvicorn flux.bot_node.main:app \
    --host 0.0.0.0 \
    --port "${FLUX_BOT_PORT:-8600}" \
    --proxy-headers \
    --forwarded-allow-ips='*'
