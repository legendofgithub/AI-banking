@echo off
chcp 65001 >nul
title AI Banking Demo Launcher
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe

echo ============================================
echo   AI Banking Agent - 一键启动
echo ============================================
echo.

rem ---- 8800 agent API ----
netstat -ano | findstr ":8800 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [跳过] 8800 agent API 已在运行
) else (
    echo [启动] 8800 agent API...
    start "agent-api" /min %PY% -m agent.api
)

rem ---- 8789 管理后台 ----
netstat -ano | findstr ":8789 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [跳过] 8789 管理后台已在运行
) else (
    echo [启动] 8789 管理后台...
    start "admin-api" /min %PY% -m bank_core.admin_api
)

rem ---- 8788 网银演示页 ----
netstat -ano | findstr ":8788 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [跳过] 8788 网银演示页已在运行
) else (
    echo [启动] 8788 网银演示页...
    start "web-api" /min %PY% -m bank_core.web_api
)

rem ---- 3000 前端 ----
netstat -ano | findstr ":3000 " | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [跳过] 3000 前端已在运行
) else (
    echo [启动] 3000 前端(编译需 20-40 秒)...
    start "webui" /min cmd /c "cd webui && set NODE_OPTIONS=--max-old-space-size=2048&& pnpm dev"
)

echo.
echo 等待服务就绪...
timeout /t 15 /nobreak >nul
start http://127.0.0.1:3000

echo.
echo ============================================
echo   全部启动指令已发出。打开的地址:
echo   前端对话:   http://127.0.0.1:3000
echo   管理后台:   http://127.0.0.1:8789
echo   演示账号:   13800002233 / Demo@12345
echo   支付密码:   888888
echo ============================================
echo   本窗口可以关闭;停止服务请运行 停止演示.bat
echo.
pause
