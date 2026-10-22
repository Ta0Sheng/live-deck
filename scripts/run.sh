#!/usr/bin/env bash
# 启动 live-deck（macOS / Linux）
#   ./scripts/run.sh           # 浏览器模式
#   ./scripts/run.sh demo      # 演示模式
#   ./scripts/run.sh serve     # 只起服务

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/.venv/bin/live-deck"

if [ ! -x "$BIN" ]; then
    echo "还没安装。先跑 ./scripts/setup.sh" >&2
    exit 1
fi

case "${1:-app}" in
    demo)  exec "$BIN" demo ;;
    serve) exec "$BIN" serve ;;
    *)     exec "$BIN" app ;;
esac
