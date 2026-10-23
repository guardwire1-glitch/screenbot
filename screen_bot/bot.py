"""
ScreenBot: Silent, fast screen reader, solver, and human typing simulator.
Features:
- Global Hotkeys: Ctrl+0 (Solve), Ctrl+9 (Explain), Ctrl+8 (Paraphrase), Ctrl+Alt+V / Ctrl+Shift+V / Ctrl+7 (Human Auto-Type)
- Human Typing Simulation to bypass copy-paste blockers (Canvas, Google Forms, Moodle)
- Smart Modifier Release Guard preventing Ctrl/Shift key-sticking conflicts
- Dynamic Anti-Plagiarism Engine ensuring unique phrasing for classmates
- System Tray menu with dynamic Subject Selection and Auto-Type toggle
"""

import os
import sys
import re
import json
import time
import random
import difflib
import textwrap
import logging
import threading
from pathlib import Path
from typing import Dict, Any, Optional
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

import keyboard
import pyperclip
import pynput
from pynput.keyboard import Controller as PynputController, Key as PynputKey
from dotenv import load_dotenv
from PIL import Image, ImageDraw
import pystray

# Ensure local imports and file paths work properly in both standard script and frozen .exe
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(SCRIPT_DIR))

from capture import (
    capture_screen_bytes,
    capture_screen_image,
    capture_screen_image_and_bytes,
    compute_frame_diff,
    CaptureBackoffManager,
    is_frame_black_or_blank,
)
from ai_solver import ScreenSolver
from shadow_master import ShadowMaster, MasterChangeEvent
from frame_pipeline import FrameArchiver, FrameStabilityChecker, OcclusionDetector, estimate_text_height
from ai_vision_extractor import (
    build_extraction_prompt,
    parse_model_extraction_response,
    UNREADABLE_TOKEN,
    SUPPORTED_LANGUAGES,
    ExtractedLine,
)
from code_validator import LineConsensusTracker, normalize_for_diff, is_valid_code_line, validate_syntax
from merge_engine import MergeEngine, MergeDecision
from doc_merge import DocMerger, Registry as DocRegistry, FrameLine as DocFrameLine, Meta as DocMeta, plan_injection, apply_plan
from ast_healer import HyperAwareCodeEngine
from smart_context_engine import SmartCodeSynchronizer, heal_and_format
from smart_sync_engine import ScreenBotSmartController, SyncResult, wait_for_physical_keys_released
from injector import SafeInjector, WindowFocusContext
from failsafes import CircuitBreaker, ScanWatchdog, compute_build_hash
from session_reconciler import SessionReconciler, FlaggedItem
from harness import run_selftest

# Default embedded API key for instant out-of-the-box operation on any machine
EMBEDDED_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Configure logging to file and console
LOG_FILE = SCRIPT_DIR / "screen_bot.log"
logger = logging.getLogger("ScreenBot")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

if sys.stdout is not None:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

CONFIG_PATH = SCRIPT_DIR / "config.json"
ENV_PATH = SCRIPT_DIR / ".env"
ICON_PATH = SCRIPT_DIR / "icon.png"
USER_CONFIG_DIR = Path(os.getenv("APPDATA", "")) / "ScreenBot"
USER_CONFIG_PATH = USER_CONFIG_DIR / "config.json"


def load_config() -> Dict[str, Any]:
    """Loads configuration with proper 4-tier precedence:
    1. System environment variable (GEMINI_API_KEY / GOOGLE_API_KEY)
    2. .env file
    3. %APPDATA%\\ScreenBot\config.json (per-user store)
    4. Local config.json template
    """
    config: Dict[str, Any] = {
        "gemini_api_key": "",
        "model": "gemini-3.5-flash-lite",
        "hotkey": "ctrl+0",
        "hotkey_explain": "ctrl+9",
        "hotkey_paraphrase": "ctrl+8",
        "hotkey_type": "ctrl+2",
        "hotkey_code_shadow": "ctrl\\\\",
        "hotkey_force_sync": "ctrl+shift\\\\",
        "hotkey_auto_scan": "ctrl+alt\\\\",
        "lecture_auto_scan_interval": 5,
        "auto_scan_enabled": True,
        "code_sync_mode": "delta",
        "hotkey_note_taker": "ctrl+4",
        "lecture_injection_mode": "paste",
        "auto_type_on_solve": False,
        "typing_wpm": 38,
        "subject": "java",
        "capture_mode": "active_window",
        "sound_feedback": True,
        "max_image_dimension": 1920,
        "image_quality": 85,
    }

    # 3. %APPDATA%\\ScreenBot\\config.json (per-user authoritative store)
    if USER_CONFIG_PATH.exists():
        try:
            with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                for k, v in user_cfg.items():
                    if v != "" or isinstance(v, bool):
                        config[k] = v
        except Exception as e:
            logger.warning(f"Could not parse user config.json: {e}")

    # 4. Local config.json template (overrides non-credential settings)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                local_cfg = json.load(f)
                for k, v in local_cfg.items():
                    if k == "gemini_api_key":
                        if not config.get("gemini_api_key") and v:
                            config[k] = v
                    elif v != "" or isinstance(v, bool):
                        config[k] = v
        except Exception as e:
            logger.warning(f"Could not parse config.json, using defaults: {e}")

    # 2. .env file beside script or executable
    if ENV_PATH.exists():
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            config["gemini_api_key"] = val
        except Exception:
            pass

    # 1. System environment variable (highest precedence)
    system_env = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if system_env:
        config["gemini_api_key"] = system_env

    if not config["gemini_api_key"] and EMBEDDED_API_KEY:
        config["gemini_api_key"] = EMBEDDED_API_KEY

    return config


def get_or_create_icon_image() -> Image.Image:
    """Returns the tray icon Image, generating a crisp modern icon if missing."""
    if ICON_PATH.exists():
        try:
            return Image.open(ICON_PATH)
        except Exception:
            pass

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Background circle (electric blue)
    draw.ellipse([4, 4, 60, 60], fill=(37, 99, 235, 255))
    # Outer white monitor outline
    draw.rounded_rectangle([16, 16, 48, 40], radius=4, outline=(255, 255, 255, 255), width=3)
    # Stand
    draw.line([32, 40, 32, 48], fill=(255, 255, 255, 255), width=3)
    draw.line([24, 48, 40, 48], fill=(255, 255, 255, 255), width=3)
    # Center dot / screen glow
    draw.ellipse([30, 26, 34, 30], fill=(255, 255, 255, 255))

    try:
        img.save(ICON_PATH)
    except Exception:
        pass
    return img


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_idle_time_seconds() -> float:
    """Returns number of seconds since last user keyboard or mouse input."""
    try:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return max(0.0, millis / 1000.0)
    except Exception:
        pass
    return 999.0


def is_browser_or_meeting_window(hwnd) -> bool:
    """Returns True if the foreground window is a browser or video meeting window."""
    try:
        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value.lower()
            meeting_keywords = [
                "youtube", "teams", "zoom", "meet -", "google meet",
                "chrome", "edge", "firefox", "brave", "opera"
            ]
            for kw in meeting_keywords:
                if kw in title:
                    return True
    except Exception:
        pass
    return False


def clean_code_line(line: str) -> str:
    """Strips OCR line numbers (e.g. '1 | ', '12: ', '3. ') and trailing whitespace."""
    l = re.sub(r"^\s*\d+[\s\|\.:]+", "", line)
    return l.rstrip()


def normalize_for_diff(line: str) -> str:
    """Normalizes a code line for sequence matching by condensing whitespace."""
    cleaned = clean_code_line(line)
    return re.sub(r"\s+", " ", cleaned).strip()


