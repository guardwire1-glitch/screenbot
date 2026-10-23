@echo off
title ScreenBot (Debug Console)
cd /d "%~dp0"

echo ========================================================
echo                  ScreenBot - Debug Mode
echo ========================================================
echo.

:: The shipped folder contains a frozen build (ScreenBot.exe) with no bot.py,
:: so the old launcher (which ran "bot.py") could never work here.
if not exist "ScreenBot.exe" (
    echo [ERROR] ScreenBot.exe not found in "%CD%".
    echo Please re-extract the archive fully, then run again.
    pause
    exit /b 1
)

echo [1/3] Running subsystem self-test...
echo.
"%~dp0ScreenBot.exe" --selftest
echo.

echo [2/3] Starting ScreenBot...
start "" "%~dp0ScreenBot.exe"
echo [OK] ScreenBot started. Live log streams below.
echo.

echo [3/3] Streaming screen_bot.log  (Ctrl+C to stop streaming)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Path 'screen_bot.log' -Tail 40 -Wait"

echo.
echo [INFO] Log streaming ended.
pause
