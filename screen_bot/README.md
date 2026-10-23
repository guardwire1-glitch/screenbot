# 🚀 ScreenBot: Silent, Fast Screen-to-Answer AI Assistant

**ScreenBot** runs silently in the background of your Windows PC. Whenever you press **`Ctrl + 0`**, it instantly captures what is on your screen, reads the question/problem using ultra-fast AI vision, and **immediately copies the exact answer directly to your clipboard** ready to paste with `Ctrl + V`.

---

## ⚡ Key Features

- **Silent & Invisible**: Starts via `start_silent.vbs` (or `START_SCREENBOT.bat`) with no console popups and no taskbar clutter.
- **Visual “I’m Running” Badge (No Beep)**: A small always-on-top card appears in the bottom-right corner for **60 seconds** saying **“ScreenBot is running”**, with the current language and Auto-Scan state. Click it to dismiss, or summon it anytime from the tray menu. No startup chime or beeps play by default.
- **No Installation Required**: The bundle ships a frozen `ScreenBot.exe`. No Python, no pip, no dependencies.
- **Fast Multimodal Vision**: Powered by Google's `gemini-3.5-flash-lite` model—analyzes text, code, math formulas, multiple choice options, and diagrams in under 1 second.
- **Instant Clipboard Output**: The answer is copied directly to your clipboard immediately upon completion.
- **Global Hotkeys**: Work from anywhere on Windows (browsers, IDEs, PDFs, fullscreen apps).
- **Optional Audio Feedback**:
  - Silent by default (`"sound_feedback": false`).
  - Set `"sound_feedback": true` if you want the old hotkey/answer beeps back.
- **Smart Active Window Capture**: By default, captures your focused active window for maximum speed and precision (or switches to fullscreen in settings).
- **Multi-Language Live Code Follower**: Watches a lecture screen and syncs the code into your editor. It **auto-detects the language** and supports Java, C, C++, C#, Python, JavaScript/TypeScript, PHP, Ruby, Go, Rust, Kotlin, Swift, SQL, HTML/CSS, and shell. In **Direct File Sync** mode it only writes the bound file on disk (zero keystrokes). With Direct File Sync off it pastes at your caret, but only after re-confirming that the *same* editor window captured at snapshot time still has focus — and never into a browser or meeting window.
- **Instant Status Checker**: Double-click `check_status.bat` anytime to see if it is currently running and whether your API key resolved.

---

## 🛠️ Quick Setup

### Step 1: Extract the archive
Right-click the `.zip` → **Extract All...** → open the extracted folder.
*(ScreenBot cannot run from inside a zip preview folder.)*

