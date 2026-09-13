@echo off
setlocal
title Holdexar

cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] python not found in PATH.
    echo         Install Python 3.11+ from https://www.python.org/downloads/
    echo         and check "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

python run.py %*
set EXITCODE=%errorlevel%

if %EXITCODE% neq 0 (
    echo.
    echo [EXIT] code %EXITCODE% - scroll up for the error above.
    pause
)
endlocal