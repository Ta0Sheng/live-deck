#!/usr/bin/env bash
# live-deck 一键安装（macOS / Linux）
#
#   ./scripts/setup.sh              # CPU 字幕
#   GPU=1 ./scripts/setup.sh        # CUDA 字幕
#   SKIP_MODELS=1 ./scripts/setup.sh
#   DEV=1 ./scripts/setup.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
VENV="$ROOT/.venv"

if [ ! -x "$VENV/bin/python" ]; then
    echo "==> 创建 venv：$VENV"
    "$PYTHON" -m venv "$VENV"
fi

echo "==> 安装依赖"
"$VENV/bin/python" -m pip install -U pip

if [ "${DEV:-0}" = "1" ]; then
    "$VENV/bin/python" -m pip install -e ".[dev]"
elif [ "${GPU:-0}" = "1" ]; then
    "$VENV/bin/python" -m pip install -e ".[asr-gpu]"
else
    "$VENV/bin/python" -m pip install -e ".[asr]"
fi

# 桌面窗口是可选件；装不上就用浏览器
if [ "${DEV:-0}" != "1" ]; then
    "$VENV/bin/python" -m pip install -e ".[window]" \
        || echo "pywebview 装不上，live-deck app 会自动用浏览器打开"
fi

if [ ! -f config.toml ]; then
    echo "==> 生成 config.toml"
    "$VENV/bin/python" -m live_deck.cli init
fi

if [ "${SKIP_MODELS:-0}" != "1" ]; then
    echo "==> 下载字幕模型（约 300 MB）"
    "$VENV/bin/python" -m live_deck.cli models install
fi

echo "==> 体检"
"$VENV/bin/python" -m live_deck.cli doctor || true

echo
echo "完成。启动："
echo "    ./scripts/run.sh          # 浏览器模式"
echo "    ./scripts/run.sh demo     # 演示模式"
