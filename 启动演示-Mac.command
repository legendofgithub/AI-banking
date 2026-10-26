#!/bin/bash
# AI Banking macOS 一键启动(与 启动演示-Windows.bat 同口径:全量重启 + 后台静默)。
# 用法:双击运行,或终端执行 ./启动演示-Mac.command [--skip-build] [--no-open]
# 注意:本文件必须保持 LF 行尾、可执行位(git update-index --chmod=+x)。
set -u
cd "$(dirname "$0")" || exit 1

PY=.venv/bin/python

if [ ! -x "$PY" ]; then
    echo "[ERROR] 未找到虚拟环境 $PY"
    echo "请先创建并安装依赖:"
    echo "  python3.12 -m venv .venv"
    echo "  .venv/bin/python -m pip install -r requirements.txt"
    echo "  cd webui && npm install   # 或 pnpm install"
    printf "\n按回车关闭窗口..."
    read -r
    exit 1
fi

"$PY" scripts/demo_launcher.py "$@"
RC=$?
printf "\n启动器已退出(代码 %s)。按回车关闭窗口..." "$RC"
read -r
exit "$RC"
