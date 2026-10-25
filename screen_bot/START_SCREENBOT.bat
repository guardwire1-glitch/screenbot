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
echo A separate status icon shows the language and Auto-Scan state for as
echo long as ScreenBot runs. Startup/hotkey beeps follow "sound_feedback"
echo (on in the built-in defaults; set it to false to mute).
echo.
echo ANSWER HOTKEYS:
echo   - Ctrl + 0               : Direct Answer (concise, copied to clipboard)
echo   - Ctrl + 9               : Explained Answer (answer + short rationale)
echo   - Ctrl + 8               : Paraphrase (anti-plagiarism rewrite)
echo   - Ctrl + 2               : Exam Highlight and Solve (also Ctrl+Shift+V)
echo.
echo LIVE CODE FOLLOWER HOTKEYS:
echo   * CTRL + SHIFT + F        : Bind the focused VS Code-family or Visual
echo                              Studio file and sync (disk IDEs need
echo                              target_file_path in config.json)
echo   - Ctrl + \ (or Ctrl + 3) : Sync New Lecture Lines Now
echo   - Ctrl + Shift + \        : Force Full Code Re-Sync
echo   - Ctrl + Alt + \          : Pause / Resume Auto-Scan
echo   - Ctrl + 4                : AI Lecture Note-Taker (Cornell notes)
echo.
echo LIVE CODE FOLLOWER LANGUAGES:
echo   Supports Java, C, C++, C#, Python, JavaScript/TypeScript, PHP,
echo   Ruby, Go, Rust, Kotlin, Swift, SQL, HTML/CSS, and shell.
echo   "subject": "auto" enables detection; the built-in default is "java".
echo   Tray menu -^> Subject Preset pins Java, Advanced Java, C, SQL, or Web.
echo.
echo OTHER:
echo   - ESC                     : Emergency Stop (abort typing / actions)
echo   - Ctrl + Alt + Z          : Undo Last Code Edit (20 snapshots)
echo   - Ctrl + Shift + Q        : Exit ScreenBot
echo.
echo IF CLICKS START OPENING LINKS/NEW WINDOWS AFTER EXITING:
echo   - Run fix_stuck_keys.bat. No reboot required.
echo.
echo This window will close automatically in 4 seconds...
timeout /t 4 >nul
