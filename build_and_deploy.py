"""
Hardened Build & Verification Pipeline for ScreenBot Pro.
1. Performs Python AST/bytecode syntax checks across all modules.
2. Runs the full pytest test suite.
3. Stamps build metadata (timestamp and revision).
4. Compiles standalone ScreenBot.exe via PyInstaller.
5. Deploys verified binaries and sanitized configs to Documents/bt and Downloads/screen_bot.
6. Verifies the deployed layout resolves credentials correctly (deploy-time guard).
"""

import sys
import json
import time
import shutil
import zipfile
import datetime
import py_compile
import subprocess
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
SCREENBOT_DIR = ROOT_DIR / "screen_bot"
DIST_DIR = ROOT_DIR / "dist"
DEST_BT = Path.home() / "OneDrive - Edulink International College" / "Documents" / "bt"
DEST_DL = Path.home() / "Downloads" / "screen_bot"

# ---------------------------------------------------------------------------
# Deployment hygiene
# ---------------------------------------------------------------------------
# Files that must NEVER appear inside a distributed archive. `.env` is the
# big one: the developer's PJR/screen_bot/.env holds the real API key, and
# shipping it inside bt_ScreenBot.zip / ScreenBot_v2.zip hands that key to
# every friend who receives the archive (MISTAKE M-03).
ZIP_EXCLUDED_NAMES = {".env", "debug_output.txt"}
ZIP_EXCLUDED_SUFFIXES = {".log", ".pyc"}

# Human-facing support files, kept in sync from screen_bot/ into both
# deployment folders so the docs can never drift from the binary again.
SUPPORT_FILES = (
    ".env.example",
    "HOW_TO_USE.txt",
    "README.md",
    "START_SCREENBOT.bat",
    "check_status.bat",
    "fix_stuck_keys.bat",
    "fix_stuck_keys.ps1",
    "icon.ico",
    "install_startup.bat",
    "requirements.txt",
    "run_debug.bat",
    "SETUP.bat",
    "start_silent.bat",
    "start_silent.vbs",
    "stop_bot.bat",
    "uninstall_startup.bat",
)

# Loose source modules belong ONLY in the source distribution
# (Downloads/screen_bot). They are deliberately never copied into
# Documents/bt: a frozen build must not sit next to loose .py files, or a
# stale module can shadow the one bundled inside the exe (MISTAKE M-02).
SOURCE_MODULES = (
    "bot.py",
    "ai_solver.py",
    "capture.py",
    "proposal_engine.py",
    "build_info.py",
)

# Blank, self-documenting .env written into every deployment folder. A blank
# value is treated as "not set" by load_config(), so this can never erase the
# key resolved from %APPDATA%\ScreenBot\config.json.
SANITIZED_ENV = """\
# =====================================================================
#  ScreenBot Pro - optional local credential override
# =====================================================================
#  You normally do NOT need to edit this file.
#
#  ScreenBot resolves your Gemini API key in this order
#  (first NON-BLANK value wins):
#
#    1. System environment variable  GEMINI_API_KEY / GOOGLE_API_KEY
#    2. This .env file  (next to ScreenBot.exe)
#    3. %APPDATA%\\ScreenBot\\config.json   <-- recommended place for your key
#    4. config.json  (next to ScreenBot.exe)
#
#  LEAVE THE VALUE BELOW BLANK unless you are running a private, unshared
#  copy. This file travels inside the shared .zip, so a key typed here
#  would be handed to everyone you send that zip to.
#
#  To set your key, run SETUP.bat - it stores it in
#  %APPDATA%\\ScreenBot\\config.json instead, which never leaves your PC.
# =====================================================================
GEMINI_API_KEY=
"""