### Step 2: Add your free Gemini API Key
1. Get a free API key in seconds from [Google AI Studio](https://aistudio.google.com/).
2. Run **`SETUP.bat`** from the extracted folder.
   - It asks for your key and stores it in **`%APPDATA%\ScreenBot\config.json`** — a per-user file that lives on *your* PC only.
   - It then runs the built-in self-test so you can confirm everything works.

> **Why not `config.json`?**
> The `config.json` sitting next to `ScreenBot.exe` is the **shared template** — it is the very file that gets packaged into the `.zip` you send to friends. Putting your key in it (or in a `.env` inside the zip) means handing your key to everyone you share the archive with.
> ScreenBot resolves the key using this order, and **a blank value never overrides a real one**:
>
> | # | Source | Notes |
> |---|---|---|
> | 1 | System environment variable | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |
> | 2 | `.env` next to `ScreenBot.exe` | Blank = treated as *not set* |
> | 3 | `%APPDATA%\ScreenBot\config.json` | **Per-user authoritative store — recommended** |
> | 4 | `config.json` next to `ScreenBot.exe` | Shipped, sanitized template |

---

## 🎮 How to Run

### 1. Silent Background Mode (Recommended)
Double-click **`start_silent.vbs`** (or **`START_SCREENBOT.bat`** for a short console window that lists the hotkeys).
- It starts immediately in the background with no console popups.
- A **“ScreenBot is running”** badge appears in the bottom-right for 60 seconds (no beep). Click it to dismiss it early.
- Press **`Ctrl + 0`** anytime to capture and copy the answer!

### 2. Debug / Console Mode (For Testing)
Double-click **`run_debug.bat`**.
- Runs the subsystem self-test (capture, hotkeys, Gemini reachability, clipboard).
- Starts ScreenBot and streams `screen_bot.log` live to the console.
- Press `Ctrl + Shift + Q` to close.

### 3. Stop the Bot
Double-click **`stop_bot.bat`**.
- Immediately terminates any background instances cleanly.
- It also force-releases Ctrl/Shift/Alt/Win, so clicks behave normally afterwards.

### 4. Auto-Start with Windows (Optional)
Double-click **`install_startup.bat`**.
- Automatically launches ScreenBot silently every time you turn on your PC.
- (To remove, run `uninstall_startup.bat`).

---

## ⌨️ Hotkeys & Modes

| Hotkey | Action | Description |
|---|---|---|
| **`Ctrl + 0`** | **Direct Answer** | Outputs only the concise, direct answer or code solution (no filler, ready to paste). |
| **`Ctrl + 9`** | **Explained Answer** | Outputs the direct answer followed by a brief 2-3 bullet explanation. |
| **`Ctrl + 8`** | **Paraphrase** | Rewrites the answer in your own words (anti-plagiarism). |
| **`Ctrl + 2`** | **Highlight & Solve** | Solves the highlighted exam question and types/pastes the answer. Also `Ctrl+Shift+V`, `Ctrl+Alt+V`, `Ctrl+7`. |
| **`Ctrl + Shift + F`** | **Code Sync** | Syncs the lecture code now. Writes the bound file when Direct File Sync is on; otherwise pastes at the caret (also `Ctrl+\` or `Ctrl+3`). |
| **`Ctrl + Shift + \`** | **Force Re-Sync** | Re-syncs the entire file from line 1. |
| **`Ctrl + Alt + \`** | **Pause / Resume Auto-Scan** | Toggles the 5-second live lecture scanner. |
| **`Ctrl + 4`** | **Lecture Note-Taker** | Turns lecture slides into Cornell-style study notes (Slides → Word/OneNote). |
| **`Ctrl + Alt + Z`** | **Undo Last Code Edit** | Rolls back up to 20 saved file snapshots, one press at a time. |
| **`ESC`** | **Emergency Stop** | Aborts typing or any running action immediately. |
| **`Ctrl + Shift + Q`** | **Exit** | Shuts down ScreenBot. |

---

## ⚙️ Configuration (`config.json`)

The `config.json` next to `ScreenBot.exe` holds the **shared defaults**. For your own machine-specific tweaks, prefer `%APPDATA%\ScreenBot\config.json`, which is loaded on top of the template.

```json
{
  "gemini_api_key": "",
  "model": "gemini-3.5-flash-lite",
  "subject": "auto",
  "hotkey": "ctrl+0",
  "hotkey_explain": "ctrl+9",
  "hotkey_paraphrase": "ctrl+8",
  "hotkey_type": "ctrl+2",
  "capture_mode": "active_window",
  "sound_feedback": false,
  "max_image_dimension": 1920,
  "image_quality": 85
}
```

- `model`: Gemini model id. Defaults to `gemini-3.5-flash-lite`.
- `capture_mode`: `"active_window"` (focuses on the current app) or `"fullscreen"` (captures whole desktop).
- `sound_feedback`: Silent by default. Set to `true` only if you want the optional hotkey/answer beeps.
- `hotkey`: Change to any combination like `"ctrl+f9"`, `"ctrl+alt+a"`, etc.
- `subject`: Language preset used by the live code follower. Use `"auto"` (default) to detect the language from the code/file, or pin one of: `java`, `adv_java`, `csharp`, `c`, `cpp`, `python`, `javascript`, `web`, `sql`, `php`, `ruby`, `go`, `rust`, `kotlin`, `swift`.
- `direct_file_sync`: `true` writes code straight to the bound file on disk **and sends no keystrokes at all** (safest — the bot can never type into Teams/YouTube). `false` pastes at the caret instead, guarded by a focus check against the window captured at snapshot time.
- `target_file_path`: The file the code follower writes to when direct file sync is enabled.
- `auto_scan_enabled`: Start the 5-second auto-scan loop immediately on launch.
- `typing_wpm`: Typing speed for the highlight-and-solve typer.

> **Auto-scan latency:** each scan waits ~0.4 s for the video frame to stabilise, then requires a newly seen line to appear on **two consecutive scans** before it is written. Expect roughly 10 seconds between the lecturer typing a line and it landing in your file. This is deliberate: it prevents half-rendered frames and one-off OCR hallucinations from corrupting the file.

---

## 🩺 Troubleshooting

**"GEMINI_API_KEY is not set."**
Your key did not resolve. Run `check_status.bat` — it prints which tier the key came from. If it says no key, run `SETUP.bat` and paste your key.

**The tray icon is missing.**
The app is running but hidden in the overflow area next to the clock. Run `check_status.bat` to confirm.

**I heard no beep on startup.**
That is intentional. The startup chime was replaced by the **“ScreenBot is running”** badge in the bottom-right corner, which auto-closes after 60 seconds. To show it again, use the tray menu → **🟢 Show Running Badge (60s)**.

**The code follower produces the wrong language.**
Leave `"subject": "auto"` in `config.json` (the default). ScreenBot detects the language from the code on screen and from the active file extension. You can also pin a language from the tray menu → **Subject Preset**.

**Nothing happens when I press a hotkey.**
Another app may have grabbed the same combination. Change it in `config.json` (e.g. `"hotkey": "ctrl+alt+a"`) and restart.

**The code follower runs but my editor never changes.**
Check `direct_file_sync` in `config.json`. When it is `true`, ScreenBot writes to `target_file_path` and deliberately sends **no keystrokes**. If the file is open in Visual Studio, accept the "file changed on disk" reload (or close/reopen the tab). If you want caret typing instead, set `direct_file_sync` to `false`.

**Code appears twice, or lands in the Teams/YouTube chat.**
This was the old behaviour when Direct File Sync wrote the file *and* pasted at the same time. Both are now mutually exclusive: a successful file write always skips caret injection, and the paste path aborts unless the exact editor window captured at snapshot time is still focused and is not a browser/meeting window.

**The live follower skips a line or waits.**
By design: an unstable video frame is skipped, and a brand-new line must be seen on two consecutive scans before it is written. Press `Ctrl + Shift + F` to force an immediate manual sync of the current frame.

**After I close ScreenBot, every click opens a link/new window or duplicates things.**
That is a stuck **Ctrl** key, not a virus. It happens when a suppressed hotkey (or `taskkill /F`) prevents the key-up from reaching Windows. ScreenBot now releases Ctrl/Shift/Alt/Win on startup, after every hotkey, and on exit, and a watchdog does it automatically if a key is held for more than 25 seconds. If it still happens, run **`fix_stuck_keys.bat`** (or the tray menu → **Release Stuck Keys**) — no reboot needed.

**Auto-scan suddenly stops, or the log mentions `429` / quota.**
You hit the Gemini free-tier rate limit. ScreenBot now pauses AI calls for 45 seconds instead of hammering the API, then resumes automatically. Wait for the cooldown, or raise your Gemini plan/quota. Manual `Ctrl+Shift+F` still works after the cooldown.

**"You are running directly from inside a ZIP file."**
Extract the archive first — ScreenBot cannot run from a zip preview folder.

**Self-test shows a FAIL line.**
Run `run_debug.bat` and read `screen_bot.log` for the full detail.

---

## 📁 What's In This Folder

| File | Purpose |
|---|---|
| `ScreenBot.exe` | The application (frozen, no dependencies). |
| `config.json` | Shared defaults template (blank API key by design). |
| `SETUP.bat` | First-time setup: saves your API key and runs the self-test. |
| `start_silent.vbs` / `start_silent.bat` | Launch ScreenBot invisibly in the background. |
| `START_SCREENBOT.bat` | Launch ScreenBot with a short hotkey cheat-sheet. |
| `run_debug.bat` | Self-test + live log console. |
| `check_status.bat` | Is it running? Is the key resolved? |
| `stop_bot.bat` | Stop all ScreenBot instances and release any stuck modifier keys. |
| `fix_stuck_keys.bat` / `.ps1` | Emergency recovery if a Ctrl/Shift key sticks: restores normal clicking without a reboot. |
| `install_startup.bat` / `uninstall_startup.bat` | Add/remove auto-start on Windows login. |
| `screen_bot.log` | Runtime log (excluded from the shared zip). |
