@echo off
chcp 65001 >nul
title AI Banking Demo Launcher
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe

echo ============================================
echo   AI Banking Agent - 一键启动(全量重启)
echo ============================================
echo.

rem ================= 1) 先停旧进程 =================
rem 踩坑(2026-10-09):旧版是"端口已在监听就跳过"。Python 不会热加载,
rem Next 生产模式也不会——代码改完后直接启动,会对着上一次的旧代码演示。
rem 现在一律先停后起,保证跑的一定是当前代码。停进程按 PID,绝不全杀 node。
rem 连做两轮:若旧版 next dev 的 CLI 父进程把子进程重拉起来,第二轮会再清一次
rem (子进程死透后父进程自己退出)。迁移到 next start 之后这一步自然空转。
echo [停止] 清理旧进程...
for /l %%r in (1,1,2) do (
    for %%p in (8800 8789 8788 3000) do (
        for /f "tokens=5" %%i in ('netstat -ano ^| findstr ":%%p " ^| findstr "LISTENING"') do (
            echo         端口 %%p -^> 停止 PID %%i
            taskkill /F /PID %%i >nul 2>&1
        )
    )
    timeout /t 2 /nobreak >nul
)

rem ================= 1.5) 首次运行自举 =================
rem 需求(2026-10-09,仓库已公开):队友 clone 后只填一个 API Key 就能跑。
rem - venv 缺失            → 友好报错(比一堆 traceback 强),指路 README §7.2
rem - webui\.env.local 缺失 → 调 scripts/setup_env.py 一次式生成
rem   (AUTH_SECRET 本机随机、提示粘贴 Key;生成物被 .gitignore 拦截,无泄露通道)
rem - data\bank.db 缺失    → 自动播种 12 个月演示数据(bank_core.db 自建 data 目录;
rem   此时刚停完旧服务,库文件无占用,不违反"重播种前先停服务"的铁律)
if not exist "%PY%" (
    echo [错误] 找不到 %PY% —— 请先创建虚拟环境并装依赖:
    echo        py -3.12 -m venv .venv
    echo        .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo        详见 README 或 docs\协作与接口契约.md 第七节
    pause
    exit /b 1
)
if not exist "webui\.env.local" (
    echo.
    echo [首次运行] webui\.env.local 不存在,进入一次式配置(全程只差粘贴一个 API Key^)...
    %PY% scripts\setup_env.py
)
if not exist "data\bank.db" (
    echo [首次运行] 演示库 data\bank.db 不存在,播种 12 个月种子数据...
    %PY% -m bank_core.seed
)

rem ================= 2) 后端三服务 =================
rem 必须用 venv 解释器(AGENTS.md 铁律 3:系统 Python312 没装项目依赖)
rem
rem ZAI_* 一律以 webui\.env.local 为准 —— 踩坑(2026-10-09):
rem 环境变量里可能残留过期的 Key/端点(实测曾因旧智谱 Key 余额不足,
rem 对话全部返回 429"余额不足"),而 .env.local 里是当前可用的
rem DeepSeek 配置。若只回填 Key、不回填 ZAI_BASE_URL/ZAI_MODEL,
rem 就会出现"DeepSeek 的 Key 打到智谱端点"这种必挂组合。
if exist "webui\.env.local" (
    for /f "usebackq tokens=1,* delims==" %%a in ("webui\.env.local") do (
        if /i "%%a"=="ZAI_API_KEY"  set "ZAI_API_KEY=%%b"
        if /i "%%a"=="ZAI_BASE_URL" set "ZAI_BASE_URL=%%b"
        if /i "%%a"=="ZAI_MODEL"    set "ZAI_MODEL=%%b"
    )
)
rem 踩坑:cmd 对 if/for 括号块是"整块先解析再执行",块内 %VAR% 在解析期就展开,
rem 所以这行回显必须放在块外,否则永远打印空值(会被误判成回填失败)。
if not "%ZAI_BASE_URL%"=="" echo [配置] ZAI_* 取自 webui\.env.local  端点=%ZAI_BASE_URL%  模型=%ZAI_MODEL%
if "%ZAI_API_KEY%"=="" (
    echo [警告] 未找到 ZAI_API_KEY —— 对话会报"未配置 API Key"。
    echo        请在 webui\.env.local 里配置,或在 webui 界面设置里填 BYOK Key。
    echo.
)

echo [启动] 8800 agent API...
start "agent-api" /min %PY% -m agent.api

echo [启动] 8789 管理后台...
start "admin-api" /min %PY% -m bank_core.admin_api

echo [启动] 8788 网银演示页...
start "web-api" /min %PY% -m bank_core.web_api

rem ================= 3) 前端:生产构建 + next start =================
rem 绝不用 next dev --turbo(AGENTS.md:16GB 机器必 OOM)。构建约 1-2 分钟。
echo.
echo [构建] 前端生产包(约 1-2 分钟,请勿关闭本窗口)...
pushd webui
set NODE_OPTIONS=--max-old-space-size=2048
call npx next build
if errorlevel 1 (
    echo.
    echo [警告] 前端构建失败,将尝试用上一次的产物启动。
)
set NODE_OPTIONS=
echo [启动] 3000 前端(next start)...
rem start 继承当前目录,故不必在 cmd /c 里再套引号(嵌套引号在 cmd 下极易出错)
start "webui" /min cmd /c "npx next start"
popd

echo.
echo 等待服务就绪...
timeout /t 20 /nobreak >nul
start http://127.0.0.1:3000

echo.
echo ============================================
echo   全部启动指令已发出。打开的地址:
echo   前端对话:   http://127.0.0.1:3000
echo   管理后台:   http://127.0.0.1:8789
echo   网银演示:   http://127.0.0.1:8788
echo   演示账号:   13800002233 / Demo@12345
echo   支付密码:   888888
echo ============================================
echo   本窗口可以关闭;停止服务请运行 停止演示.bat
echo.
pause