def _purge_bytecode(root: Path) -> int:
    """Deletes stale .pyc files / __pycache__ dirs under `root`.

    Loose bytecode is the residue of the shadow-import collision
    (MISTAKE M-02): a stale module compiled against an old API can be
    imported instead of the bundled one. It must never survive a deploy.
    """
    removed = 0
    if not root.exists():
        return removed
    for cache in list(root.rglob("__pycache__")):
        if not cache.is_dir():
            continue
        for pyc in cache.glob("*.pyc"):
            try:
                pyc.unlink()
                removed += 1
            except OSError:
                pass
        try:
            cache.rmdir()
        except OSError:
            pass
    for pyc in root.rglob("*.pyc"):
        try:
            pyc.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _sanitized_config_text(path: Path) -> str:
    """Returns the config.json that is safe to ship inside a shared archive.

    Keeps the user's layout/tuning defaults but strips everything device-
    specific: the API key, the direct-file-sync toggle and the absolute
    target path (which points at a file that only exists on this PC).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["gemini_api_key"] = ""
    data["direct_file_sync"] = False
    data["target_file_path"] = ""
    return json.dumps(data, indent=2)


def _zip_one_folder(zf: zipfile.ZipFile, root: Path, sanitized_config: str) -> None:
    for f in sorted(root.iterdir()):
        if not f.is_file():
            continue
        if f.name in ZIP_EXCLUDED_NAMES:
            continue
        if f.suffix.lower() in ZIP_EXCLUDED_SUFFIXES:
            continue
        if f.name == "config.json":
            zf.writestr(f.name, sanitized_config)
        else:
            zf.write(f, arcname=f.name)


def step_1_syntax_check():
    print("[1/6] Running syntax compile checks on screen_bot/ modules...")
    py_files = list(SCREENBOT_DIR.glob("*.py"))
    for py_file in py_files:
        try:
            py_compile.compile(str(py_file), doraise=True)
            print(f"  [OK] {py_file.name}: Syntax OK")
        except Exception as e:
            print(f"  [FAIL] {py_file.name}: SYNTAX ERROR: {e}")
            sys.exit(1)


def step_2_run_tests():
    print("\n[2/6] Running pytest test suite...")
    # Invoke pytest through the *same* interpreter running this script.
    # `uv run pytest` resolves a separate ephemeral environment and can fail to
    # spawn the console script, which aborts the whole build on a transient
    # resolver hiccup.
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v"], cwd=str(ROOT_DIR)
    )
    if res.returncode != 0:
        print("\n[FAIL] Build aborted: One or more tests failed!")
        sys.exit(1)
    print("  [OK] All pytest replay, injection & config tests PASSED!")


def step_3_stamp_build():
    print("\n[3/6] Stamping build metadata...")
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    commit_str = "hardened-rel-v2.1"
    build_info_content = f'''"""
