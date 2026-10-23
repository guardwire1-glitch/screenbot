@echo off
cd /d "%~dp0"
echo ========================================================
echo         ScreenBot Pro - Live Code Follower Active
echo ========================================================
echo.
echo Starting ScreenBot...
start "" "%~dp0ScreenBot.exe"
echo.
echo ScreenBot is now running in your Windows System Tray!
echo A "ScreenBot is running" badge will appear bottom-right for 60 seconds.
echo No beep is played. Click the badge to dismiss it early.
echo.
echo ANSWER HOTKEYS:
echo   - Ctrl + 0               : Direct Answer (concise, copied to clipboard)
echo   - Ctrl + 9               : Explained Answer (answer + short rationale)
echo   - Ctrl + 8               : Paraphrase (anti-plagiarism rewrite)
echo   - Ctrl + 2               : Exam Highlight and Solve (also Ctrl+Shift+V)
echo.
echo LIVE CODE FOLLOWER HOTKEYS:
echo   * CTRL + SHIFT + F        : Sync Lecture Code Now (writes bound file,
echo                              or pastes at caret if direct sync is off)
echo                              (also Ctrl + \ or Ctrl + 3)
echo   - Ctrl + Shift + \        : Force Full Code Re-Sync
echo   - Ctrl + Alt + \          : Pause / Resume Auto-Scan
echo   - Ctrl + 4                : AI Lecture Note-Taker (Cornell notes)
echo.
echo LIVE CODE FOLLOWER LANGUAGES:
echo   Auto-detects Java, C, C++, C#, Python, JavaScript/TypeScript, PHP,
echo   Ruby, Go, Rust, Kotlin, Swift, SQL, HTML/CSS, and shell.
echo   Tray menu -^> Subject Preset to pin a language manually.
echo.
echo OTHER:
echo   - ESC                     : Emergency Stop (abort typing / actions)
echo   - Ctrl + Alt + Z          : Undo Last Code Edit (20 snapshots)
echo   - Ctrl + Shift + Q        : Exit ScreenBot
echo.
echo IF CLICKS START OPENING LINKS/NEW WINDOWS AFTER EXITING:
echo   - Run fix_stuck_keys.bat (or tray menu: Release Stuck Keys).
echo     No reboot required.
echo.
echo This window will close automatically in 4 seconds...
timeout /t 4 >nul
