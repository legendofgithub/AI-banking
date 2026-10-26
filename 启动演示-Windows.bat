@echo off
chcp 65001 >nul
title AI Banking Demo Launcher
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

rem Four services are launched as hidden background processes by
rem scripts\demo_launcher.py. This window only shows startup progress.
rem Closing it does not stop the services. Use the stop batch file to stop them.
if not exist "%PY%" (
    echo [ERROR] %PY% not found.
    echo Create the venv and install dependencies first:
    echo   py -3.12 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo See README or docs for details.
    pause
    exit /b 1
)

"%PY%" scripts\demo_launcher.py %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo [ERROR] Startup incomplete. Check logs\*.err.log.
pause
exit /b %RC%
