@echo off
title Remove ScreenBot from Windows Startup
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$startupDir = [Environment]::GetFolderPath('Startup'); " ^
    "$shortcutPath = Join-Path $startupDir 'ScreenBot.lnk'; " ^
    "if (Test-Path $shortcutPath) { " ^
    "    Remove-Item $shortcutPath -Force; " ^
    "    Write-Host '[OK] ScreenBot removed from Startup.'; " ^
    "} else { " ^
    "    Write-Host '[INFO] No ScreenBot startup shortcut found.'; " ^
    "}"

echo Done.
ping 127.0.0.1 -n 2 >nul

