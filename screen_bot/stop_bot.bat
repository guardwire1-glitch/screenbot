@echo off
title Stop ScreenBot
cd /d "%~dp0"

echo Stopping any running ScreenBot instances...
taskkill /F /IM ScreenBot.exe /T >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bot.py*' -and $_.Name -like 'python*' }; " ^
    "if ($procs) { " ^
    "    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('[OK] Terminated ScreenBot PID: ' + $_.ProcessId) }; " ^
    "} else { " ^
    "    Write-Host '[INFO] No running ScreenBot process found.'; " ^
    "}"

rem taskkill cannot run Python cleanup, so a hotkey may have left Ctrl/Shift
rem logically down. Force-release them so clicks behave normally again.
if exist "%~dp0fix_stuck_keys.bat" (
    call "%~dp0fix_stuck_keys.bat" /nopause
)

echo Done.
ping 127.0.0.1 -n 2 >nul

