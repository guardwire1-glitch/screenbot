@echo off
title ScreenBot Status Checker
cd /d "%~dp0"

echo ========================================================
echo                 ScreenBot Status Checker
echo ========================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ScreenBot.exe' -or ($_.CommandLine -like '*bot.py*' -and $_.Name -like 'python*') }); " ^
    "if ($procs.Count -gt 0) { " ^
    "    Write-Host '--------------------------------------------------------' -ForegroundColor Green; " ^
    "    Write-Host ' [STATUS] ACTIVE and LISTENING' -ForegroundColor Green; " ^
    "    Write-Host '--------------------------------------------------------' -ForegroundColor Green; " ^
    "    foreach ($p in $procs) { " ^
    "        Write-Host (' - Process ID (PID) : ' + $p.ProcessId); " ^
    "        Write-Host (' - Process Name     : ' + $p.Name); " ^
    "    }; " ^
    "    Write-Host ''; " ^
    "    Write-Host 'Press Ctrl + 0 anywhere to capture and answer!' -ForegroundColor Cyan; " ^
    "} else { " ^
    "    Write-Host '--------------------------------------------------------' -ForegroundColor Yellow; " ^
    "    Write-Host ' [STATUS] NOT RUNNING' -ForegroundColor Yellow; " ^
    "    Write-Host '--------------------------------------------------------' -ForegroundColor Yellow; " ^
    "    Write-Host 'ScreenBot is not currently active.'; " ^
    "    Write-Host 'Double-click start_silent.vbs (or START_SCREENBOT.bat) to start it.'; " ^
    "}; " ^
    "Write-Host ''; " ^
    "Write-Host 'CREDENTIAL STATUS:' -ForegroundColor DarkGray; " ^
    "$here = (Get-Location).Path; " ^
    "$src = ''; $key = ''; " ^
    "if ($env:GEMINI_API_KEY) { $src = 'system environment'; $key = $env:GEMINI_API_KEY } " ^
    "elseif ($env:GOOGLE_API_KEY) { $src = 'system environment (GOOGLE_API_KEY)'; $key = $env:GOOGLE_API_KEY } " ^
    "else { " ^
    "    $envFile = Join-Path $here '.env'; " ^
    "    if (Test-Path $envFile) { " ^
    "        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*(GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*(\S+)' } | Select-Object -First 1; " ^
    "        if ($line) { $src = '.env file'; $key = $line.Substring($line.IndexOf('=') + 1).Trim() } " ^
    "    }; " ^
    "    if (-not $key) { " ^
    "        $userCfg = Join-Path $env:APPDATA 'ScreenBot\config.json'; " ^
    "        if (Test-Path $userCfg) { try { $j = Get-Content $userCfg -Raw | ConvertFrom-Json; if ($j.gemini_api_key) { $src = '%%APPDATA%%\ScreenBot\config.json'; $key = $j.gemini_api_key } } catch {} } " ^
    "    }; " ^
    "    if (-not $key) { " ^
    "        $localCfg = Join-Path $here 'config.json'; " ^
    "        if (Test-Path $localCfg) { try { $j = Get-Content $localCfg -Raw | ConvertFrom-Json; if ($j.gemini_api_key) { $src = 'local config.json'; $key = $j.gemini_api_key } } catch {} } " ^
    "    } " ^
    "}; " ^
    "if ($key) { Write-Host (' [OK] API key resolved from ' + $src + ' (' + $key.Length + ' chars).') -ForegroundColor Green } " ^
    "else { " ^
    "    Write-Host ' [FAIL] No API key resolved - every Gemini call will fail.' -ForegroundColor Red; " ^
    "    Write-Host '        Run SETUP.bat to configure your key.' -ForegroundColor Red " ^
    "}; " ^
    "Write-Host ''; " ^
    "if (Test-Path 'screen_bot.log') { " ^
    "    Write-Host 'Recent Log Activity:' -ForegroundColor DarkGray; " ^
    "    Get-Content 'screen_bot.log' -Tail 6 | ForEach-Object { Write-Host ('   ' + $_) -ForegroundColor Gray } " ^
    "}"

echo.
pause
