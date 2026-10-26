#!/bin/bash
# AI Banking macOS 一键停止(与 停止演示-Windows.bat 同口径:只按演示端口的 PID 杀,
# 绝不全杀 node/python)。双击运行,或终端执行 ./停止演示-Mac.command
# 注意:本文件必须保持 LF 行尾、可执行位(git update-index --chmod=+x)。
cd "$(dirname "$0")" || exit 1

echo "正在停止 AI Banking 全部服务..."
for port in 8800 8789 8788 3000; do
    pids=$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null)
    for pid in $pids; do
        echo "  停止端口 $port (PID $pid)"
        kill -9 "$pid" 2>/dev/null
    done
done

echo
echo "全部服务已停止。"
printf "按回车关闭窗口..."
read -r
