@echo off
title Install ScreenBot to Windows Startup
cd /d "%~dp0"

echo Adding ScreenBot to Windows Startup folder...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$vbsPath = (Resolve-Path 'start_silent.vbs').Path; " ^
    "$startupDir = [Environment]::GetFolderPath('Startup'); " ^
    "$shortcutPath = Join-Path $startupDir 'ScreenBot.lnk'; " ^
    "$wsh = New-Object -ComObject WScript.Shell; " ^
    "$shortcut = $wsh.CreateShortcut($shortcutPath); " ^
    "$shortcut.TargetPath = 'wscript.exe'; " ^
    "$shortcut.Arguments = '\"' + $vbsPath + '\"'; " ^
    "$shortcut.WorkingDirectory = (Resolve-Path '.').Path; " ^
    "$shortcut.Description = 'ScreenBot Silent Background Assistant'; " ^
    "$shortcut.Save(); " ^
    "Write-Host ('[OK] ScreenBot shortcut created at: ' + $shortcutPath);"

echo.
echo ScreenBot will now start automatically whenever your PC logs in.
pause
