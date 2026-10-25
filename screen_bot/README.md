# 🚀 ScreenBot: Silent, Fast Screen-to-Answer AI Assistant

**ScreenBot** runs silently in the background of your Windows PC. Whenever you press **`Ctrl + 0`**, it captures what is on your screen, reads the question/problem using AI vision, and **copies the answer directly to your clipboard** ready to paste with `Ctrl + V`.

---

## ⚡ Key Features

- **Silent & Invisible**: Starts via `start_silent.vbs` (or `START_SCREENBOT.bat`) with no console popups and no taskbar clutter.
- **Running Status Indicator**: A separate silent notification-area icon reports **“ScreenBot running | auto-scan ON/OFF | language”**. It stays for as long as ScreenBot runs and exits with the app; it is not a 60-second popup. Startup and hotkey sounds are controlled by `"sound_feedback"` (on in the built-in defaults; set it to `false` to mute).
- **No Installation Required**: The bundle ships a frozen `ScreenBot.exe`. No Python, no pip, no dependencies.
- **Fast Multimodal Vision**: Powered by Google's `gemini-3.5-flash-lite` model—analyzes text, code, math formulas, multiple choice options, and diagrams in one vision call (latency depends on network and model load).
- **Instant Clipboard Output**: The answer is copied directly to your clipboard immediately upon completion.
- **Global Hotkeys**: Work from anywhere on Windows (browsers, IDEs, PDFs, fullscreen apps).
- **Audio Feedback**: Startup and hotkey cue beeps are on in the built-in defaults. Set `"sound_feedback": false` to mute them.
- **Smart Active Window Capture**: By default, captures your focused active window for maximum speed and precision (or switches to fullscreen in settings).
- **Multi-Language Live Code Follower**: Watches a lecture screen and syncs the code into your editor. It supports Java, C, C++, C#, Python, JavaScript/TypeScript, PHP, Ruby, Go, Rust, Kotlin, Swift, SQL, HTML/CSS, and shell, with a configurable language hint or auto-detection. In **Direct File Sync** mode it writes only the explicitly bound file through a hash-checked editor adapter (zero keystrokes). With the VS Code-family extension it edits the real buffer, including unsaved text; other IDEs write the saved file on disk through an explicit `target_file_path`. With Direct File Sync off it pastes at your caret, but only after re-confirming that the *same* editor window captured at snapshot time still has focus — and never into a browser or meeting window.
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
>
> **Repository note:** in this working copy, `screen_bot/config.json` and `screen_bot/.env` are gitignored because they can contain your real Gemini key. Never commit, log, zip, or share either file while a key is present. Clear the key before packaging a build for someone else.

---

## 🎮 How to Run

### 1. Silent Background Mode (Recommended)
Double-click **`start_silent.vbs`** (or **`START_SCREENBOT.bat`** for a short console window that lists the hotkeys).
- It starts immediately in the background with no console popups.
- A separate notification-area status icon reports that ScreenBot is running; it disappears when ScreenBot exits.
- Press **`Ctrl + 0`** anytime to capture and copy the answer!

### 2. Debug / Console Mode (For Testing)
Double-click **`run_debug.bat`**.
- Runs the subsystem self-test (capture, hotkeys, Gemini reachability, clipboard).
- Starts ScreenBot and streams `screen_bot.log` live to the console.
- Press `Ctrl + Shift + Q` to close.

