@echo off
setlocal
title Holdexar Go (dev)
cd /d "%~dp0"
set "HOLDEXAR_INSTALL_ROOT=%CD%"
if not defined HOLDEXAR_PORT set "HOLDEXAR_PORT=28767"
set "HOLDEXAR_LOCK_PORT=28966"
set "HOLDEXAR_DEV_WATCH=%HOLDEXAR_INSTALL_ROOT%\server-go"

:pick_temp
set "DEVBIN=%TEMP%\holdexar-dev-bin-%RANDOM%-%RANDOM%-%RANDOM%"
if exist "%DEVBIN%" goto :pick_temp
mkdir "%DEVBIN%"
if errorlevel 1 exit /b 1

pushd "%HOLDEXAR_INSTALL_ROOT%\server-go"
call go build -o "%DEVBIN%\serverd.exe" ./cmd/serverd
set "EXITCODE=%ERRORLEVEL%"
popd
if not "%EXITCODE%"=="0" goto :done

pushd "%HOLDEXAR_INSTALL_ROOT%\desktop\windowhost"
call go build -o "%DEVBIN%\Holdexar.exe" .
set "EXITCODE=%ERRORLEVEL%"
popd
if not "%EXITCODE%"=="0" goto :done

set "HOLDEXAR_SERVER_EXE=%DEVBIN%\serverd.exe"
call "%DEVBIN%\Holdexar.exe" %*
set "EXITCODE=%ERRORLEVEL%"

:done
if exist "%DEVBIN%" rmdir /s /q "%DEVBIN%"
if %EXITCODE% neq 0 pause
exit /b %EXITCODE%