def compute_code_delta(master_code: str, new_code: str):
    """Closed-loop scroll-immune delta computation.
    Preserves all accumulated code across the entire file so lines that scroll
    off the top/bottom are never forgotten or re-injected when scrolling back.
    Returns: (delta_to_inject, updated_master_code)"""
    if not new_code or not new_code.strip():
        return ("", master_code or "")

    new_raw_lines = [clean_code_line(l) for l in new_code.splitlines()]
    while new_raw_lines and not new_raw_lines[0].strip():
        new_raw_lines.pop(0)
    while new_raw_lines and not new_raw_lines[-1].strip():
        new_raw_lines.pop()

    if not new_raw_lines:
        return ("", master_code or "")

    cleaned_new_code = "\n".join(new_raw_lines)

    if not master_code or not master_code.strip():
        return (cleaned_new_code, cleaned_new_code)

    master_raw_lines = [clean_code_line(l) for l in master_code.splitlines()]
    while master_raw_lines and not master_raw_lines[0].strip():
        master_raw_lines.pop(0)
    while master_raw_lines and not master_raw_lines[-1].strip():
        master_raw_lines.pop()

    m_norm = [normalize_for_diff(l) for l in master_raw_lines if l.strip()]
    n_norm = [normalize_for_diff(l) for l in new_raw_lines if l.strip()]

    if m_norm == n_norm:
        return ("", master_code)

    # Check if new_code is an exact tail subset of master (lecturer paused or scrolled slightly)
    if len(m_norm) >= len(n_norm) and m_norm[-len(n_norm):] == n_norm:
        return ("", master_code)

    matcher = difflib.SequenceMatcher(None, m_norm, n_norm)

    # If completely different file (lecturer switched tab, class, or project)
    if matcher.ratio() < 0.25 and len(m_norm) >= 4 and len(n_norm) >= 4:
        return (cleaned_new_code, cleaned_new_code)

    # Check if new_code starts from the top of the file (e.g. line 0 or 1 matches root)
    match_start = matcher.find_longest_match(0, min(3, len(m_norm)), 0, min(3, len(n_norm)))
    starts_at_top = (match_start.a == 0 and match_start.b == 0 and match_start.size >= 1)

    # If new_code starts at the top, it is an in-place update of the file.
    # It must NEVER append to the bottom of master_code!
    if starts_at_top:
        delta_lines = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("insert", "replace"):
                for k in range(j1, j2):
                    delta_lines.append(new_raw_lines[k])
        delta_text = "\n".join(delta_lines)
        return (delta_text, cleaned_new_code)

    # Check for scroll-down anchor: does new_code overlap with lines near the end of master_code?
    best_match = matcher.find_longest_match(0, len(m_norm), 0, len(n_norm))
    if best_match.size >= max(2, int(len(n_norm) * 0.30)):
        m_end = best_match.a + best_match.size
        n_end = best_match.b + best_match.size
        # Anchor is at the end of master, and new_lines has tail lines
        if m_end >= len(m_norm) - 1:
            tail_lines = new_raw_lines[n_end:]
            if tail_lines:
                new_master = master_code + "\n" + "\n".join(tail_lines)
                return ("\n".join(tail_lines), new_master)
            return ("", master_code)

    # Standard diff for inline additions or edits
    delta_norm_indices = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            for k in range(j1, j2):
                delta_norm_indices.append(k)

    if not delta_norm_indices:
        return ("", master_code)

    delta_lines = []
    norm_idx = 0
    for raw_l in new_raw_lines:
        if raw_l.strip():
            if norm_idx in delta_norm_indices:
                delta_lines.append(raw_l)
            norm_idx += 1
        else:
            if delta_lines:
                delta_lines.append("")

    while delta_lines and not delta_lines[-1].strip():
        delta_lines.pop()

    if not delta_lines:
        return ("", master_code)

    return ("\n".join(delta_lines), cleaned_new_code)


def is_likely_question(text: str, is_highlighted: bool = False) -> bool:
    """Detects if text is an assignment question, prompt, or comment instruction needing AI solving."""
    t = text.strip()
    if len(t) < 10:
        return False

    # 1. If text starts with code comment syntax, it is almost certainly a task/prompt comment!
    if t.startswith("//") or t.startswith("/*") or t.startswith("#") or t.startswith("--") or t.startswith("<!--"):
        return True

    # 2. If it was an active highlight on screen and does NOT look like pure runnable code, solve it!
    if is_highlighted and not (re.match(r"^(public\s+)?(class|interface|enum)\s+\w+\s*\{", t) or t.startswith("import ") or t.startswith("package ")):
        return True

    # Clean leading/trailing comment markers (/* ... */, // ..., # ...)
    clean_t = re.sub(r"^[/\*\s#\-]+", "", t)
    clean_t = re.sub(r"[/\*\s\-]+$", "", clean_t).strip()

    indicators = [
        r"\byou are\b",
        r"\bneeds to\b",
        r"\bscenario\b",
        r"\brequirements\b",
        r"\bwrite a\b",
        r"\bcreate a\b",
        r"\bimplement\b",
        r"\bwhich of the following\b",
        r"\bwhat (is|are|will be|does)\b",
        r"\bexplain\b",
        r"\bquestion\b",
        r"\bproblem\b",
        r"\btask\b",
        r"\bcalculate\b",
        r"\bgiven\b",
        r"\bconsider the following\b",
        r"\bfill in the blank\b",
        r"\boutput should be\b",
        r"\bexpected output\b",
        r"\binstructions?\b",
    ]

    for pat in indicators:
        if re.search(pat, clean_t, re.IGNORECASE):
            return True

    # Question mark check for theory/MCQ
    if clean_t.endswith("?") and not ("{" in clean_t or ";" in clean_t):
        return True

    return False


