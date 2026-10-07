@echo off
chcp 65001 >nul
title AI Banking Demo Stopper
echo 正在停止 AI Banking 全部服务...

for %%p in (8800 8789 8788 3000) do (
    for /f "tokens=5" %%i in ('netstat -ano ^| findstr ":%%p " ^| findstr "LISTENING"') do (
        echo   停止端口 %%p (PID %%i)
        taskkill /F /PID %%i >nul 2>&1
    )
)

echo.
echo 全部服务已停止。
pause
