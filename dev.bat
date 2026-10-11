@echo off
setlocal
title Holdexar (dev)

rem Dev launcher. Runtime data never lands in the git worktree. The data
rem directory is resolved by server\app\core\paths.py (env var, portable.flag,
rem an existing data\holdexar.db, a holdexar-dev-home.txt pointer, then the
rem machine-level holdexar-dev dir). Dev and packaged never share a dir: the
rem packaged app puts its data beside its own program directory.
rem Setting HOLDEXAR_DATA_DIR here would outrank all of those, so it is not set.

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
