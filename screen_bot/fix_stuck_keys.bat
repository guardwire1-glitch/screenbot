@echo off
title ScreenBot - Release Stuck Keys
cd /d "%~dp0"

echo ============================================================
echo  ScreenBot - Release Stuck Ctrl / Shift / Alt / Windows
echo ============================================================
echo.
echo Use this if a normal click is opening new windows/links or
echo duplicating items (a "stuck Ctrl" key). No reboot required.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_stuck_keys.ps1"

echo.
if /I not "%~1"=="/nopause" pause
