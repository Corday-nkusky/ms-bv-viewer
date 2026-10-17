#!/bin/sh
# macOS / Linux 启动脚本：把本目录当成扫雷目录的下一层，自动打开浏览器
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    exec python3 server.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python server.py "$@"
else
    echo "Python 3 not found. Install it from https://www.python.org/downloads/"
    exit 1
fi