### 3. Stop the Bot
Double-click **`stop_bot.bat`**.
- Force-stops any background instances.
- It also runs `fix_stuck_keys.bat` to release Ctrl/Shift/Alt/Win, so clicks behave normally afterwards.

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
| **`Ctrl + \`** / **`Ctrl + 3`** | **Sync New Lines** | Captures the current lecture frame and delivers newly accepted lines to the bound file or caret after the stability and consensus gates. |
| **`Ctrl + Shift + F`** | **Bind Editor & Sync** | Binds the exact active file in the focused VS Code-family editor or Visual Studio, turns on Direct File Sync, and starts a sync. A browser or meeting window is refused. Other IDEs have no exact active-path API, so they use `target_file_path` instead. This shortcut is suppressed so it does not also open the editor's Find-in-Files dialog. |
| **`Ctrl + Shift + \`** | **Force Full Re-Sync** | Rebuilds the target from the full visible lecture buffer. |
| **`Ctrl + Alt + \`** | **Pause / Resume Auto-Scan** | Toggles the interval-based live lecture scanner. |
| **`Ctrl + 4`** | **Lecture Note-Taker** | Turns lecture slides into Cornell-style study notes (Slides → Word/OneNote). |
| **`Ctrl + Alt + Z`** | **Undo Last Code Edit** | Restores the previous ScreenBot snapshot. In Direct File Sync it also rolls the bound file back, but only when that file still exactly matches the last delivered code; it refuses rather than overwrite newer user edits. In caret mode it only restores ScreenBot's internal master. |
| **`ESC`** / **`Ctrl + Shift + X`** | **Emergency Stop** | Aborts typing or any running action immediately. |
| **`Ctrl + Shift + Q`** / **`Ctrl + Alt + Esc`** | **Exit** | Shuts down ScreenBot. |

---

## 🧩 Editor Bridge & Supported IDEs

ScreenBot starts a localhost-only editor bridge with the app. It writes a token-protected discovery file to `%APPDATA%\ScreenBot\bridge.json` and removes it when ScreenBot exits. The bridge is how ScreenBot reads the real buffer/file and the editor's diagnostics before writing, and how it applies a hash-checked edit instead of blind keystrokes.

Every request must carry the token in the `X-ScreenBot-Token` header. A token in the query string is rejected, because URLs leak through proxies, logs, and history. The only unauthenticated endpoint is `/v1/health`, which reports no file content.

### VS Code family (VS Code, Antigravity, Cursor, Windsurf)

Install the bundled extension from `ide_extension/vscode`:

1. Copy that folder into your editor's extensions directory as `screenbot.screenbot-editor-bridge-1.0.0`:
   - VS Code: `%USERPROFILE%\.vscode\extensions\screenbot.screenbot-editor-bridge-1.0.0`
   - Antigravity: `%USERPROFILE%\.antigravity\extensions\screenbot.screenbot-editor-bridge-1.0.0`
   - Other VS Code-based editors: their equivalent extensions directory.
   - Alternatively, package it with `npx @vscode/vsce package` inside `ide_extension/vscode`, then install the generated `.vsix` from the Extensions view.
2. Restart the editor. When ScreenBot is running, the status bar shows `ScreenBot: bridge connected`.
3. From the command palette: `ScreenBot: Show Bridge Status` and `ScreenBot: Review Pending Code Changes`.

The extension supplies the exact active buffer (including unsaved edits), dirty flag, cursor position, and diagnostics. It replaces the buffer only when the base hash still matches, saves through the editor API, and can optionally run the editor's document formatter.

### Visual Studio

If `pywin32` is installed in the Python environment, ScreenBot uses the Visual Studio DTE for the active buffer and Error List. Without `pywin32`, `Ctrl+Shift+F` cannot resolve the active path, so set `target_file_path` and Visual Studio is written through the disk adapter (saved files only).

The DTE adapter initializes COM on each calling thread, keeps its connection state per thread, and retries a missing Visual Studio with a short exponential backoff (5 s up to 30 s). It therefore recovers when Visual Studio starts after ScreenBot or restarts. An apply is wrapped in a single DTE undo context, so one `Ctrl+Z` in Visual Studio removes the whole ScreenBot edit. Only the changed line range is replaced — the rest of the document, including breakpoints and bookmarks on untouched lines, is left alone. If another undo operation is already open, the change is held rather than joined to it. This path is covered by unit tests with a simulated DTE; a real Visual Studio session has not been automated yet, so verify one bind/apply/undo cycle on your machine before relying on it in class.

### JetBrains, Eclipse, NetBeans, SSMS, and other editors

These use the universal disk adapter: set `target_file_path` to the exact saved file (ScreenBot will not guess a path from a filename), and Direct File Sync reads and writes that file atomically, preserving its existing newline style.

The adapter writes automatically only when it can **prove** the editor buffer is saved. It inspects every visible editor window, not just the foreground one, because the lecture video normally has focus. A dirty marker in a matching window always holds the change. VS Code and Antigravity titles are trusted to show a clean buffer as well. JetBrains, Eclipse, NetBeans, SSMS, and Notepad++ do not report saved state reliably in their window titles, so a change for an open file in one of those editors is **queued for review instead of written automatically**. The same applies when the file is not visible in any editor window while an editor is running, since it may be a background tab with unsaved edits. If no editor is running at all, the file cannot have an unsaved buffer and a normal atomic write is allowed.

There is no native plugin for these IDEs yet; they use the same bridge protocol, so a plugin can add the exact-buffer path later. The review UI lives in the VS Code-family extension and can resolve any pending item by path; after accepting a change for a disk-based editor, save/reload the file in that editor if it does not reload automatically.

### What is applied automatically

A direct apply happens only when:

- the target was explicitly bound (`Ctrl+Shift+F`, a valid `target_file_path`, or the tray menu);
- for disk-based editors, the buffer's saved state is positively known (VS Code/Antigravity titles, or no editor running);
- the file/buffer still matches the content ScreenBot read (hash check);
- the merged document does not re-add lines the user deliberately deleted after a previous sync;
- syntax validation of the **full proposed document** passes;
- the editor's error count does not increase (VS Code family / Visual Studio; disk targets have no diagnostics);
- in Visual Studio, no other undo operation is open.

Otherwise the change is queued for review or discarded, and ScreenBot does not advance its master. Compatible new lecture lines can still be applied when unrelated user code differs; the conflicting part is queued. An identical held change is queued only once, and an explicitly dismissed proposal is not queued again on the next scan.

Indentation comes from the merged document, and ScreenBot preserves the file's existing CRLF/LF style. It does not silently reformat your file. To run the editor's formatter after an apply, set `format_on_apply` to `true` in ScreenBot's config, or enable the `screenbot.formatOnApply` setting in the VS Code-family extension. Formatting the whole file can move code you did not touch.

---

## ⚙️ Configuration (`config.json`)

The `config.json` next to `ScreenBot.exe` holds the **shared defaults**. For your own machine-specific tweaks, prefer `%APPDATA%\ScreenBot\config.json`, which is loaded on top of the template.

```json
{
  "gemini_api_key": "",
  "model": "gemini-3.5-flash-lite",
  "subject": "java",
  "hotkey": "ctrl+0",
  "hotkey_explain": "ctrl+9",
  "hotkey_paraphrase": "ctrl+8",
  "hotkey_type": "ctrl+2",
  "capture_mode": "active_window",
  "sound_feedback": true,
  "max_image_dimension": 1920,
  "image_quality": 85,
  "direct_file_sync": false,
  "target_file_path": "",
  "auto_scan_enabled": true,
  "lecture_auto_scan_interval": 5,
  "lecture_injection_mode": "paste",
  "stability_interval": 0.35,
  "stability_threshold": 2.0,
  "lecture_pixel_diff_threshold": 2.0,
  "max_frame_age_seconds": 12,
  "quota_cooldown_seconds": 90,
  "active_line_hold_frames": 2,
  "format_on_apply": false
}
```

- `model`: Gemini model id. Defaults to `gemini-3.5-flash-lite`.
- `capture_mode`: `"active_window"` (focuses on the current app) or `"fullscreen"` (captures whole desktop).
- `sound_feedback`: Controls the startup and hotkey cue beeps. Built-in default is `true`; set `false` to mute.
- `hotkey`: Change to any combination like `"ctrl+f9"`, `"ctrl+alt+a"`, etc.
- `subject`: Language hint used by the live code follower. The built-in default is `java`; set `"auto"` to let the extractor detect the language, or pin one of: `java`, `adv_java`, `csharp`, `c`, `cpp`, `python`, `javascript`, `web`, `sql`, `php`, `ruby`, `go`, `rust`, `kotlin`, `swift`. A `%APPDATA%\ScreenBot\config.json` or local `config.json` value overrides the built-in default.
- `direct_file_sync`: `true` writes through the bound editor/file **and sends no keystrokes at all** (safest — the bot can never type into Teams/YouTube). Bind with `Ctrl+Shift+F` in the VS Code family/Visual Studio, or set `target_file_path` for any IDE. `false` pastes at the caret instead, guarded by a focus check against the window captured at snapshot time.
- `target_file_path`: Absolute path to the file the code follower may write when Direct File Sync is enabled. Required for JetBrains/Eclipse/NetBeans/SSMS and other disk-based IDEs, whose window titles do not expose an exact path. ScreenBot never auto-discovers a write target.
- `auto_scan_enabled`: Start the interval-based auto-scan loop on launch. The built-in default is `true`; this working copy's config sets it to `false`.
- `lecture_auto_scan_interval`: Seconds between auto-scan attempts (default `5`).
- `lecture_injection_mode`: `"paste"` (clipboard) or `"turbo_type"` (synthetic keystrokes) for caret mode.
- `stability_interval`: Seconds between the two frames compared by the stability gate (default `0.35`).
- `stability_threshold`: Maximum mean grayscale difference between those two frames, on a 0–255 scale (default `2.0`, about 0.8% of full range). Frames above it are deferred without an AI call.
- `lecture_pixel_diff_threshold`: When auto-scanning, skip the AI call if the new frame differs from the last scanned frame by less than this amount (default `2.0`). Pending consensus lines force a scan.
- `max_frame_age_seconds`: Discard an extraction if the frame is older than this when the AI answer arrives (default `12`). Set `0` to disable.
- `quota_cooldown_seconds`: Pause after a Gemini 429/quota error (default `90`, minimum `15`). Auto-scan and manual code sync both respect the cooldown.
- `active_line_hold_frames`: Extra clean scans the bottom-most (currently typed) line must survive before auto-delivery (default `2`). Incomplete lines such as `int total =` or `if (ready)` are held until they become complete or another line appears below them. Manual sync bypasses the hold. Set to `0` to disable the extra hold.
- `format_on_apply`: Run the editor's formatter after a successful direct apply (default `false`). This can reformat the whole file.
- `typing_wpm`: Typing speed for the highlight-and-solve typer.

> **Auto-scan latency:** each scan compares two frames ~0.35 s apart and requires a newly seen line on **two consecutive scans**. The line being typed at the bottom waits `active_line_hold_frames` extra scans, and an incomplete line is held until it is finished. At the default 5 s interval, expect roughly 10–15 s for that last line to land, plus capture and AI time; completed lines above it arrive sooner. Press `Ctrl + \` (or `Ctrl+3`) to bypass the hold and sync the current frame. This is deliberate: it prevents half-rendered frames and one-off OCR hallucinations from corrupting the file.

### Development checks

- Python: run `.venv\Scripts\python.exe -m pytest -q`. `pytest.ini` limits collection to `tests/` so scratch scripts that call Gemini are never collected.
- VS Code extension: run `npm test` inside `ide_extension/vscode` (Node 20+; no editor required). It exercises the real HTTP bridge against a mocked `vscode` API.
- CI: `.github/workflows/screenbot-tests.yml` runs both jobs for changes under `screen_bot/`, `tests/`, `ide_extension/`, and the workflow itself.

These are contract and unit tests. They do not replace a recorded-lecture run or a real Visual Studio session; see `RISKS.md` for the remaining unautomated paths.

---

## 🩺 Troubleshooting

**"GEMINI_API_KEY is not set."**
Your key did not resolve. Run `check_status.bat` — it prints which tier the key came from. If it says no key, run `SETUP.bat` and paste your key.

**The tray icon is missing.**
The app is running but hidden in the overflow area next to the clock. Run `check_status.bat` to confirm.

**I want to mute the startup/hotkey beeps.**
Set `"sound_feedback": false` in `%APPDATA%\ScreenBot\config.json` (or the local `config.json`) and restart. The separate notification-area status icon is not a timed popup; it stays while ScreenBot runs and exits with it.

**The code follower produces the wrong language.**
Set `"subject": "auto"` in `config.json` to let the extractor detect the language, or pin one of the supported names. The built-in default is `java`, and a local or `%APPDATA%` value overrides it. The tray menu → **Subject Preset** can pin Java SE/OOP, Advanced Java, C, SQL, or Web; for any other supported language, set `subject` in `config.json`.

**Nothing happens when I press a hotkey.**
Another app may have grabbed the same combination. Change it in `config.json` (e.g. `"hotkey": "ctrl+alt+a"`) and restart.

**The code follower runs but my editor never changes.**
Check `direct_file_sync` in `config.json`. When it is `true`, a file must be bound first: press `Ctrl+Shift+F` in VS Code/Antigravity or Visual Studio, or set a valid `target_file_path` for JetBrains/Eclipse/NetBeans/SSMS. ScreenBot then writes through a hash-checked adapter and deliberately sends **no keystrokes**. For JetBrains/Eclipse/NetBeans/SSMS (and VS Code without the extension), the editor's saved state cannot be proven from its window title, so changes for an open file are queued instead of written. Save the file and accept the review item, or bind the file through the editor bridge. In the disk-based IDEs, save the file before syncing; if the editor did not reload a change written to disk, accept/reload it. If you want caret typing instead, set `direct_file_sync` to `false`.

**The editor extension says "ScreenBot: waiting for app", or never connects.**
Start ScreenBot first. The extension reads `%APPDATA%\ScreenBot\bridge.json`, which exists only while ScreenBot runs. For VS Code-family editors, confirm the extension folder is installed under that editor's extensions directory and fully restart the editor. `ScreenBot: Show Bridge Status` lists connected editor hosts. If the bridge port could not be bound, check `screen_bot.log`.

**A change was held for review.**
In the VS Code family, run `ScreenBot: Review Pending Code Changes` from the command palette. The same review UI can resolve an item for a file from another editor by path. The hold reason is written to the log and shown in the item: unsaved buffer, saved state that could not be verified (JetBrains/Eclipse and similar), hash conflict, a line you deliberately removed, the syntax gate, or worsening diagnostics. An identical change is queued once, not on every scan, and a dismissed change is not queued again. For a disk-based editor, save/reload the file after accepting so its buffer matches the new disk content.

**Code appears twice, or lands in the Teams/YouTube chat.**
This was the old behaviour when Direct File Sync wrote the file *and* pasted at the same time. Both are now mutually exclusive: a successful file write always skips caret injection, and the paste path aborts unless the exact editor window captured at snapshot time is still focused and is not a browser/meeting window.

**The live follower skips a line or waits.**
By design: an unstable video frame is skipped, a brand-new line must be seen on two consecutive scans, and the bottom line is held longer while it is still being typed. An incomplete line such as `int total =` is not delivered until it is finished or another line appears below it. Press `Ctrl + \` (or `Ctrl+3`) to bypass the hold and sync the current frame. `Ctrl+Shift+F` binds the focused editor file and syncs in one step.

**Undo says it refused.**
That is intentional. In Direct File Sync, undo proceeds only if the bound file still exactly matches the code ScreenBot last delivered; it will not overwrite edits you made after the last sync. In caret mode, undo restores ScreenBot's internal master only; use the editor's own undo for typed text.

**After I close ScreenBot, every click opens a link/new window or duplicates things.**
That is a stuck **Ctrl** key, not a virus. It happens when a suppressed hotkey (or `taskkill /F`) prevents the key-up from reaching Windows. ScreenBot releases Ctrl/Shift/Alt/Win on startup, after every hotkey, and on exit, and a watchdog does it automatically if a key is held for more than 25 seconds. If it still happens, run **`fix_stuck_keys.bat`** — no reboot needed.

**Auto-scan suddenly stops, or the log mentions `429` / quota.**
You hit the Gemini free-tier rate limit. ScreenBot pauses AI calls for 90 seconds by default (`quota_cooldown_seconds`, minimum 15) instead of hammering the API, then resumes automatically. Both auto-scan and manual code sync wait out the cooldown. Wait for it, or raise your Gemini plan/quota.

**"You are running directly from inside a ZIP file."**
Extract the archive first — ScreenBot cannot run from a zip preview folder.

**Self-test shows a FAIL line.**
Run `run_debug.bat` and read `screen_bot.log` for the full detail.

---

## 📁 What's In This Folder

| File | Purpose |
|---|---|
| `ScreenBot.exe` | The application (frozen, no dependencies). |
| `config.json` | Shared defaults. Keep your API key out of it when sharing; in this working copy it is gitignored because it can contain a real key. |
| `SETUP.bat` | First-time setup: saves your API key and runs the self-test. |
| `start_silent.vbs` / `start_silent.bat` | Launch ScreenBot invisibly in the background. |
| `START_SCREENBOT.bat` | Launch ScreenBot with a short hotkey cheat-sheet. |
| `run_debug.bat` | Self-test + live log console. |
| `check_status.bat` | Is it running? Is the key resolved? |
| `stop_bot.bat` | Stop all ScreenBot instances and release any stuck modifier keys. |
| `fix_stuck_keys.bat` / `.ps1` | Emergency recovery if a Ctrl/Shift key sticks: restores normal clicking without a reboot. |
| `install_startup.bat` / `uninstall_startup.bat` | Add/remove auto-start on Windows login. |
| `screen_bot.log` | Runtime log (excluded from the shared zip). |

The optional VS Code-family editor extension source lives in `../ide_extension/vscode`; it is installed separately and is not part of the frozen `ScreenBot.exe` bundle.