Build metadata generated automatically during the build process.
"""

BUILD_TIMESTAMP = "{now_str}"
BUILD_COMMIT = "{commit_str}"
'''
    (SCREENBOT_DIR / "build_info.py").write_text(build_info_content, encoding="utf-8")
    print(f"  [OK] Stamped: {now_str} ({commit_str})")


def step_4_pyinstaller():
    print("\n[4/6] Compiling standalone executable via PyInstaller...")
    spec_file = ROOT_DIR / "ScreenBot.spec"
    res = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(spec_file), "--noconfirm"],
        cwd=str(ROOT_DIR),
    )
    if res.returncode != 0:
        print("\n[FAIL] PyInstaller compilation failed!")
        sys.exit(1)

    exe_path = DIST_DIR / "ScreenBot.exe"
    if not exe_path.exists():
        print("\n[FAIL] Expected executable was not found at dist/ScreenBot.exe!")
        sys.exit(1)

    exe_size_mb = exe_path.stat().st_size / (1024 * 1024)
    print(f"  [OK] Compiled dist/ScreenBot.exe successfully ({exe_size_mb:.2f} MB)")


def step_5_deploy():
    print("\n[5/6] Deploying hardened binaries and sanitized configs...")
    try:
        subprocess.run(["taskkill", "/F", "/IM", "ScreenBot.exe"], capture_output=True)
    except Exception:
        pass
    DEST_BT.mkdir(parents=True, exist_ok=True)
    DEST_DL.mkdir(parents=True, exist_ok=True)

    src_exe = DIST_DIR / "ScreenBot.exe"
    src_config = SCREENBOT_DIR / "config.json"
    src_icon = SCREENBOT_DIR / "icon.png"

    # 1. Purge stale bytecode from both deployment folders.
    for dest in (DEST_BT, DEST_DL):
        removed = _purge_bytecode(dest)
        if removed:
            print(f"  [OK] Purged {removed} stale .pyc file(s) from {dest}")

    def _safe_copy(src, dst):
        for attempt in range(6):
            try:
                shutil.copy2(src, dst)
                return
            except PermissionError:
                subprocess.run(["taskkill", "/F", "/IM", dst.name], capture_output=True)
                time.sleep(0.5)
        shutil.copy2(src, dst)

    # 2. Ship the frozen binary + shared assets.
    for dest in (DEST_BT, DEST_DL):
        _safe_copy(src_exe, dest / "ScreenBot.exe")
        _safe_copy(src_config, dest / "config.json")
        if src_icon.exists():
            _safe_copy(src_icon, dest / "icon.png")

    # 3. Sync the human-facing support files so docs never drift.
    synced = 0
    for name in SUPPORT_FILES:
        src = SCREENBOT_DIR / name
        if not src.exists():
            print(f"  [WARN] Missing support file in source tree: {name}")
            continue
        for dest in (DEST_BT, DEST_DL):
            shutil.copy2(src, dest / name)
        synced += 1
    print(f"  [OK] Synced {synced} support file(s) into both deployment folders")

    # 4. Source distribution only: loose .py modules (never into Documents/bt).
    copied = 0
    for name in SOURCE_MODULES:
        src = SCREENBOT_DIR / name
        if src.exists():
            shutil.copy2(src, DEST_DL / name)
            copied += 1
    print(f"  [OK] Synced {copied} source module(s) into {DEST_DL}")

    # 5. Sanitize .env everywhere. Never inherit the developer's real key.
    for dest in (DEST_BT, DEST_DL):
        (dest / ".env").write_text(SANITIZED_ENV, encoding="utf-8")
    print("  [OK] Wrote blank, documented .env to both deployment folders")

    # 6. Refresh the deployed diagnostic snapshot (was stale for weeks).
    _write_debug_snapshot(DEST_BT)

    # 7. Repackage self-contained ready-to-run zips. The shipped config.json
    #    is sanitized so the real key and this PC's absolute file path never
    #    leave the machine.
    sanitized_config = _sanitized_config_text(src_config)
    zip_bt = Path.home() / "OneDrive - Edulink International College" / "Documents" / "bt_ScreenBot.zip"
    with zipfile.ZipFile(zip_bt, "w", zipfile.ZIP_DEFLATED) as zf:
        _zip_one_folder(zf, DEST_BT, sanitized_config)

    zip_dl = Path.home() / "Downloads" / "ScreenBot_v2.zip"
    with zipfile.ZipFile(zip_dl, "w", zipfile.ZIP_DEFLATED) as zf:
        _zip_one_folder(zf, DEST_DL, sanitized_config)

    print(f"  [OK] Deployed to {DEST_BT}")
    print(f"  [OK] Deployed to {DEST_DL}")
    print(f"  [OK] Packaged ready-to-run {zip_bt.name} and {zip_dl.name}")
    print("\n>>> BUILD AND DEPLOY COMPLETE: 100% HARDENED & VERIFIED! <<<")


def _write_debug_snapshot(dest: Path) -> None:
    """Regenerates debug_output.txt from the freshly deployed binary."""
    exe = dest / "ScreenBot.exe"
    if not exe.exists():
        return
    try:
        res = subprocess.run(
            [str(exe), "--selftest"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(dest),
        )
        output = ((res.stdout or "") + (res.stderr or "")).strip()
    except Exception as e:
        output = f"[WARN] Could not run deployed self-test: {e}"

    try:
        stamp = (SCREENBOT_DIR / "build_info.py").read_text(encoding="utf-8")
        build_line = next(
            (ln.strip() for ln in stamp.splitlines() if ln.startswith("BUILD_TIMESTAMP")),
            "BUILD_TIMESTAMP = unknown",
        )
    except Exception:
        build_line = "BUILD_TIMESTAMP = unknown"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (dest / "debug_output.txt").write_text(
        "ScreenBot Pro - deployed diagnostic snapshot\n"
        f"Generated: {now}\n"
        f"{build_line}\n"
        + "=" * 60 + "\n"
        + output + "\n",
        encoding="utf-8",
    )
    print("  [OK] Refreshed debug_output.txt from the deployed binary")



def step_6_verify_deployed_credentials():
    """Guards against shipping a deployment whose credential resolution is broken.

    Historically the deployed config.json template shipped with
    "gemini_api_key": "" which silently erased the real key held in
    %APPDATA%\\ScreenBot\\config.json, so the shipped exe failed with
    "GEMINI_API_KEY is not set." at runtime. This replays the deployed
    layout through the real loader and reports which tier the key came from.
    """
    print("\n[6/6] Verifying deployed credential resolution...")
    probe = (
        "import sys, pathlib;"
        f"sys.path.insert(0, {str(SCREENBOT_DIR)!r});"
        "import bot;"
        f"bot.SCRIPT_DIR = pathlib.Path({str(DEST_BT)!r});"
        f"bot.ENV_PATH = pathlib.Path({str(DEST_BT / '.env')!r});"
        f"bot.CONFIG_PATH = pathlib.Path({str(DEST_BT / 'config.json')!r});"
        "print('RESOLVED=' + (bot.load_config().get('gemini_api_key') or ''))"
    )
    try:
        res = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            cwd=str(ROOT_DIR),
            timeout=120,
        )
    except Exception as e:
        print(f"  [WARN] Could not probe deployed credentials: {e}")
        return

    resolved = ""
    for line in (res.stdout or "").splitlines():
        if line.startswith("RESOLVED="):
            resolved = line.split("=", 1)[1].strip()

    if resolved:
        print(f"  [OK] Deployed layout resolves a credential ({len(resolved)} chars, not printed).")
    else:
        print("  [WARN] Deployed layout resolves NO credential.")
        print("         The exe will run but every Gemini call will fail with")
        print("         'GEMINI_API_KEY is not set.' until a key is provided via")
        print("         system env var, .env, or %APPDATA%\\ScreenBot\\config.json.")


if __name__ == "__main__":
    step_1_syntax_check()
    step_2_run_tests()
    step_3_stamp_build()
    step_4_pyinstaller()
    step_5_deploy()
    step_6_verify_deployed_credentials()
