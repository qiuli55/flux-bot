#!/usr/bin/env sh
# 清空 Playwright MCP sidecar 持久化的浏览器 profile（cookies / localStorage /
# IndexedDB / 登录态）。只删除 /home/node/profiles/<profile id> 子目录，
# 同一 playwright-home 卷里的 /home/node/output 会保留。
#
# 用法（在仓库根目录，或任意位置直接执行本脚本）：
#   scripts/reset-browser-profile.sh
#   FLUX_BROWSER_PROFILE_ID=alice scripts/reset-browser-profile.sh
#
# 需要本机已安装 docker compose，并能访问本仓库的 compose 文件。
set -eu

cd "$(dirname "$0")/.."

PROFILE_ID="${FLUX_BROWSER_PROFILE_ID:-default}"
case "$PROFILE_ID" in
  "" | *[!A-Za-z0-9_.-]*)
    echo "非法 FLUX_BROWSER_PROFILE_ID: '$PROFILE_ID'（只允许 A-Za-z0-9_.-）" >&2
    exit 2
    ;;
esac

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.browser.yml"

# 先停掉正在使用该 profile 的浏览器进程，避免删除运行中的 Chromium 用户目录。
$COMPOSE stop playwright-mcp
# 一次性容器复用同一命名卷；以镜像默认的 node(1000) 用户删除子目录。
$COMPOSE run --rm --no-deps --entrypoint sh playwright-mcp \
  -c "rm -rf '/home/node/profiles/$PROFILE_ID'"
# 重新拉起（up -d 对尚未创建过的容器也适用）。
$COMPOSE up -d playwright-mcp

echo "已清空浏览器 profile: /home/node/profiles/$PROFILE_ID"
