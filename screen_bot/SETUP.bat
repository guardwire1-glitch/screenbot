@echo off
setlocal EnableDelayedExpansion
title ScreenBot - One-Click Device Setup
cd /d "%~dp0"

echo ========================================================
echo        ScreenBot Setup for New Devices ^& Friends
echo ========================================================
echo.

:: 1. Check if user is running directly from inside a ZIP file
echo "%CD%" | findstr /i "\.zip \\temp\\" >nul
if %errorlevel% equ 0 (
    echo [ERROR] You are running directly inside a ZIP preview folder.
    echo.
    echo Please EXTRACT the zip first:
    echo   1. Close this window.
    echo   2. Right-click the zip file and click "Extract All..."
    echo   3. Open the extracted folder and run SETUP.bat again.
    echo.
    pause
    exit /b 1
)

:: 2. Verify the runnable payload. This distribution ships a frozen,
::    dependency-free ScreenBot.exe - Python is NOT required.
if not exist "ScreenBot.exe" (
    echo [ERROR] ScreenBot.exe was not found in "%CD%".
    echo.
    echo Please re-extract the archive fully, then run SETUP.bat again.
    echo.
    pause
    exit /b 1
)
echo [OK] Found ScreenBot.exe  (no Python installation required)

:: 3. Prepare the per-user configuration store.
::    The key is stored in %APPDATA%\ScreenBot\config.json and NEVER in the
::    config.json sitting next to this script, because that file is the one
::    that gets zipped and shared with friends. Putting the key there is
::    what caused the original "GEMINI_API_KEY is not set." failure.
set "USER_CFG=%APPDATA%\ScreenBot\config.json"
echo.
if not exist "%USER_CFG%" (
    echo [1/3] Creating your personal settings file...
    if not exist "%APPDATA%\ScreenBot" mkdir "%APPDATA%\ScreenBot" 2>nul
    echo {"gemini_api_key": "", "model": "gemini-3.5-flash-lite", "lecture_injection_mode": "paste", "auto_scan_enabled": false} > "%USER_CFG%"
    echo       %USER_CFG%
) else (
    echo [1/3] Found your personal settings file:
    echo       %USER_CFG%
)

:: 4. Check whether an API key already resolves from any tier.
::    Precedence: system env var -> .env -> %APPDATA% -> local config.json
echo.
echo [2/3] Checking API key...
set "KEYFOUND="
if not "%GEMINI_API_KEY%"=="" set "KEYFOUND=1"
if not "%GOOGLE_API_KEY%"=="" set "KEYFOUND=1"
if not defined KEYFOUND if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        set "NAME=%%A"
        set "NAME=!NAME: =!"
        if /i "!NAME!"=="GEMINI_API_KEY" if not "%%B"=="" set "KEYFOUND=1"
        if /i "!NAME!"=="GOOGLE_API_KEY" if not "%%B"=="" set "KEYFOUND=1"
    )
)
if not defined KEYFOUND if exist "%USER_CFG%" (
    for /f "usebackq tokens=1,* delims=:" %%A in ("%USER_CFG%") do (
        set "NAME=%%A"
        set "NAME=!NAME: =!"
        set "NAME=!NAME:"=!"
        set "VAL=%%B"
        set "VAL=!VAL: =!"
        set "VAL=!VAL:"=!"
        set "VAL=!VAL:,=!"
        if /i "!NAME!"=="gemini_api_key" if not "!VAL!"=="" set "KEYFOUND=1"
    )
)
if not defined KEYFOUND if exist "config.json" (
    for /f "usebackq tokens=1,* delims=:" %%A in ("config.json") do (
        set "NAME=%%A"
        set "NAME=!NAME: =!"
        set "NAME=!NAME:"=!"
        set "VAL=%%B"
        set "VAL=!VAL: =!"
        set "VAL=!VAL:"=!"
        set "VAL=!VAL:,=!"
        if /i "!NAME!"=="gemini_api_key" if not "!VAL!"=="" set "KEYFOUND=1"
    )
)

if defined KEYFOUND (
    echo       [OK] An API key is already configured on this device.
) else (
    echo       [ACTION REQUIRED] No Gemini API key found on this device.
    echo.
    echo       Get a free key in seconds from: https://aistudio.google.com/
    echo.
    set /p NEWKEY="       Paste your Gemini API key here (Enter to skip): "
    if not "!NEWKEY!"=="" (
        set "KEYFOUND=1"
        echo.
        echo       Saving to %USER_CFG% ...
        powershell -NoProfile -ExecutionPolicy Bypass -Command ^
            "$p=Join-Path $env:APPDATA 'ScreenBot\config.json'; " ^
            "$j=$null; " ^
            "if(Test-Path $p){try{$t=Get-Content $p -Raw; $j=ConvertFrom-Json -InputObject $t}catch{}}; " ^
            "if(-not $j){$j=New-Object PSObject}; " ^
            "$j ^| Add-Member -NotePropertyName 'gemini_api_key' -NotePropertyValue $env:NEWKEY -Force; " ^
            "if(-not ($j.PSObject.Properties['model'])){$j ^| Add-Member -NotePropertyName 'model' -NotePropertyValue 'gemini-3.5-flash-lite' -Force}; " ^
            "if(-not ($j.PSObject.Properties['lecture_injection_mode'])){$j ^| Add-Member -NotePropertyName 'lecture_injection_mode' -NotePropertyValue 'paste' -Force}; " ^
            "if(-not ($j.PSObject.Properties['auto_scan_enabled'])){$j ^| Add-Member -NotePropertyName 'auto_scan_enabled' -NotePropertyValue $false -Force}; " ^
            "$null = New-Item -ItemType Directory -Force -Path (Split-Path $p) -ErrorAction SilentlyContinue; " ^
            "$out = ConvertTo-Json -InputObject $j -Depth 10; " ^
            "Set-Content -Path $p -Value $out -Encoding UTF8; " ^
            "Write-Host ('       [OK] Key saved to ' + $p)"
        set "NEWKEY="
    ) else (
        echo       [WARNING] No key entered. ScreenBot will still start, but every
        echo                Gemini call will fail with "GEMINI_API_KEY is not set."
        echo                Run SETUP.bat again once you have your key.
    )
)

:: 5. Verification test
echo.
echo [3/3] Verifying installation...
"%~dp0ScreenBot.exe" --selftest

echo.
echo ========================================================
echo       [SUCCESS] ScreenBot is Ready on this Computer
echo ========================================================
echo.
echo How to use:
echo   1. Double-click "start_silent.vbs" (or START_SCREENBOT.bat) to run invisibly.
echo   2. You will see the ScreenBot icon in your System Tray (near the clock).
echo   3. Press Ctrl + 0 anywhere on screen to answer questions and copy to clipboard
echo.
set /p LAUNCH="Would you like to start ScreenBot now? (Y/N): "
if /i "%LAUNCH%"=="Y" (
    start "" start_silent.vbs
    echo [OK] ScreenBot started in background. Look for the tray icon.
)
ping -n 4 127.0.0.1 >nul
endlocal
