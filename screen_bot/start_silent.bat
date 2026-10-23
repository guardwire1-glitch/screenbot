@echo off
cd /d "%~dp0"

:: 1. Check if running inside an unextracted zip temporary folder
echo "%CD%" | findstr /i "\.zip \\temp\\" >nul
if %errorlevel% equ 0 (
    echo [ERROR] Please EXTRACT the zip file before running ScreenBot!
    pause
    exit /b 1
)

:: 2. PREFERRED: the frozen ScreenBot.exe (no Python required)
if exist "ScreenBot.exe" (
    wscript.exe "%~dp0start_silent.vbs"
    exit /b
)

:: 3. FALLBACK: source mode - find a Python interpreter
set PYTHONW_EXE=.venv\Scripts\pythonw.exe
if not exist "%PYTHONW_EXE%" set PYTHONW_EXE=..\.venv\Scripts\pythonw.exe

if not exist "%PYTHONW_EXE%" (
    where pythonw >nul 2>nul
    if %errorlevel% equ 0 set PYTHONW_EXE=pythonw.exe
)

:: 4. If no Python environment and no exe, run SETUP automatically
if not exist "%PYTHONW_EXE%" (
    echo [ERROR] ScreenBot.exe was not found in this folder.
    echo Please re-extract the archive fully, then run SETUP.bat.
    echo.
    pause
    exit /b 1
)

:: 5. Start silently via VBScript
wscript.exe "%~dp0start_silent.vbs"
