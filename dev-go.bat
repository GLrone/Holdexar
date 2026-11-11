@echo off
setlocal
title Holdexar Go (dev)
set "HOLDEXAR_INSTALL_ROOT=%~dp0"
cd /d "%~dp0"

set "HOLDEXAR_SERVER_EXE="
set "HOLDEXAR_DEV_WATCH=%~dp0server-go"
set "HOLDEXAR_LOCK_PORT=28966"
call python run.py --port 28767 %*
set "EXITCODE=%ERRORLEVEL%"
if %EXITCODE% neq 0 pause
exit /b %EXITCODE%