class ScreenBot:
    def __init__(self):
        self.config = load_config()
        self.solver = ScreenSolver(
            api_key=self.config.get("gemini_api_key"),
            model=self.config.get("model", "gemini-3.5-flash-lite"),
        )
        self.is_busy = False
        self.is_typing = False
        self.stop_typing_flag = False
        self.lock = threading.Lock()
        self.tray_icon = None
        self.current_subject = self.config.get("subject", "java")
        self.auto_type_enabled = self.config.get("auto_type_on_solve", False)
        self.pynput_ctrl = PynputController()
        self.last_trigger_time = 0.0
        self.last_code_shadow = ""
        self.last_lecture_frame = None
        self.last_injection_ended_with_newline = False
        self.target_file_path = self.config.get("target_file_path", None)
        self.direct_file_sync = self.config.get("direct_file_sync", False)
        self.auto_scan_enabled = False
        self.auto_scan_thread = None
        target_path = Path(self.target_file_path) if self.target_file_path else (SCRIPT_DIR / "shadow_master_code.txt")
        self.shadow_master = ShadowMaster(file_path=target_path, log_dir=SCRIPT_DIR / "session_data", max_snapshots=50)
        self.frame_archiver = FrameArchiver(session_dir=SCRIPT_DIR / "session_frames")
        self.stability_checker = FrameStabilityChecker(stability_threshold=0.02)
        self.occlusion_detector = OcclusionDetector()
        self.consensus_tracker = LineConsensusTracker(required_observations=2, min_confidence=0.85)
        self.merge_engine = MergeEngine()
        self.doc_registry = DocRegistry()
        self.ast_healer = HyperAwareCodeEngine(default_lang=self.current_subject)
        self.smart_synchronizer = SmartCodeSynchronizer(require_confirmations=2)
        self.smart_sync_controller = ScreenBotSmartController(
            workspace_roots=[Path.cwd(), SCRIPT_DIR, Path.home() / "Documents", Path.home() / "source"],
            require_confirmations=2,
            history_dir=SCRIPT_DIR / ".screenbot_history",
        )
        self.safe_injector = SafeInjector()
        self.circuit_breaker = CircuitBreaker(max_edits_per_minute=3)
        self.scan_watchdog = ScanWatchdog(timeout_seconds=30.0)
        self.session_reconciler = SessionReconciler(report_path=SCRIPT_DIR / "session_reconcile_report.md")
        self.build_hash = compute_build_hash()

    def check_and_set_busy(self, action_name: str) -> bool:
        """Atomically checks if bot is busy, typing, or in cooldown debounce.
        Returns True if allowed to proceed, False to discard duplicate keypress."""
        with self.lock:
            now = time.time()
            if self.is_busy:
                logger.info(f"ScreenBot is already busy, ignoring '{action_name}'.")
                return False
            if self.is_typing:
                logger.info(f"ScreenBot is currently typing, ignoring '{action_name}'.")
                return False
            if (now - self.last_trigger_time) < 1.5:
                logger.info(f"Ignored rapid duplicate trigger for '{action_name}' (cooldown active).")
                return False
            self.is_busy = True
            self.last_trigger_time = now
            return True

    def play_sound(self, sound_type: str):
        """Plays subtle audio cues in a background thread."""
        if not self.config.get("sound_feedback", True):
            return

        def _sound():
            try:
                import winsound
                if sound_type == "startup":
                    # Rising chime: bot online
                    winsound.Beep(1000, 60)
                    time.sleep(0.03)
                    winsound.Beep(1500, 90)
                elif sound_type == "trigger":
                    # Short discreet beep: capture started
                    winsound.Beep(950, 70)
                elif sound_type == "done":
                    # Two fast cheerful beeps: answer ready
                    winsound.Beep(1400, 60)
                    time.sleep(0.04)
                    winsound.Beep(1800, 90)
                elif sound_type == "typing":
                    # Soft tick: typing started
                    winsound.Beep(1200, 80)
                elif sound_type == "aborted":
                    # Distinct descending double tone: emergency stopped by user
                    winsound.Beep(850, 60)
                    time.sleep(0.02)
                    winsound.Beep(450, 90)
                elif sound_type == "error":
                    # Low buzz: error occurred
                    winsound.Beep(450, 250)
            except Exception:
                pass

        threading.Thread(target=_sound, daemon=True).start()

    def update_tray_tooltip(self, text: str):
        """Updates the hover tooltip on the system tray icon."""
        if self.tray_icon:
            try:
                self.tray_icon.title = text
            except Exception:
                pass

    def wait_for_modifiers_release(self):
        """Waits until user physically releases Ctrl, Alt, and Shift keys to avoid shortcut conflicts."""
        timeout = time.time() + 2.0
        while time.time() < timeout:
            try:
                if not (keyboard.is_pressed("ctrl") or keyboard.is_pressed("alt") or keyboard.is_pressed("shift")):
                    break
            except Exception:
                break
            time.sleep(0.04)

        # Force Windows to treat modifier keys as released in the system state table
        try:
            import ctypes
            user32 = ctypes.windll.user32
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)  # VK_CONTROL
            user32.keybd_event(0x10, 0, KEYEVENTF_KEYUP, 0)  # VK_SHIFT
            user32.keybd_event(0x12, 0, KEYEVENTF_KEYUP, 0)  # VK_MENU (Alt)
        except Exception:
            pass

        time.sleep(0.12)

    def interruptible_sleep(self, duration_s: float, user32=None) -> bool:
        """Sleeps for duration_s in small 12ms slices while checking for ESC key.
        Returns True if cancelled/interrupted, False if completed normally."""
        if duration_s <= 0:
            return False

        end_time = time.time() + duration_s
        while time.time() < end_time:
            if self.stop_typing_flag:
                return True
            if user32:
                try:
                    # 0x1B = VK_ESCAPE, 0x03 = VK_CANCEL
                    if (user32.GetAsyncKeyState(0x1B) & 0x8000) or (user32.GetAsyncKeyState(0x03) & 0x8000):
                        self.stop_typing_flag = True
                        return True
                except Exception:
                    pass
            time.sleep(0.012)
        return False

    def instant_paste(self, text: str):
        """Pastes text instantly using clipboard and hardware Ctrl+V with modifier guards.
        Preserves 100% exact indentation in IDEs (VS Code, IntelliJ, Eclipse) with zero syntax errors."""
        if not text:
            return
        try:
            self.wait_for_modifiers_release()
            pyperclip.copy(text)
            time.sleep(0.04)
            import ctypes
            user32 = ctypes.windll.user32
            VK_CONTROL = 0x11
            VK_V = 0x56
            KEYEVENTF_KEYUP = 0x0002

            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_V, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            logger.info(f"Instant paste injected ({len(text)} chars) directly into active window.")
        except Exception as e:
            logger.error(f"Error during instant paste, falling back to turbo typing: {e}")
            self.human_type(text, is_turbo=True)

    def select_all_and_paste(self, text: str):
        """Replaces entire editor buffer cleanly with updated code (Mirror mode)."""
        if not text:
            return
        try:
            self.wait_for_modifiers_release()
            pyperclip.copy(text)
            time.sleep(0.04)
            import ctypes
            user32 = ctypes.windll.user32
            VK_CONTROL = 0x11
            VK_A = 0x41
            VK_V = 0x56
            KEYEVENTF_KEYUP = 0x0002

            # Ctrl + A
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_A, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_A, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.02)
            # Ctrl + V
            user32.keybd_event(VK_V, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
            logger.info(f"Mirror paste injected ({len(text)} chars) replacing editor buffer.")
        except Exception as e:
            logger.error(f"Error during select_all_and_paste: {e}")
            self.instant_paste(text)

    def human_type(self, text: str, is_turbo: bool = False):
        """Simulates realistic student typing (dynamic ~35-40 WPM) or turbo speed,
        with micro-pauses and instant ESC cancel."""
        if not text:
            return

        with self.lock:
            if self.is_typing:
                logger.info("Already typing text, ignoring duplicate call.")
                return
            self.is_typing = True
            self.stop_typing_flag = False

        user32 = None
        try:
            import ctypes
            user32 = ctypes.windll.user32
            # Flush previous ESC key state
            user32.GetAsyncKeyState(0x1B)
        except Exception:
            pass

        try:
            # 1. Wait for physical keys (Ctrl, Alt, Shift) to be released so they don't combine with typed letters!
            self.wait_for_modifiers_release()

            clean_text = text.replace("\r\n", "\n").replace("\r", "\n")

            if is_turbo:
                self.play_sound("typing")
                logger.info(f"Starting Turbo typing injection ({len(clean_text)} chars)... Press ESC to stop immediately.")
                for char in clean_text:
                    if self.stop_typing_flag or (user32 and (user32.GetAsyncKeyState(0x1B) & 0x8000)):
                        self.stop_typing_flag = True
                        logger.info("Turbo typing cancelled immediately by ESC key.")
                        self.play_sound("aborted")
                        break
                    self.pynput_ctrl.type(char)
                    time.sleep(0.0015)
                if not self.stop_typing_flag:
                    logger.info("Turbo typing finished successfully.")
                return

            target_wpm = int(self.config.get("typing_wpm", 38))
            self.play_sound("typing")
            logger.info(f"Starting dynamic typing simulation ({len(clean_text)} chars, target ~{target_wpm} WPM)... Press ESC to stop immediately.")

            # Dynamic WPM drift: smoothly wanders around target WPM (e.g. 33 - 42 WPM)
            current_wpm = float(target_wpm)

            for i, char in enumerate(clean_text):
                # Instant ESC cancel check before typing
                if self.stop_typing_flag or (user32 and (user32.GetAsyncKeyState(0x1B) & 0x8000)):
                    self.stop_typing_flag = True
                    logger.info("Typing cancelled immediately by ESC key.")
                    self.play_sound("aborted")
                    break

                self.pynput_ctrl.type(char)

                # Dynamically drift WPM slightly every few characters (random walk)
                if i % 6 == 0:
                    drift = random.uniform(-1.5, 1.5)
                    current_wpm = max(26.0, min(current_wpm + drift, target_wpm + 4.0))

                # Standard: 1 word = 5 chars. Base char interval = 60 / (wpm * 5)
                # At 38 WPM, base char interval is ~0.315s
                base_interval = 60.0 / (max(10.0, current_wpm) * 5.0)

                # Realistic human keystroke timing distribution:
                if char == "\n":
                    # Natural thinking pause between code lines or sentences (0.6s - 1.2s at 38 WPM)
                    delay = base_interval * random.uniform(2.2, 3.8)
                elif char in (";", "{", "}"):
                    # Syntax milestone pause (0.35s - 0.65s)
                    delay = base_interval * random.uniform(1.2, 2.0)
                elif char in (".", ",", ":", "?", "!"):
                    # Punctuation pause (0.3s - 0.5s)
                    delay = base_interval * random.uniform(1.1, 1.7)
                elif char == " ":
                    # Word boundary transition (0.25s - 0.45s)
                    delay = base_interval * random.uniform(0.9, 1.4)
                elif char.isupper() or char in ("_", "-", "$", "@", "(", ")", "[", "]"):
                    # Shift key reach / symbol lookup delay (0.25s - 0.4s)
                    delay = base_interval * random.uniform(0.8, 1.3)
                else:
                    # Normal letter keystroke (0.15s - 0.28s)
                    delay = base_interval * random.uniform(0.55, 0.95)

                # Occasional human thinking hitch every 25-45 characters
                if i > 0 and (i % random.randint(28, 45) == 0):
                    delay += random.uniform(0.3, 0.6)

                # Sleep in small 12ms slices with live ESC key polling
                if self.interruptible_sleep(delay, user32):
                    logger.info("Typing cancelled during pause by ESC key.")
                    self.play_sound("aborted")
                    break

            if not self.stop_typing_flag:
                logger.info("Dynamic typing simulation finished successfully.")

        except Exception as e:
            logger.error(f"Error during human typing: {e}")
        finally:
            with self.lock:
                self.is_typing = False
                self.stop_typing_flag = False

    def cancel_typing(self):
        """Aborts ongoing typing immediately if user presses Escape or calls stop."""
        self.stop_typing_flag = True
        logger.info("Emergency abort requested for typing.")
        self.play_sound("aborted")

    def set_typing_wpm(self, wpm: int):
        """Sets the typing simulation speed in words per minute."""
        self.config["typing_wpm"] = wpm
        logger.info(f"Typing speed updated to: {wpm} WPM")

    def grab_highlighted_text(self) -> Optional[str]:
        """Attempts to copy whatever text is currently highlighted in the active window without polluting clipboard."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            seq_before = user32.GetClipboardSequenceNumber()

            # Hardware Ctrl+C
            VK_CONTROL = 0x11
            VK_C = 0x43
            KEYEVENTF_KEYUP = 0x0002

            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_C, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

            # Wait up to 300ms for clipboard sequence number to change
            t_end = time.time() + 0.30
            while time.time() < t_end:
                if user32.GetClipboardSequenceNumber() > seq_before:
                    break
                time.sleep(0.02)

            if user32.GetClipboardSequenceNumber() > seq_before:
                copied = pyperclip.paste()
                if copied and len(copied.strip()) > 1:
                    return copied.strip()
        except Exception as e:
            logger.debug(f"Could not grab highlighted text: {e}")
        return None

    def handle_type_or_solve(self):
        """Intelligently handles Ctrl+2 / Ctrl+Shift+V / Ctrl+7:
        1. If text is highlighted on screen: grabs it, drops cursor under comment, solves it, and types it.
        2. If nothing is highlighted: checks clipboard. If question, solves and types where cursor is.
           If already an answer/code, types where cursor is.
        """
        if not self.check_and_set_busy("Highlight & Solve / Type"):
            return
        threading.Thread(target=self._process_type_or_solve, daemon=True).start()

    def _process_type_or_solve(self):
        try:
            # 1. Wait for physical keys (Ctrl, 2, Shift, V) to be released
            self.wait_for_modifiers_release()

            # 2. Check if text is currently highlighted on screen
            grabbed_selection = self.grab_highlighted_text()

            was_highlighted = bool(grabbed_selection and len(grabbed_selection.strip()) > 2)
            target_text = ""
            if was_highlighted:
                target_text = grabbed_selection.strip()
                logger.info(f"Grabbed {len(target_text)} characters from active highlighted selection.")

                # Move cursor to end of the comment/instructions and drop to a fresh line under it
                try:
                    self.pynput_ctrl.tap(PynputKey.right)
                    time.sleep(0.04)
                    self.pynput_ctrl.tap(PynputKey.enter)
                    time.sleep(0.04)
                except Exception as e:
                    logger.debug(f"Could not reposition cursor after selection: {e}")
            else:
                # Fall back to clipboard content (e.g. user selected text, pressed Ctrl+C, then placed cursor)
                try:
                    target_text = pyperclip.paste().strip()
                except Exception:
                    target_text = ""

            if not target_text:
                logger.warning("No highlighted text and clipboard is empty.")
                self.play_sound("error")
                return

            # 3. Check if target_text is a question/prompt that needs to be solved
            if is_likely_question(target_text, is_highlighted=was_highlighted):
                logger.info("Detected question/instructions in text! Calling Gemini AI to solve first...")
                self.play_sound("trigger")
                self.update_tray_tooltip(f"ScreenBot: Solving Question ({self.current_subject.upper()})...")

                t0 = time.time()
                solution = self.solver.solve_text(
                    target_text,
                    mode="concise",
                    subject=self.current_subject,
                )
                logger.info(f"Question solved in {(time.time() - t0)*1000:.1f}ms! Updating clipboard and typing solution...")

                # Update clipboard with the final code solution so the user has it saved
                try:
                    pyperclip.copy(solution)
                except Exception:
                    pass

                self.play_sound("done")
                self.update_tray_tooltip("ScreenBot: Typing Answer (ESC to stop)...")

                # Type the generated solution at the current cursor position!
                self.human_type(solution)

            else:
                # Text is already code or an answer; type it directly where cursor is
                logger.info(f"Typing existing answer/code ({len(target_text)} chars) where cursor is...")
                self.human_type(target_text)

        except Exception as e:
            logger.error(f"Error in handle_type_or_solve: {e}", exc_info=True)
            self.play_sound("error")
        finally:
            hotkey = self.config.get("hotkey", "ctrl+0")
            self.update_tray_tooltip(f"ScreenBot: Ready [{self.current_subject.upper()}] ({hotkey.upper()})")
            with self.lock:
                self.is_busy = False

    def type_clipboard(self):
        """Legacy helper pointing to smart handle_type_or_solve."""
        self.handle_type_or_solve()

    def process_screen(self, mode: str = "concise"):
        """Worker thread to capture screen, query AI, and output solution."""
        hotkey = self.config.get("hotkey", "ctrl+0")
        try:
            t0 = time.time()
            self.play_sound("trigger")
            self.update_tray_tooltip(f"ScreenBot: Solving ({self.current_subject.upper()})...")
            logger.info(f"Hotkey triggered (mode={mode}, subject={self.current_subject}). Capturing screen...")

            # 1. Capture screen or active window
            img_bytes = capture_screen_bytes(
                mode=self.config.get("capture_mode", "active_window"),
                max_dim=self.config.get("max_image_dimension", 1920),
                quality=self.config.get("image_quality", 85),
            )
            t_cap = time.time()
            logger.info(f"Capture completed in {(t_cap - t0)*1000:.1f}ms. Calling Gemini AI...")

            # 2. Query Gemini AI Vision with dynamic subject and anti-plagiarism seed
            answer = self.solver.solve(img_bytes, mode=mode, subject=self.current_subject)
            t_ai = time.time()
            logger.info(f"AI response received in {(t_ai - t_cap)*1000:.1f}ms.")

            # 3. Copy to clipboard
            pyperclip.copy(answer)
            logger.info(f"Answer copied to clipboard! (Total time: {(t_ai - t0):.2f}s)")
            logger.info(f"Snippet: {repr(answer[:80])}...")

            self.play_sound("done")

            # 4. If auto-type on solve is enabled, type directly into the focused field
            if self.auto_type_enabled:
                threading.Thread(target=self.human_type, args=(answer,), daemon=True).start()

        except Exception as e:
            logger.error(f"Error processing screen: {e}", exc_info=True)
            self.play_sound("error")
            pyperclip.copy(f"[ScreenBot Error]: {str(e)}")

        finally:
            self.update_tray_tooltip(f"ScreenBot: Ready ({hotkey.upper()})")
            with self.lock:
                self.is_busy = False

    def trigger_concise(self):
        """Triggered on Ctrl+0 (Direct answer & smart code completion)."""
        if not self.check_and_set_busy("Solve Screen"):
            return
        threading.Thread(target=self.process_screen, args=("concise",), daemon=True).start()

    def trigger_explain(self):
        """Triggered on Ctrl+9 (Detailed explanation)."""
        if not self.check_and_set_busy("Explain Screen"):
            return
        threading.Thread(target=self.process_screen, args=("explain",), daemon=True).start()

    def trigger_paraphrase(self):
        """Triggered on Ctrl+8 (Extra anti-plagiarism variation)."""
        if not self.check_and_set_busy("Paraphrase"):
            return
        threading.Thread(target=self.process_screen, args=("paraphrase",), daemon=True).start()

    def trigger_force_code_shadow(self):
        """Triggered on Ctrl+Shift+\\ to force re-inject the full code without delta diffing."""
        if not self.check_and_set_busy("Force Full Code Sync"):
            return
        threading.Thread(target=self.process_code_shadow, kwargs={"force_full": True}, daemon=True).start()

    def trigger_code_shadow(self):
        """Triggered on Ctrl+\\ or Ctrl+3 (Incremental Live Code Follower with delta diffing)."""
        if not self.check_and_set_busy("Live Code Follower"):
            return
        threading.Thread(target=self.process_code_shadow, kwargs={"force_full": False}, daemon=True).start()

    def get_or_detect_target_file(self, hint_file_name: Optional[str] = None) -> Optional[Path]:
        """Dynamically resolves the target file path for Direct File Sync across ANY active file.
        1. Checks active foreground editor window title (e.g., 'DateTimeSpec.cs - Visual Studio Code').
        2. Checks hint_file_name extracted from lecturer's screen tab.
        3. Falls back to configured target_file_path or auto-creates in active project."""
        user32 = ctypes.windll.user32
        target_fname = hint_file_name

        # 1. Inspect foreground window title
        try:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value
                    match = re.search(r"([a-zA-Z0-9_\-\.]+\.(cs|java|py|c|cpp|js|ts|html|css|sql|php|rb|go|rs))\b", title, re.IGNORECASE)
                    if match:
                        target_fname = match.group(1)
        except Exception as e:
            logger.debug(f"Title file detect error: {e}")

        # 2. Search dynamically across project directories for target_fname
        if target_fname:
            onedrive_docs = Path.home() / "OneDrive - Edulink International College" / "Documents"
            search_dirs = [
                Path.cwd(),
                onedrive_docs / "adse_2509",
                onedrive_docs,
                SCRIPT_DIR,
                Path.home() / "Documents",
                Path.home() / "Downloads",
            ]
            for s_dir in search_dirs:
                if not s_dir.exists():
                    continue
                # Exact file in root of s_dir
                candidate = s_dir / target_fname
                if candidate.is_file():
                    return candidate
                # Recursive search in subfolders
                try:
                    for sub in s_dir.rglob(target_fname):
                        if sub.is_file():
                            return sub
                except Exception:
                    pass

        # 3. Fallback to explicitly configured target_file_path if valid
        if self.target_file_path:
            p = Path(self.target_file_path)
            if p.is_file():
                return p

        return None

    def write_code_to_file(self, file_path: Path, code: str) -> bool:
        """Atomic safe write to source file on disk."""
        try:
            tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
            tmp_path.write_text(code, encoding="utf-8")
            os.replace(tmp_path, file_path)
            return True
        except Exception as e:
            logger.error(f"Failed to write code to file {file_path}: {e}")
            return False


    def undo_last_edit(self):
        """Rolls back the master code to the previous snapshot."""
        undone = self.smart_sync_controller.undo_last() or self.shadow_master.undo()
        if undone:
            self.play_sound("done")
            logger.info("Undo: Restored previous code snapshot.")
            if self.tray_icon:
                try:
                    self.tray_icon.notify("Restored previous code snapshot.", "ScreenBot Undo")
                except Exception:
                    pass
        else:
            self.play_sound("error")

    def bind_active_editor_file(self):
        """Binds foreground editor file for zero-keystroke Direct File Sync AND immediately syncs code!"""
        target = self.get_or_detect_target_file()
        if target:
            self.direct_file_sync = True
            self.target_file_path = str(target)
            self.shadow_master.file_path = target
            self.play_sound("done")
            logger.info(f"Direct File Sync bound to: {target}")
            if self.tray_icon:
                try:
                    self.tray_icon.notify(
                        f"Bound directly to {target.name}!\nSyncing code from lecture now...",
                        "Direct File Sync Active"
                    )
                except Exception:
                    pass
            # Trigger immediate sync right now!
            threading.Thread(target=self.process_code_shadow, kwargs={"force_full": False}, daemon=True).start()
        else:
            # If no file found on disk, still do immediate Caret Sync so user gets code instantly!
            self.play_sound("trigger")
            logger.info("No file on disk to bind, triggering immediate Caret Sync...")
            threading.Thread(target=self.process_code_shadow, kwargs={"force_full": False}, daemon=True).start()

    def process_code_shadow(self, is_auto: bool = False, force_full: bool = False):
        """Captures lecturer's screen/video (YouTube, Teams Web/App, Zoom),
        runs pixel gatekeeper, archives raw frame, extracts structured JSON code lines,
        enforces 2-consecutive-frame consensus gate, deterministic syntax validation,
        append-only middle-insertion merge into ShadowMaster, and persists atomically."""
        hotkey = self.config.get("hotkey", "ctrl+0")
        try:
            t0 = time.time()
            frame_id = f"frame_{int(t0 * 1000)}"
            if not is_auto:
                self.play_sound("trigger")
                self.update_tray_tooltip(f"ScreenBot: Capturing Lecture Code ({self.current_subject.upper()})...")
                logger.info(f"[{frame_id}] Live Code Follower triggered (subject={self.current_subject}). Snapping lecture...")

            # 1. Capture screen: both PIL Image (for local gates) and JPEG bytes (for AI)
            pil_img, img_bytes = capture_screen_image_and_bytes(
                mode="lecture",
                max_dim=self.config.get("max_image_dimension", 1920),
                quality=self.config.get("image_quality", 85),
            )
            t_cap = time.time()

            # 1b. Black/Blank / Locked Display Check
            if is_frame_black_or_blank(pil_img):
                logger.warning(f"[{frame_id}] Captured frame is black or blank (display locked/sleeping). Skipping cycle.")
                return

            # 2. LOCAL PIXEL GATEKEEPER (Auto-Scan Only):
            if is_auto and not force_full and self.last_lecture_frame is not None:
                p_diff = compute_frame_diff(self.last_lecture_frame, pil_img)
                if p_diff < 0.015:
                    logger.debug(f"[{frame_id}] Lecture screen pixel diff={p_diff:.4f} < threshold. Screen is static, skipping AI call.")
                    return

            self.last_lecture_frame = pil_img

            # 3. Ring buffer archive
            try:
                self.frame_archiver.archive_frame(pil_img, frame_id=frame_id)
            except Exception as e:
                logger.warning(f"[{frame_id}] Frame archive warning: {e}")

            # 4. Occlusion / Overlay detection
            occlusions = self.occlusion_detector.detect_occlusions(pil_img)
            if occlusions:
                logger.info(f"[{frame_id}] Detected {len(occlusions)} occlusion/overlay region(s).")

            if not is_auto:
                logger.info(f"[{frame_id}] Capture & local gates finished in {(t_cap - t0)*1000:.1f}ms. Calling Gemini Vision...")

            # 5. Extract structured code lines via AI
            prompt = build_extraction_prompt(language_hint=self.current_subject)
            t_ai_start = time.time()
            raw_resp = self.solver.extract_lecture_code_structured(img_bytes, prompt=prompt)
            t_ai_end = time.time()
            logger.info(f"[{frame_id}] AI Vision responded in {(t_ai_end - t_ai_start)*1000:.1f}ms.")

            if not raw_resp or raw_resp.startswith("[ScreenBot] Error"):
                if not is_auto:
                    logger.warning(f"[{frame_id}] AI extraction returned error: {raw_resp}")
                    self.play_sound("error")
                return

            parsed = parse_model_extraction_response(raw_resp)
            if not parsed.lines:
                logger.info(f"[{frame_id}] No valid code lines returned by model.")
                return

            # 6. Consensus Gate (2 consecutive stable frames, confidence >= 0.85)
            accepted, pending = self.consensus_tracker.feed_frame_lines(parsed.lines)
            logger.info(f"[{frame_id}] Consensus Gate: {len(accepted)} accepted, {len(pending)} pending 2nd observation.")

            if not accepted:
                logger.info(f"[{frame_id}] 0 lines passed 2-frame consensus; master code untouched.")
                return

            # 7. Merge Engine via DocMerger & Plan Injection (no duplicates, entity-level replacement)
            target = self.get_or_detect_target_file(hint_file_name=parsed.file_name)
            target_fname = target.name if target else (parsed.file_name or "lecture.cs")
            doc = self.doc_registry.get(target_fname)

            # Seed baseline from disk if doc is fresh or empty
            disk_code = ""
            if target and target.exists():
                try:
                    disk_code = target.read_text(encoding="utf-8")
                    if disk_code.strip() and not doc.lines:
                        doc.seed_baseline(disk_code)
                except Exception:
                    pass

            doc_lines = [DocFrameLine(text=l.text, line_no=l.line_no, visible=l.visible, confidence=l.confidence) for l in accepted]
            meta = DocMeta(file_name=target_fname, quality_ok=True, overlay=bool(occlusions))
            merge_res = doc.merge(doc_lines, meta=meta)
            logger.info(f"[{frame_id}] DocMerge Event: {merge_res.event}, notes={merge_res.notes}")

            # Fallback to MergeEngine if doc.text is empty
            merged_code = doc.text if doc.text.strip() else self.merge_engine.merge(disk_code, accepted).merged_code

            if not merged_code.strip() or merge_res.event in ("noop", "blocked", "low_quality", "held"):
                logger.info(f"[{frame_id}] No changes to apply (event={merge_res.event}). Master is already consistent.")
                return

            # 8. AST Syntax Validation & Deterministic Healing
            healed = self.ast_healer.heal_entire_file(merged_code, filename=target_fname)
            if healed.was_healed:
                logger.info(f"[{frame_id}] AST Healer resolved issues: {healed.fixed_issues}")
                merged_code = healed.code

            is_valid_syntax, syntax_err = validate_syntax(merged_code, language=self.current_subject)
            if not is_valid_syntax and healed.needs_llm_repair:
                logger.warning(f"[{frame_id}] Syntax Gate flagged errors ({syntax_err}); attempting surgical AST LLM repair...")
                repair_prompt = self.ast_healer.build_llm_repair_prompt(
                    current_file_code=disk_code or merged_code,
                    new_screen_code=merged_code,
                    issues=healed.remaining_issues,
                )
                repaired_resp = self.solver.solve_text(repair_prompt, mode="concise", subject=self.current_subject)
                if repaired_resp and not repaired_resp.startswith("[ScreenBot] Error"):
                    is_rep_valid, _ = validate_syntax(repaired_resp, language=self.current_subject)
                    if is_rep_valid:
                        merged_code = repaired_resp
                        is_valid_syntax = True
                        logger.info(f"[{frame_id}] Surgical LLM repair succeeded!")

            if not is_valid_syntax:
                logger.warning(f"[{frame_id}] Syntax Gate FAILED ({syntax_err}). Holding changes as pending.")
                self.session_reconciler.record_flagged_item(FlaggedItem(
                    line_no=0,
                    raw_text="SYNTAX_CHECK_FAIL",
                    reason=f"Syntax error: {syntax_err}",
                    confidence=0.0,
                    frame_id=frame_id,
                ))
                return

            # 9. Atomic Update to Shadow Master & JSONL Event Log
            change_event = MasterChangeEvent(
                frame_id=frame_id,
                event_type=merge_res.event,
                lines_added=[i + 1 for i, _ in enumerate(merge_res.inserted)],
                lines_modified=[],
                gates_passed=["stability_gate", "consensus_gate", "syntax_gate"],
                result_code=merged_code,
            )
            self.shadow_master.apply_change(change_event)
            self.last_code_shadow = merged_code
            logger.info(f"[{frame_id}] Shadow Master updated ({len(merged_code.splitlines())} lines). Total cycle: {(time.time() - t0):.2f}s.")

            # 10. Persistence / Direct File Sync (Never repeats existing user code)
            if self.direct_file_sync and target and target.exists():
                steps, conflicts = plan_injection(merged_code, disk_code)
                if steps:
                    final_disk_code = apply_plan(disk_code, steps)
                    self.write_code_to_file(target, final_disk_code)
                    logger.info(f"[{frame_id}] Direct File Sync applied {len(steps)} plan steps to: {target}")
                elif not disk_code.strip():
                    self.write_code_to_file(target, merged_code)
                    logger.info(f"[{frame_id}] Direct File Sync initialized empty file: {target}")
                else:
                    logger.info(f"[{frame_id}] File on disk already contains all merged code.")
            else:
                # Caret Mode: inject ONLY missing lines planned against target
                steps, conflicts = plan_injection(merged_code, disk_code)
                delta_lines = []
                for s in steps:
                    if isinstance(s, tuple) and len(s) == 4 and s[0] == "replace":
                        delta_lines.extend(s[3])
                    else:
                        delta_lines.extend(s[1])
                delta_text = "\n".join(delta_lines)
                if delta_text:
                    self.inject_lecture_code(delta_text, is_delta=True, full_master=merged_code)
                    logger.info(f"[{frame_id}] Caret Delta Injected ({len(delta_lines)} new lines).")

        except Exception as e:
            logger.error(f"Error in process_code_shadow: {e}", exc_info=True)
            if not is_auto:
                self.play_sound("error")
        finally:
            self.update_tray_tooltip(f"ScreenBot: Ready [{self.current_subject.upper()}] ({hotkey.upper()})")
            with self.lock:
                self.is_busy = False

    def inject_lecture_code(self, code_text: str, is_delta: bool = True, full_master: str = ""):
        """Injects lecture code:
        If Direct File Sync is active: writes the master code directly to the source file on disk.
        Otherwise: uses Smart Caret Injection (dedented, drop to next line, trailing newline).
        """
        if not code_text and not full_master:
            return

        # 1. DIRECT FILE SYNC MODE (Zero-Keystroke Toyota Hilux Mode)
        if self.direct_file_sync:
            target = self.get_or_detect_target_file()
            if target and target.exists():
                code_to_write = full_master if full_master else code_text
                if self.write_code_to_file(target, code_to_write):
                    self.play_sound("done")
                    self.update_tray_tooltip(f"ScreenBot: Synced to {target.name}")
                    logger.info(f"Direct File Sync succeeded -> {target} ({len(code_to_write)} chars).")
                    if self.tray_icon:
                        try:
                            self.tray_icon.notify(
                                f"Synced code into {target.name} ({len(code_text)} new chars)!",
                                "ScreenBot Sync Complete"
                            )
                        except Exception:
                            pass
                    return
                else:
                    logger.warning(f"Direct file write to {target} failed, falling back to Caret Mode.")

        # 2. SMART CARET INJECTION MODE
        dedented = textwrap.dedent(code_text).strip()
        if not dedented:
            return

        payload = dedented + "\n"

        if is_delta and not self.last_injection_ended_with_newline:
            try:
                self.pynput_ctrl.tap(PynputKey.end)
                time.sleep(0.02)
                self.pynput_ctrl.tap(PynputKey.enter)
                time.sleep(0.03)
            except Exception as e:
                logger.debug(f"Cursor prep error: {e}")

        injection_mode = self.config.get("lecture_injection_mode", "paste")
        self.update_tray_tooltip("ScreenBot: Injecting New Code...")
        self.play_sound("done")

        if injection_mode == "turbo_type":
            self.human_type(payload, is_turbo=True)
        else:
            self.instant_paste(payload)

        if self.tray_icon:
            try:
                self.tray_icon.notify(
                    f"Pasted new code at cursor ({len(code_text)} chars)!",
                    "ScreenBot Paste Complete"
                )
            except Exception:
                pass

        self.last_injection_ended_with_newline = True

    def toggle_direct_file_sync(self):
        """Toggles zero-keystroke Direct File Sync mode."""
        self.direct_file_sync = not self.direct_file_sync
        self.config["direct_file_sync"] = self.direct_file_sync
        mode_str = "ENABLED" if self.direct_file_sync else "DISABLED"
        logger.info(f"Direct File Sync mode: {mode_str}")
        self.play_sound("done" if self.direct_file_sync else "aborted")
        if self.tray_icon:
            try:
                target = self.get_or_detect_target_file()
                target_name = target.name if target else "Auto-Detecting on active window"
                status_msg = (
                    f"Direct File Sync is ON ({target_name})\nCode writes straight to disk with zero keystrokes!"
                    if self.direct_file_sync
                    else "Direct File Sync is OFF (Caret paste mode active)"
                )
                self.tray_icon.notify(status_msg, "ScreenBot File Sync")
            except Exception:
                pass

    def toggle_auto_scan(self):
        """Toggles the automatic interval-based lecture code scanner."""
        self.auto_scan_enabled = not self.auto_scan_enabled
        interval = int(self.config.get("lecture_auto_scan_interval", 5))
        if self.auto_scan_enabled:
            self.play_sound("startup")
            logger.info(f"Auto-Scan Live Code Follower ENABLED (every {interval}s).")
            self.update_tray_tooltip(f"ScreenBot: Auto-Scan Active ({interval}s)")
            if self.tray_icon:
                try:
                    self.tray_icon.notify(
                        f"Auto-Scan Mode is ON!\nScanning every {interval}s for new lines of code.\nPress Ctrl+Alt+\\ to turn off.",
                        "ScreenBot Auto-Scan Active"
                    )
                except Exception:
                    pass
            self.auto_scan_thread = threading.Thread(target=self._auto_scan_loop, daemon=True)
            self.auto_scan_thread.start()
        else:
            self.play_sound("aborted")
            logger.info("Auto-Scan Live Code Follower DISABLED.")
            self.update_tray_tooltip("ScreenBot: Ready")
            if self.tray_icon:
                try:
                    self.tray_icon.notify("Auto-Scan Mode has been turned OFF.", "ScreenBot Auto-Scan Disabled")
                except Exception:
                    pass

    def _auto_scan_loop(self):
        """Background loop scanning lecture split-screen every N seconds for newly added code lines."""
        interval = float(self.config.get("lecture_auto_scan_interval", 5))
        while self.auto_scan_enabled:
            t_end = time.time() + interval
            while time.time() < t_end and self.auto_scan_enabled:
                time.sleep(0.3)

            if not self.auto_scan_enabled:
                break

            # If student is actively typing in Caret Mode (last 0.6s), pause to avoid caret conflict
            # In Direct File Sync mode, we write straight to disk so user typing is never interrupted!
            if not self.direct_file_sync and get_idle_time_seconds() < 0.6:
                self.last_injection_ended_with_newline = False
                continue

            with self.lock:
                if self.is_busy or self.is_typing:
                    continue
                self.is_busy = True

            self.process_code_shadow(is_auto=True, force_full=False)

    def trigger_note_taker(self):
        """Triggered on Ctrl+4 (AI Lecture Note-Taker for slides, presentations, YouTube, Teams)."""
        if not self.check_and_set_busy("Lecture Note-Taker"):
            return
        threading.Thread(target=self.process_note_taker, daemon=True).start()

    def process_note_taker(self):
        """Captures lecture slides/whiteboard (YouTube, Teams Web/App, Zoom, Meet),
        summarizes into Cornell-style study notes, and injects directly into Word/OneNote/Notion."""
        hotkey = self.config.get("hotkey", "ctrl+0")
        try:
            t0 = time.time()
            self.play_sound("trigger")
            self.update_tray_tooltip(f"ScreenBot: Generating Study Notes ({self.current_subject.upper()})...")
            logger.info("AI Lecture Note-Taker triggered (Ctrl+4). Snapping lecture slide...")

            # 1. Capture opposite split-screen (or full screen if not split)
            img_bytes = capture_screen_bytes(
                mode="lecture",
                max_dim=self.config.get("max_image_dimension", 1920),
                quality=self.config.get("image_quality", 85),
            )
            t_cap = time.time()
            logger.info(f"Lecture capture finished in {(t_cap - t0)*1000:.1f}ms. Generating notes with Gemini...")

            # 2. Generate structured Cornell study notes via AI
            notes = self.solver.generate_lecture_notes(img_bytes, subject=self.current_subject)
            t_ai = time.time()
            logger.info(f"Study notes generated in {(t_ai - t_cap)*1000:.1f}ms ({len(notes)} chars).")

            if not notes or notes.startswith("[ScreenBot] Error"):
                logger.warning(f"Could not generate notes: {notes}")
                self.play_sound("error")
                return

            # 3. Save to clipboard
            pyperclip.copy(notes)
            self.play_sound("done")

            # 4. Inject notes into active document (Word / OneNote / Notion)
            injection_mode = self.config.get("lecture_injection_mode", "paste")
            self.update_tray_tooltip("ScreenBot: Injecting Study Notes...")
            if injection_mode == "turbo_type":
                self.human_type(notes, is_turbo=True)
            else:
                self.instant_paste(notes)

            logger.info(f"Notes successfully injected in {(time.time() - t0):.2f}s total!")

        except Exception as e:
            logger.error(f"Error in process_note_taker: {e}", exc_info=True)
            self.play_sound("error")
        finally:
            self.update_tray_tooltip(f"ScreenBot: Ready [{self.current_subject.upper()}] ({hotkey.upper()})")
            with self.lock:
                self.is_busy = False

    def set_subject(self, subject: str):
        """Switches active Aptech subject in real-time."""
        self.current_subject = subject
        logger.info(f"Switched active subject to: {subject.upper()}")
        self.update_tray_tooltip(f"ScreenBot: Ready [{subject.upper()}] (CTRL+0)")

    def toggle_auto_type(self):
        """Toggles automatic typing on completion."""
        self.auto_type_enabled = not self.auto_type_enabled
        logger.info(f"Auto-Type on Solve toggled to: {self.auto_type_enabled}")

    def open_config(self):
        """Opens config.json with the default text editor."""
        try:
            os.startfile(str(CONFIG_PATH))
        except Exception as e:
            logger.error(f"Could not open config: {e}")

    def open_log(self):
        """Opens screen_bot.log with the default text editor."""
        try:
            os.startfile(str(LOG_FILE))
        except Exception as e:
            logger.error(f"Could not open log: {e}")

    def stop(self, icon=None, item=None):
        """Terminates ScreenBot cleanly."""
        logger.info("ScreenBot exiting via tray menu...")
        try:
            if hasattr(self, "session_reconciler") and hasattr(self, "shadow_master"):
                self.session_reconciler.generate_report(self.shadow_master.get_current_code())
                logger.info("Session reconcile report written to session_reconcile_report.md.")
        except Exception as e:
            logger.debug(f"Could not generate reconcile report: {e}")
        try:
            keyboard.unhook_all()
        except Exception:
            pass

        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        sys.exit(0)

    def start(self):
        """Registers hotkeys, creates system tray icon, and enters event loop."""
        hotkey = self.config.get("hotkey", "ctrl+0")
        hotkey_explain = self.config.get("hotkey_explain", "ctrl+9")
        hotkey_paraphrase = self.config.get("hotkey_paraphrase", "ctrl+8")
        hotkey_type = self.config.get("hotkey_type", "ctrl+2")
        hotkey_code_shadow = self.config.get("hotkey_code_shadow", "ctrl+\\")
        hotkey_force_sync = self.config.get("hotkey_force_sync", "ctrl+shift+\\")
        hotkey_auto_scan = self.config.get("hotkey_auto_scan", "ctrl+alt+\\")
        hotkey_note_taker = self.config.get("hotkey_note_taker", "ctrl+4")
        scan_interval = int(self.config.get("lecture_auto_scan_interval", 5))

        target_wpm = int(self.config.get("typing_wpm", 38))
        logger.info("=" * 60)
        logger.info("ScreenBot Pro started!")
        logger.info(f"  Primary Solve Hotkey (Direct Answer)       : {hotkey}")
        logger.info(f"  Explain Hotkey (Detailed Rationale)       : {hotkey_explain}")
        logger.info(f"  Paraphrase Hotkey (Anti-Plagiarism)       : {hotkey_paraphrase}")
        logger.info(f"  Highlight & Solve / Type Hotkey           : {hotkey_type.upper()} (also Ctrl+Shift+V)")
        logger.info(f"  Live Code Follower (Incremental Delta)    : {hotkey_code_shadow.upper()} (also Ctrl+3)")
        logger.info(f"  Force Full Code Re-Sync                   : {hotkey_force_sync.upper()}")
        logger.info(f"  Auto-Scan Loop (60 WPM benchmark)         : {hotkey_auto_scan.upper()} (Every {scan_interval}s)")
        logger.info(f"  Lecture Note-Taker (Cornell Study Notes)  : {hotkey_note_taker.upper()} (Slides to Word/OneNote)")
        logger.info(f"  Active Subject                            : {self.current_subject.upper()}")
        logger.info(f"  Auto-Scan Enabled on Startup              : {self.config.get('auto_scan_enabled', True)}")
        logger.info("=" * 60)

        # Register global solve hotkeys
        keyboard.add_hotkey(hotkey, self.trigger_concise)
        if hotkey_explain:
            keyboard.add_hotkey(hotkey_explain, self.trigger_explain)
        if hotkey_paraphrase:
            keyboard.add_hotkey(hotkey_paraphrase, self.trigger_paraphrase)

        # Register Live Code Follower: Incremental Delta (Ctrl+\ and Ctrl+3)
        keyboard.add_hotkey(hotkey_code_shadow, self.trigger_code_shadow)
        if hotkey_code_shadow.lower() != "ctrl+\\":
            try:
                keyboard.add_hotkey("ctrl+\\", self.trigger_code_shadow)
            except Exception:
                pass
        if hotkey_code_shadow.lower() != "ctrl+3":
            try:
                keyboard.add_hotkey("ctrl+3", self.trigger_code_shadow)
            except Exception:
                pass

        # Force Full Code Re-Sync (Ctrl+Shift+\)
        keyboard.add_hotkey(hotkey_force_sync, self.trigger_force_code_shadow)

        # Bind Active File to Direct File Sync (Ctrl+Shift+F)
        keyboard.add_hotkey("ctrl+shift+f", self.bind_active_editor_file)

        # Toggle Auto-Scan Loop (Ctrl+Alt+\)
        keyboard.add_hotkey(hotkey_auto_scan, self.toggle_auto_scan)

        # Lecture Note-Taker (Ctrl+4)
        keyboard.add_hotkey(hotkey_note_taker, self.trigger_note_taker)

        # Register Highlight-and-Solve / Auto-Typer hotkeys (Primary: Ctrl+2)
        keyboard.add_hotkey("ctrl+2", self.handle_type_or_solve)
        if hotkey_type and hotkey_type.lower() != "ctrl+2":
            try:
                keyboard.add_hotkey(hotkey_type.lower(), self.handle_type_or_solve)
            except Exception:
                pass
        keyboard.add_hotkey("ctrl+shift+v", self.handle_type_or_solve)
        keyboard.add_hotkey("ctrl+alt+v", self.handle_type_or_solve)
        keyboard.add_hotkey("ctrl+7", self.handle_type_or_solve)

        # Kill switch hotkey Ctrl+Alt+Esc (not bare ESC) and undo hotkey Ctrl+Alt+Z
        keyboard.add_hotkey("ctrl+alt+esc", lambda: self.stop())
        keyboard.add_hotkey("ctrl+alt+z", self.undo_last_edit)
        keyboard.add_hotkey("esc", self.cancel_typing)
        keyboard.add_hotkey("ctrl+shift+x", self.cancel_typing)
        keyboard.add_hotkey("ctrl+shift+q", lambda: self.stop())

        # Build System Tray Menu
        icon_img = get_or_create_icon_image()
        tooltip_text = f"ScreenBot: Ready [{self.current_subject.upper()}] ({hotkey.upper()})"

        def make_action(k):
            return lambda icon, item: self.set_subject(k)

        def make_checked(k):
            return lambda item: self.current_subject == k

        def subject_item(title, key):
            return pystray.MenuItem(
                title,
                make_action(key),
                checked=make_checked(key),
            )

        def make_speed_action(w):
            return lambda icon, item: self.set_typing_wpm(w)

        def make_speed_checked(w):
            return lambda item: int(self.config.get("typing_wpm", 38)) == w

        def speed_item(title, wpm):
            return pystray.MenuItem(
                title,
                make_speed_action(wpm),
                checked=make_speed_checked(wpm),
            )

        def set_injection_mode(mode_val):
            self.config["lecture_injection_mode"] = mode_val
            logger.info(f"Class Mode injection set to: {mode_val}")

        def injection_mode_item(title, mode_val):
            return pystray.MenuItem(
                title,
                lambda icon, item: set_injection_mode(mode_val),
                checked=lambda item: self.config.get("lecture_injection_mode", "paste") == mode_val,
            )

        def set_sync_mode(mode_val):
            self.config["code_sync_mode"] = mode_val
            logger.info(f"Code sync mode set to: {mode_val}")

        def sync_mode_item(title, mode_val):
            return pystray.MenuItem(
                title,
                lambda icon, item: set_sync_mode(mode_val),
                checked=lambda item: self.config.get("code_sync_mode", "delta") == mode_val,
            )

        menu_items = [
            pystray.MenuItem(f"ScreenBot: Ready ({hotkey.upper()})", None, enabled=False),
            pystray.MenuItem("Solve Screen (Ctrl+0)", lambda icon, item: self.trigger_concise()),
            pystray.MenuItem("Explain Screen (Ctrl+9)", lambda icon, item: self.trigger_explain()),
            pystray.MenuItem("Paraphrase / Unique Phrasing (Ctrl+8)", lambda icon, item: self.trigger_paraphrase()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Type Answer / Solve Highlighted (Ctrl+2)", lambda icon, item: self.handle_type_or_solve()),
            pystray.MenuItem("Sync New Lines Now (Ctrl+\\)", lambda icon, item: self.trigger_code_shadow()),
            pystray.MenuItem("Force Full Re-Sync (Ctrl+Shift+\\)", lambda icon, item: self.trigger_force_code_shadow()),
            pystray.MenuItem(
                "⚡ Direct File Sync (Zero Keystrokes)",
                lambda icon, item: self.toggle_direct_file_sync(),
                checked=lambda item: self.direct_file_sync,
            ),
            pystray.MenuItem("Bind Active Editor File (Ctrl+Shift+F)", lambda icon, item: self.bind_active_editor_file()),
            pystray.MenuItem(
                f"⚡ Auto-Scan Live Coding ({scan_interval}s interval)",
                lambda icon, item: self.toggle_auto_scan(),
                checked=lambda item: self.auto_scan_enabled,
            ),
            pystray.MenuItem("Lecture Note-Taker / Slides (Ctrl+4)", lambda icon, item: self.trigger_note_taker()),
            pystray.MenuItem("⏹ Emergency Stop Typing (ESC)", lambda icon, item: self.cancel_typing()),
            pystray.MenuItem(
                "Code Follower Mode",
                pystray.Menu(
                    sync_mode_item("Incremental Delta (Only New Lines at Cursor) [Default]", "delta"),
                    sync_mode_item("Full Buffer Mirror (Select All & Replace)", "mirror"),
                ),
            ),
            pystray.MenuItem(
                "Typing Speed (WPM)",
                pystray.Menu(
                    speed_item("Dynamic Human (35-40 WPM) [Recommended]", 38),
                    speed_item("Relaxed Student (~30 WPM)", 30),
                    speed_item("Fast Human (~55 WPM)", 55),
                    speed_item("Turbo Paste-Bypass (~150 WPM)", 150),
                ),
            ),
            pystray.MenuItem(
                "Class Mode Injection",
                pystray.Menu(
                    injection_mode_item("Instant Paste (Zero Indent Error) [Default]", "paste"),
                    injection_mode_item("Turbo Keystrokes (1.5ms / bypass)", "turbo_type"),
                ),
            ),
            pystray.MenuItem(
                "Auto-Type on Solve",
                lambda icon, item: self.toggle_auto_type(),
                checked=lambda item: self.auto_type_enabled,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Subject Preset",
                pystray.Menu(
                    subject_item("Java SE / OOP", "java"),
                    subject_item("Advanced Java & JDBC", "adv_java"),
                    subject_item("C Programming", "c"),
                    subject_item("SQL & Databases", "sql"),
                    subject_item("Web Technologies", "web"),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Settings (config.json)", lambda icon, item: self.open_config()),
            pystray.MenuItem("View Logs", lambda icon, item: self.open_log()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit ScreenBot", lambda icon, item: self.stop(icon, item)),
        ]

        self.tray_icon = pystray.Icon(
            "ScreenBot",
            icon_img,
            tooltip_text,
            pystray.Menu(*menu_items),
        )

        def on_tray_ready(icon):
            icon.visible = True
            self.play_sound("startup")
            if self.config.get("auto_scan_enabled", True):
                self.toggle_auto_scan()
            try:
                icon.notify(
                    f"Auto-Scan Live Coding is ACTIVE (Every {scan_interval}s)!\n"
                    "• Auto-syncs newly added lines without repetition\n"
                    "• Ctrl + \\ : Manual Sync Now\n"
                    "• Ctrl + Shift + \\ : Full Re-Sync\n"
                    "• Ctrl + Alt + \\ : Pause/Resume Auto-Scan",
                    "ScreenBot Pro is Active!"
                )
            except Exception:
                pass

        try:
            self.tray_icon.run(setup=on_tray_ready)
        except (KeyboardInterrupt, SystemExit):
            self.stop()


_single_instance_mutex = None


def show_message_box(title: str, message: str, is_info: bool = True):
    try:
        import ctypes
        flags = 0x40 if is_info else 0x30
        flags |= 0x10000
        ctypes.windll.user32.MessageBoxW(0, message, title, flags)
    except Exception:
        pass


def ensure_single_instance():
    """Ensures only a single instance of ScreenBot runs at any time via a Windows Kernel Mutex."""
    global _single_instance_mutex
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        MUTEX_NAME = "Global\\ScreenBot_SingleInstance_Mutex_98234"
        _single_instance_mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            logger.warning("Another instance of ScreenBot is already running! Showing notification.")
            show_message_box(
                "ScreenBot is Already Running",
                "ScreenBot Pro is ALREADY running and active in your Windows System Tray (near the clock)!\n\n"
                "Active Hotkeys:\n"
                "• Ctrl + \\ (or Ctrl + 3) : Sync YouTube / Teams Code -> IDE\n"
                "• Ctrl + 4 : AI Lecture Note-Taker -> Word\n"
                "• Ctrl + 2 : Highlight & Solve (Assignments)\n"
                "• ESC : Emergency Stop Typing\n\n"
                "Look for the blue ScreenBot monitor icon in your system tray (bottom-right near the clock, click the ^ arrow).",
                is_info=True,
            )
            sys.exit(0)
    except Exception as e:
        logger.debug(f"Could not check single instance mutex: {e}")


def main():
    if "--selftest" in sys.argv:
        report = run_selftest(check_api=True, api_key=load_config().get("gemini_api_key", ""))
        print("========================================")
        print("        SCREENBOT SELFTEST REPORT       ")
        print("========================================")
        for k, v in report.items():
            print(f"[{v:4s}] {k}")
            logger.info(f"SELFTEST [{v:4s}] {k}")
        sys.exit(0 if all(v == "PASS" for v in report.values()) else 1)

    ensure_single_instance()
    bot = ScreenBot()
    bot.start()


if __name__ == "__main__":
    main()
