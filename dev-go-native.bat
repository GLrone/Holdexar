@echo off
setlocal
title Holdexar Go native (dev)
cd /d "%~dp0"

rem Build serverd + windowhost from current source (no Python involved).
rem Data dir follows holdexar-dev-home.txt; serverd watches Go sources and
rem rebuilds on save; the window reloads on the new bootstrap ticket.

set "DEVBIN=%~dp0desktop\windowhost\bin"
if not exist "%DEVBIN%" mkdir "%DEVBIN%"

pushd server-go
go build -o "%DEVBIN%\serverd.exe" ./cmd/serverd
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" goto :fail

pushd desktop\windowhost
go build -ldflags "-H=windowsgui" -o "%DEVBIN%\windowhost.exe" .
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" goto :fail

set "HOLDEXAR_INSTALL_ROOT=%~dp0"
set "HOLDEXAR_SERVER_EXE=%DEVBIN%\serverd.exe"
set "HOLDEXAR_DEV_WATCH=%~dp0server-go"
set "HOLDEXAR_PORT=28767"
set "HOLDEXAR_LOCK_PORT=28966"
start "" "%DEVBIN%\windowhost.exe"
exit /b 0

:fail
echo [dev-go-native] build failed, not started
exit /b 1
