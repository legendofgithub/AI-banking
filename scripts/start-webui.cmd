@echo off
rem 隐藏窗口启动 webui(next start),供 ZCode 会话/并行会话复用;
rem 与 demo_launcher.py 的 webui 日志同路径追加。
rem 注意:分离启动时用 Start-Process cmd.exe -ArgumentList '/c','"<本脚本绝对路径>"'
rem (数组式参数,路径含空格必须这样传,单字符串内联会静默失败)
cd /d "%~dp0..\webui"
call npx next start >> "..\logs\webui.log" 2>> "..\logs\webui.err.log"
