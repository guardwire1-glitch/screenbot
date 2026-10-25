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
import subprocess
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
    capture_stable_frame,
    encode_image_to_bytes,
    compute_frame_diff,
    CaptureBackoffManager,
    is_frame_black_or_blank,
    get_foreground_window,
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
from doc_merge import (
    DocMerger, Registry as DocRegistry, FrameLine as DocFrameLine,
    Meta as DocMeta, Cfg as DocCfg, plan_injection, apply_plan,
)
from ast_healer import HyperAwareCodeEngine
from smart_context_engine import SmartCodeSynchronizer, heal_and_format
from smart_sync_engine import ScreenBotSmartController, SyncResult, wait_for_physical_keys_released
from injector import SafeInjector, WindowFocusContext
from failsafes import CircuitBreaker, ScanWatchdog, compute_build_hash
from session_reconciler import SessionReconciler, FlaggedItem
from harness import run_selftest
from proposal_engine import MasterCodeTracker, validate_code_to_inject
from editor_bridge import BridgeServer, PendingChange, BridgeError
from editor_adapters import EditorRouter, EditorContext, ApplyResult, sha256_text
from syntax_gate import gate as syntax_gate, check as syntax_check

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
    3. %APPDATA%\\ScreenBot\\config.json (per-user store)
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
        "hotkey_note_taker": "ctrl+4",
        "lecture_injection_mode": "paste",
        "stability_interval": 0.35,
        "stability_threshold": 2.0,
        "lecture_pixel_diff_threshold": 2.0,
        "max_frame_age_seconds": 12,
        "quota_cooldown_seconds": 90,
        "format_on_apply": False,
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


# Virtual-key codes for every modifier Windows can leave logically down after
# a suppressed global hotkey. CCtrl/Shift/Alt include generic, left and right.
_MODIFIER_VKS = (0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)
_KEYEVENTF_KEYUP = 0x0002


def release_all_modifiers(reason: str = "") -> None:
    """Force key-up events for all modifiers. Must never raise.

    A suppressed hotkey can leave Ctrl/Shift/Alt logically down when a handler
    is interrupted. That turns every later click into Ctrl+click and every drag
    into a copy. This is the last line of defence and is called in finally blocks.
    """
    try:
        for vk in _MODIFIER_VKS:
            try:
                user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
        if reason:
            logger.debug(f"Released modifier keys ({reason}).")
    except Exception:
        pass


def release_keys(*vks: int) -> None:
    """Release specific shortcut keys (e.g. Ctrl/V) without touching other keys."""
    try:
        for vk in vks:
            try:
                user32.keybd_event(int(vk), 0, _KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
    except Exception:
        pass



def clean_code_line(line: str) -> str:
    """Strips OCR line numbers (e.g. '1 | ', '12: ', '3. ') and trailing whitespace."""
    l = re.sub(r"^\s*\d+[\s\|\.:]+", "", line)
    return l.rstrip()


def normalize_for_diff(line: str) -> str:
    """Normalizes a line for comparison only.

    Whitespace, smart quotes and quote style must not create a fake "new" line:
    re-typing an unchanged statement is the exact duplicate-typing bug this
    comparison exists to prevent. The raw text is never rewritten.
    """
    cleaned = clean_code_line(line)
    cleaned = cleaned.translate(str.maketrans({
        "“": '"', "”": '"', "‘": "'", "’": "'", "\u00a0": " ", "\u200b": "",
    }))
    cleaned = cleaned.replace("'", '"')
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*([(){}\[\];,.:])\s*", r"\1", cleaned)
    return cleaned


def compute_code_delta(master_code: str, new_code: str, with_parts: bool = False):
    """Closed-loop, scroll-immune delta computation.

    A frame is a *viewport*, never the whole file. Lines that scrolled off the
    top or bottom must survive; new lines are spliced in at the position where
    the frame's known lines anchor them. The only case that replaces the whole
    master is a frame that starts at the top of the file.

    Returns (delta, updated_master) or, with_parts=True,
    (delta, updated_master, prefix, tail, suffix) where
    prefix + tail + suffix == updated_master.splitlines().
    """
    def pack(delta: str, updated: str):
        if not with_parts:
            return delta, updated
        lines = updated.splitlines()
        delta_norm = [normalize_for_diff(x) for x in delta.splitlines() if x.strip()]
        if not delta_norm:
            return delta, updated, lines, [], []
        line_norm = [normalize_for_diff(x) for x in lines if x.strip()]
        positions, cursor = [], 0
        for target_line in delta_norm:
            found = -1
            for idx in range(cursor, len(line_norm)):
                if line_norm[idx] == target_line:
                    found = idx
                    break
            if found < 0:
                return delta, updated, [], lines, []
            positions.append(found)
            cursor = found + 1
        content_rows = [i for i, x in enumerate(lines) if x.strip()]
        first = content_rows[positions[0]]
        last = content_rows[positions[-1]]
        return delta, updated, lines[:first], lines[first:last + 1], lines[last + 1:]

    if not new_code or not new_code.strip():
        return pack("", master_code or "")

    new_raw_lines = [clean_code_line(l) for l in new_code.splitlines()]
    while new_raw_lines and not new_raw_lines[0].strip():
        new_raw_lines.pop(0)
    while new_raw_lines and not new_raw_lines[-1].strip():
        new_raw_lines.pop()
    if not new_raw_lines:
        return pack("", master_code or "")

    cleaned_new_code = "\n".join(new_raw_lines)
    if not master_code or not master_code.strip():
        return pack(cleaned_new_code, cleaned_new_code)

    master_raw_lines = [clean_code_line(l) for l in master_code.splitlines()]
    while master_raw_lines and not master_raw_lines[0].strip():
        master_raw_lines.pop(0)
    while master_raw_lines and not master_raw_lines[-1].strip():
        master_raw_lines.pop()

    m_norm = [normalize_for_diff(l) for l in master_raw_lines if l.strip()]
    n_norm = [normalize_for_diff(l) for l in new_raw_lines if l.strip()]
    if not n_norm:
        return pack("", master_code)
    if m_norm == n_norm:
        return pack("", master_code)

    # Exact tail/subset: lecturer paused or the viewport moved slightly.
    if len(m_norm) >= len(n_norm) and m_norm[-len(n_norm):] == n_norm:
        return pack("", master_code)

    matcher = difflib.SequenceMatcher(None, m_norm, n_norm, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size]
    if not blocks:
        return pack(cleaned_new_code, cleaned_new_code)

    first = blocks[0]
    starts_at_top = first.a == 0 and first.b == 0
    if starts_at_top:
        delta_lines = []
        for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
            if tag in ("insert", "replace"):
                delta_lines.extend(new_raw_lines[j1:j2])
        return pack("\n".join(delta_lines), cleaned_new_code)

    # A low-similarity frame that does not start at the top is a different file
    # (tab switch), not a scroll position.
    if first.b > 0 and matcher.ratio() < 0.25 and len(m_norm) >= 4 and len(n_norm) >= 4:
        return pack(cleaned_new_code, cleaned_new_code)

    # Map normalized content indices back to raw rows so blank lines survive.
    master_row = [i for i, l in enumerate(master_raw_lines) if l.strip()]
    new_row = [i for i, l in enumerate(new_raw_lines) if l.strip()]

    inserts: Dict[int, list] = {}
    replaces: Dict[int, tuple] = {}
    delta_lines = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            pos = master_row[i1] if i1 < len(master_row) else len(master_raw_lines)
            lines = [new_raw_lines[k] for k in new_row[j1:j2]]
            inserts.setdefault(pos, []).extend(lines)
            delta_lines.extend(lines)
        elif tag == "replace":
            if i2 <= i1:
                continue
            start = master_row[i1]
            end = master_row[i2 - 1] + 1
            lines = [new_raw_lines[k] for k in new_row[j1:j2]]
            replaces[start] = (end, lines)
            delta_lines.extend(lines)
        # "delete" is deliberately ignored: an absent line may simply have
        # scrolled out of view or be hidden by a tooltip.

    out, idx = [], 0
    while idx < len(master_raw_lines):
        if idx in inserts:
            out.extend(inserts[idx])
        if idx in replaces:
            end, lines = replaces[idx]
            out.extend(lines)
            idx = end
            continue
        out.append(master_raw_lines[idx])
        idx += 1
    if len(master_raw_lines) in inserts:
        out.extend(inserts[len(master_raw_lines)])

    updated = "\n".join(out)
    return pack("\n".join(delta_lines), updated)


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
        # ShadowMaster is an audit/snapshot store, never the delivery mechanism.
        # Pointing it at the user's real file would bypass every gate below.
        shadow_path = SCRIPT_DIR / "session_data" / "shadow_master_code.txt"
        self.shadow_master = ShadowMaster(file_path=shadow_path, log_dir=SCRIPT_DIR / "session_data", max_snapshots=50)
        self.frame_archiver = FrameArchiver(session_dir=SCRIPT_DIR / "session_frames")
        self.stability_checker = FrameStabilityChecker(stability_threshold=0.02)
        self.occlusion_detector = OcclusionDetector()
        self.consensus_tracker = LineConsensusTracker(required_observations=2, min_confidence=0.85)
        self.merge_engine = MergeEngine()
        # Consensus already requires two frames, so the document merger may apply
        # an insertion on its first accepted frame. Destructive edits stay gated.
        self.doc_registry = DocRegistry(cfg=DocCfg(
            req_insert=1, req_modify=2, req_delete=2, req_rewrite=2,
        ))
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

        # Live-path state. The tracker is the source of truth for undo.
        self.master_tracker = MasterCodeTracker("")
        self._ai_cooldown_until = 0.0
        self._manual_sync_queued = False
        self._manual_sync_waiting = False
        self._stopping = False
        self._consensus_file = ""
        self._last_pending_count = 0

        # IDE-agnostic editor bridge. The socket is started by main(), not by
        # the constructor, so unit tests and one-off imports never bind a port.
        self.bridge = BridgeServer(
            pending_path=SCRIPT_DIR / "session_data" / "pending_changes.json",
            logger=logger,
        )
        self.editor_router = EditorRouter(
            self.bridge,
            resolver=self._explicit_target_path,
            logger=logger,
            format_on_apply=bool(self.config.get("format_on_apply", False)),
        )
        self.bridge.pending_resolver = self.editor_router.resolve_pending
        self._bridge_started = False

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

    # ------------------------------------------------------------------
    # Focus, clipboard and modifier guards
    # ------------------------------------------------------------------
    def _notify_guard(self, message: str) -> None:
        """Best-effort tray notification for a safety-guard abort."""
        if self.tray_icon:
            try:
                self.tray_icon.notify(message, "ScreenBot Guard")
            except Exception:
                pass

    def _foreground_hwnd(self) -> Optional[int]:
        try:
            return int(user32.GetForegroundWindow() or 0) or None
        except Exception:
            return None

    def _caret_target_safe(self, target_hwnd: Optional[int], action: str = "type") -> bool:
        """Refuses synthetic keystrokes when focus moved or landed on chat/video."""
        if not target_hwnd:
            return True
        try:
            current = int(user32.GetForegroundWindow() or 0)
        except Exception:
            return True
        if current != int(target_hwnd):
            logger.warning(
                f"Focus moved before {action} (expected {target_hwnd}, got {current}). "
                "Aborting to prevent mis-typing."
            )
            self._notify_guard("Target window lost focus. Aborted to prevent mis-typing.")
            self.play_sound("aborted")
            return False
        if is_browser_or_meeting_window(current):
            logger.warning(
                f"Refusing to {action} into a browser/meeting window (hwnd={current})."
            )
            self._notify_guard("Refused to type into a browser/meeting window.")
            self.play_sound("aborted")
            return False
        return True

    def _guarded_hotkey(self, handler):
        """Wrap a hotkey so a swallowed modifier key-up cannot stick Ctrl/Shift."""
        def _wrapped(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            finally:
                timer = threading.Timer(0.35, release_all_modifiers)
                timer.daemon = True
                timer.start()
        return _wrapped

    def _modifier_watchdog(self):
        """Force-releases a modifier held implausibly long (>25s)."""
        watched = {
            "Ctrl": (0x11,),
            "Shift": (0x10,),
            "Alt": (0x12,),
            "Win": (0x5B, 0x5C),
        }
        held_since: Dict[str, float] = {}
        while not self._stopping:
            time.sleep(1.0)
            if self._stopping:
                break
            now = time.time()
            try:
                states = {
                    name: any(bool(user32.GetAsyncKeyState(vk) & 0x8000) for vk in vks)
                    for name, vks in watched.items()
                }
            except Exception:
                continue
            for name, is_down in states.items():
                if is_down:
                    held_since.setdefault(name, now)
                else:
                    held_since.pop(name, None)
            stuck = [name for name, started in held_since.items() if now - started > 25.0]
            if stuck:
                logger.warning(
                    "Modifier watchdog: %s held for >25s with no key-up. Force-releasing.",
                    "/".join(stuck),
                )
                release_all_modifiers()
                held_since.clear()

    def release_stuck_keys(self, notify: bool = False):
        """Manual escape hatch for a stuck modifier state."""
        release_all_modifiers(reason="manual/tray request")
        if notify:
            self._notify_guard("Released Ctrl/Shift/Alt/Win. Normal clicks should work again.")

    def show_running_badge(self):
        """Show the 'ScreenBot is running' badge in a separate silent process."""
        try:
            auto_flag = "1" if self.auto_scan_enabled else "0"
            parent_pid = str(os.getpid())
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--badge", parent_pid, auto_flag, self.current_subject]
            else:
                cmd = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--badge",
                    parent_pid,
                    auto_flag,
                    self.current_subject,
                ]
            creationflags = 0x08000000 if os.name == "nt" else 0
            subprocess.Popen(cmd, creationflags=creationflags, close_fds=True)
        except Exception as e:
            logger.debug(f"Could not launch running badge: {e}")

    # ------------------------------------------------------------------
    # Master snapshots / undo / 2-read bookkeeping
    # ------------------------------------------------------------------
    def _commit_master_code(self, new_master: str) -> None:
        """Advance the undo tracker only after an edit actually reached the editor."""
        if not new_master or not new_master.strip():
            return
        previous = self.master_tracker.master_code
        if previous and previous != new_master:
            self.master_tracker.save_snapshot()
        self.master_tracker.master_code = new_master
        self.last_code_shadow = new_master

    def _decay_candidate_reads(self, current_lines) -> None:
        """Drop 2-read counters for candidate lines no longer on screen."""
        from proposal_engine import normalize_code_line
        keep = {
            normalize_code_line(getattr(line, "text", line))
            for line in current_lines
        }
        for key in list(self.master_tracker.read_counts):
            if key not in keep:
                del self.master_tracker.read_counts[key]

    def trigger_undo(self, quiet: bool = False) -> bool:
        """Ctrl+Alt+Z: restore the previous master and roll the target file back.

        Refuses to touch the editor when its content no longer matches the code
        ScreenBot last delivered. Undoing through a user's newer edits would be
        data loss, not an undo.
        """
        if not self.master_tracker.snapshots:
            if not quiet:
                self.play_sound("aborted")
                self._notify_guard("No earlier snapshots available to undo.")
            return False

        restored = self.master_tracker.snapshots[-1]
        current_master = self.last_code_shadow

        target = self._explicit_target_path() if self.direct_file_sync else None
        ctx = None
        if target is not None and target.exists():
            ctx = self._editor_context_for(target)
            if ctx is None:
                logger.warning("Undo aborted: cannot read the bound editor/file.")
                if not quiet:
                    self.play_sound("aborted")
                    self._notify_guard("Undo aborted: the bound file is not readable.")
                return False
            live_text = (ctx.text or "").replace("\r\n", "\n").strip()
            expected_text = (current_master or "").replace("\r\n", "\n").strip()
            if live_text != expected_text:
                logger.warning(
                    "Undo refused: the editor changed after ScreenBot's last sync."
                )
                if not quiet:
                    self.play_sound("aborted")
                    self._notify_guard(
                        "Undo refused: the file changed after the last sync. "
                        "Your newer edits were left untouched."
                    )
                return False

        if target is not None and target.exists() and ctx is not None:
            result = self.editor_router.apply(ctx, restored)
            if not result.ok:
                logger.warning(f"Undo apply failed ({result.error}); snapshot kept.")
                if not quiet:
                    self.play_sound("aborted")
                    self._notify_guard(f"Undo failed: {result.error or 'editor rejected the edit'}")
                return False

        self.master_tracker.undo()
        self.last_code_shadow = restored
        self.last_injection_ended_with_newline = False
        self.master_tracker.read_counts.clear()
        self.consensus_tracker.reset()

        if target is not None and target.exists():
            logger.info(f"Undo rolled back {target} ({len(restored)} chars).")
        else:
            logger.info("Undo restored the internal master snapshot (no file was bound).")

        self.play_sound("done")
        if self.tray_icon:
            try:
                self.tray_icon.notify(
                    "Restored the previous ScreenBot snapshot.",
                    "ScreenBot Undo (Ctrl+Alt+Z)",
                )
            except Exception:
                pass
        return True

    # ------------------------------------------------------------------
    # Editor delivery
    # ------------------------------------------------------------------
    def _default_filename(self) -> str:
        ext = {
            "csharp": ".cs", "cs": ".cs", "java": ".java", "python": ".py",
            "py": ".py", "sql": ".sql", "web": ".html", "javascript": ".js",
            "typescript": ".ts", "cpp": ".cpp", "c": ".c",
        }.get(str(self.current_subject).lower(), ".cs")
        return f"lecture{ext}"

    def _explicit_target_path(self) -> Optional[Path]:
        """The user-bound file only. Auto-discovery is never allowed to pick a
        write target: a same-named file in another folder must not be edited."""
        if not self.target_file_path:
            return None
        try:
            path = Path(self.target_file_path)
            return path if path.is_file() else None
        except Exception:
            return None

    def _bound_target_path(self) -> Optional[Path]:
        return self._explicit_target_path() or self.get_or_detect_target_file()

    def _editor_context_for(self, target: Optional[Path]) -> Optional[EditorContext]:
        path = str(target) if target else None
        try:
            return self.editor_router.get_context(file_path=path)
        except TypeError:
            return self.editor_router.get_context()

    def _start_bridge(self) -> None:
        if self._bridge_started:
            return
        try:
            self.bridge.start()
            self._bridge_started = True
        except Exception as e:
            logger.warning(f"Editor bridge unavailable: {e}")

    def _stop_bridge(self) -> None:
        if not self._bridge_started:
            return
        try:
            self.bridge.stop()
        except Exception:
            pass
        self._bridge_started = False

    @staticmethod
    def _diagnostic_error_count(diagnostics, file_path: str = "") -> int:
        count = 0
        target_name = Path(file_path).name.lower() if file_path else ""
        for item in diagnostics or []:
            try:
                if str(item.get("severity", "")).lower() != "error":
                    continue
                diag_file = str(item.get("file") or "")
                if diag_file and target_name and Path(diag_file).name.lower() != target_name:
                    continue
                count += 1
            except Exception:
                continue
        return count

    def _queue_pending(self, context: EditorContext, proposed: str, reason: str,
                       diagnostics=None) -> Optional[PendingChange]:
        try:
            change = self.editor_router.queue_pending(context, proposed, reason, diagnostics)
            logger.warning(f"Change held for review: {reason} ({context.file_path}).")
            self._notify_guard(f"Change held for review: {reason}")
            return change
        except Exception as e:
            logger.error(f"Could not queue pending change: {e}")
            return None

    @staticmethod
    def _missing_delivered_lines(previous_master: str, current_text: str) -> List[str]:
        """Lines ScreenBot delivered that the buffer no longer contains.

        Re-adding them would fight a deliberate user deletion, so the caller
        holds the change for review instead.
        """
        current = {
            normalize_for_diff(line)
            for line in (current_text or "").replace("\r\n", "\n").splitlines()
        }
        current.discard("")
        missing: List[str] = []
        for line in (previous_master or "").replace("\r\n", "\n").splitlines():
            key = normalize_for_diff(line)
            if key and key not in current and key not in missing:
                missing.append(key)
        return missing

    def _apply_master_direct(self, target: Path, master_text: str, delta_text: str = "") -> bool:
        """Apply lecture knowledge to a real editor without clobbering user code."""
        ctx = self._editor_context_for(target)
        if ctx is None:
            logger.error(f"Direct File Sync: cannot read editor/file context for {target}.")
            self._notify_guard("Direct sync: target file is not readable. Nothing was typed.")
            return False
        if not ctx.can_apply or (ctx.dirty and ctx.source == "disk"):
            self._queue_pending(
                ctx, apply_plan(ctx.text, plan_injection(master_text, ctx.text)[0]),
                "editor buffer has unsaved changes", ctx.diagnostics,
            )
            return False

        missing = self._missing_delivered_lines(self.last_code_shadow, ctx.text)
        if missing:
            self._queue_pending(
                ctx, apply_plan(ctx.text, plan_injection(master_text, ctx.text)[0]),
                f"user removed {len(missing)} line(s) ScreenBot previously synced "
                f"(first: {missing[0][:80]})",
                ctx.diagnostics,
            )
            return False

        steps, conflicts = plan_injection(master_text, ctx.text)
        proposed = apply_plan(ctx.text, steps)
        if proposed == ctx.text:
            if conflicts:
                self._queue_pending(
                    ctx, master_text,
                    f"user code differs from the lecture in {len(conflicts)} place(s)",
                    ctx.diagnostics,
                )
                return False
            logger.info("Target already contains every lecture line; no write needed.")
            return True

        # Validate the full document that will reach the editor, not only the
        # delta: a closing brace alone is a valid live-coding insertion.
        guard_text = master_text.strip() or delta_text
        valid, reason = validate_code_to_inject(guard_text, self.current_subject)
        if not valid:
            logger.warning(f"Injection guard rejected direct-sync payload: {reason}")
            self._queue_pending(ctx, proposed, f"injection guard: {reason}", ctx.diagnostics)
            return False

        syntax_ok, syntax_reason, _status = syntax_gate(
            ctx.text, proposed,
            language=ctx.language or self.current_subject,
            filename=ctx.file_path,
        )
        if not syntax_ok:
            logger.warning(f"Syntax gate held direct-sync change: {syntax_reason}")
            self.session_reconciler.record_flagged_item(FlaggedItem(
                frame_id=f"direct_{int(time.time() * 1000)}",
                item_type="syntax_error",
                reason=syntax_reason,
                line_text=(master_text.splitlines()[0] if master_text.splitlines() else "")[:120],
            ))
            self._queue_pending(ctx, proposed, f"syntax gate: {syntax_reason}", ctx.diagnostics)
            return False

        result = self.editor_router.apply(ctx, proposed)
        if not result.ok:
            reason_text = result.error or ("file changed" if result.conflict else "editor rejected edit")
            self._queue_pending(ctx, proposed, f"apply failed: {reason_text}", result.diagnostics)
            return False

        before_errors = self._diagnostic_error_count(ctx.diagnostics, ctx.file_path)
        after_errors = self._diagnostic_error_count(result.diagnostics, ctx.file_path)
        if after_errors > before_errors:
            rollback = self.editor_router.restore(ctx, ctx.text, result.hash)
            logger.error(
                f"Diagnostics worsened ({before_errors} -> {after_errors}); "
                f"rollback {'succeeded' if rollback.ok else 'FAILED'}."
            )
            self._queue_pending(
                ctx, proposed,
                f"diagnostics worsened from {before_errors} to {after_errors}",
                result.diagnostics,
            )
            return False

        if conflicts:
            follow_steps, follow_conflicts = plan_injection(master_text, result.text)
            if follow_conflicts:
                self._queue_pending(
                    ctx, apply_plan(result.text, follow_steps),
                    f"user code differs in {len(follow_conflicts)} place(s)",
                    result.diagnostics,
                )

        logger.info(
            f"Direct File Sync applied through {result.source} -> {target} "
            f"({len(proposed.splitlines())} lines, diagnostics {before_errors}->{after_errors})."
        )
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

        release_all_modifiers("")
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

    def instant_paste(self, text: str, target_hwnd: Optional[int] = None) -> bool:
        """Paste via clipboard + Ctrl+V, then put the user's clipboard back.

        Refuses to send synthetic keys when focus moved or landed on a browser or
        meeting window. Modifier key-ups are sent in a finally block, so an
        interrupted paste can never leave Ctrl or V logically down.
        """
        if not text:
            return False
        if not self._caret_target_safe(target_hwnd, "paste"):
            return False

        VK_CONTROL, VK_V, KEYUP = 0x11, 0x56, 0x0002
        original_clip: Optional[str] = None
        pasted = False
        try:
            self.wait_for_modifiers_release()
            try:
                original_clip = pyperclip.paste()
            except Exception:
                original_clip = None

            pyperclip.copy(text)
            time.sleep(0.04)
            try:
                if pyperclip.paste() != text:
                    raise RuntimeError("clipboard did not accept the payload")
            except RuntimeError:
                raise
            except Exception:
                # Some clipboard backends cannot read back; absence of proof is
                # not proof of failure.
                pass

            if not self._caret_target_safe(target_hwnd, "paste"):
                return False

            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_V, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_V, 0, KEYUP, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
            pasted = True
            logger.info(f"Instant paste injected ({len(text)} chars) into hwnd={target_hwnd or 'foreground'}.")
            return True
        except Exception as e:
            logger.error(f"Instant paste failed ({e}); falling back to guarded typing.")
            self.human_type(text, is_turbo=True, target_hwnd=target_hwnd)
            return False
        finally:
            try:
                user32.keybd_event(VK_V, 0, KEYUP, 0)
                user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
            except Exception:
                pass
            if pasted:
                # Give the target a moment to consume the clipboard before the
                # user's own clipboard contents are restored.
                time.sleep(0.12)
            if original_clip is not None:
                try:
                    pyperclip.copy(original_clip)
                except Exception:
                    pass

    def select_all_and_paste(self, text: str, target_hwnd: Optional[int] = None) -> bool:
        """Ctrl+A then Ctrl+V. Only used for an explicit full-buffer mirror.

        A failed clipboard write aborts instead of falling back to typing:
        typing at the caret after a failed select-all would append partial code
        to the wrong place.
        """
        if not text:
            return False
        if not self._caret_target_safe(target_hwnd, "paste"):
            return False

        VK_CONTROL, VK_A, VK_V, KEYUP = 0x11, 0x41, 0x56, 0x0002
        original_clip: Optional[str] = None
        pasted = False
        try:
            self.wait_for_modifiers_release()
            try:
                original_clip = pyperclip.paste()
            except Exception:
                original_clip = None
            pyperclip.copy(text)
            time.sleep(0.04)
            if not self._caret_target_safe(target_hwnd, "paste"):
                return False

            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_A, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_A, 0, KEYUP, 0)
            time.sleep(0.02)
            user32.keybd_event(VK_V, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_V, 0, KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
            pasted = True
            logger.info(f"Mirror paste replaced the editor buffer ({len(text)} chars).")
            return True
        except Exception as e:
            logger.error(f"select_all_and_paste aborted without typing: {e}")
            return False
        finally:
            try:
                user32.keybd_event(VK_A, 0, KEYUP, 0)
                user32.keybd_event(VK_V, 0, KEYUP, 0)
                user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
            except Exception:
                pass
            if pasted:
                time.sleep(0.12)
            if original_clip is not None:
                try:
                    pyperclip.copy(original_clip)
                except Exception:
                    pass

    def human_type(self, text: str, is_turbo: bool = False, target_hwnd: Optional[int] = None):
        """Simulates realistic student typing (dynamic ~35-40 WPM) or turbo speed,
        with micro-pauses and instant ESC cancel. When a target HWND is supplied,
        typing stops as soon as focus leaves that window."""
        if not text:
            return False

        with self.lock:
            if self.is_typing:
                logger.info("Already typing text, ignoring duplicate call.")
                return False
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
            if target_hwnd and not self._caret_target_safe(target_hwnd, "type"):
                return False

            clean_text = text.replace("\r\n", "\n").replace("\r", "\n")

            if is_turbo:
                self.play_sound("typing")
                logger.info(f"Starting Turbo typing injection ({len(clean_text)} chars)... Press ESC to stop immediately.")
                for i, char in enumerate(clean_text):
                    if self.stop_typing_flag or (user32 and (user32.GetAsyncKeyState(0x1B) & 0x8000)):
                        self.stop_typing_flag = True
                        logger.info("Turbo typing cancelled immediately by ESC key.")
                        self.play_sound("aborted")
                        break
                    if target_hwnd and i % 60 == 0 and not self._caret_target_safe(target_hwnd, "type"):
                        self.stop_typing_flag = True
                        break
                    self.pynput_ctrl.type(char)
                    time.sleep(0.0015)
                if not self.stop_typing_flag:
                    logger.info("Turbo typing finished successfully.")
                return not self.stop_typing_flag

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
                if target_hwnd and i % 40 == 0 and not self._caret_target_safe(target_hwnd, "type"):
                    self.stop_typing_flag = True
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
            return not self.stop_typing_flag

        except Exception as e:
            logger.error(f"Error during human typing: {e}")
            return False
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
        """Ctrl+Alt+Z: restore the previous master and roll the target file back."""
        if self.trigger_undo(quiet=True):
            return True

        # Legacy/audit fallback (also keeps direct ShadowMaster users working).
        if self.shadow_master.undo():
            self.last_code_shadow = self.shadow_master.get_current_code()
            self.play_sound("done")
            logger.info("Undo: restored the previous shadow-master snapshot.")
            self._notify_guard("Restored the previous code snapshot.")
            return True

        self.play_sound("aborted")
        self._notify_guard("No earlier snapshots available to undo.")
        return False

    def trigger_ctrl_shift_f(self):
        """Ctrl+Shift+F: bind the active editor file and start direct sync.

        If a scan is already running the request is queued instead of ignored;
        the watcher starts exactly one sync when the current cycle goes idle.
        """
        if not self.check_and_set_busy("Bind Editor File"):
            self._manual_sync_queued = True
            if not self._manual_sync_waiting:
                self._manual_sync_waiting = True
                threading.Thread(target=self._run_queued_manual_sync, daemon=True).start()
            logger.info("Manual bind/sync queued until the current scan finishes.")
            return
        threading.Thread(target=self._bind_and_sync_worker, daemon=True).start()

    def _run_queued_manual_sync(self):
        try:
            deadline = time.time() + 180.0
            while not self._stopping and time.time() < deadline:
                while self.is_busy and not self._stopping and time.time() < deadline:
                    time.sleep(0.25)
                if self._stopping or not self._manual_sync_queued:
                    return
                self._manual_sync_queued = False
                self.trigger_ctrl_shift_f()
                if not self._manual_sync_queued:
                    return
                # trigger_ctrl_shift_f re-queued this request (for example the
                # hotkey debounce rejected it). Keep watching instead of
                # dropping the user's bind request.
                time.sleep(0.25)
        finally:
            self._manual_sync_waiting = False

    _EDITOR_IDES = {
        "vscode", "antigravity", "visualstudio", "ssms",
        "jetbrains", "eclipse", "netbeans", "editor",
    }

    def _active_editor_file(self) -> Optional[Path]:
        """Resolve the file the user is actually looking at.

        VS Code family and Visual Studio answer with their real active buffer.
        Disk-based IDEs expose only a filename in their title, so they cannot
        be bound by guessing: they use an explicit target_file_path instead.
        Binding a file while a browser or meeting window is focused is refused
        by the caller.
        """
        _hwnd, _title, _exe, ide = self.editor_router.active_window()
        if ide in {"vscode", "antigravity"}:
            ctx = self.editor_router.extension.get_context(ide, file_path=None)
            if ctx and ctx.file_path:
                path = Path(ctx.file_path)
                if path.is_file():
                    return path
        if ide == "visualstudio":
            ctx = self.editor_router.visual_studio.get_context(file_path=None)
            if ctx and ctx.file_path:
                path = Path(ctx.file_path)
                if path.is_file():
                    return path
        if ide in self._EDITOR_IDES:
            # Guessing a path from a filename can bind a same-named file in
            # another folder, which is the overwrite this design must never
            # allow. Disk-based IDEs must use target_file_path.
            logger.warning(
                f"{ide} does not expose an exact active-file path; refusing to "
                "bind by filename. Set target_file_path for this IDE."
            )
        return None

    def _bind_and_sync_worker(self):
        try:
            target = self._active_editor_file()
            if target is None:
                logger.warning("Bind aborted: no exact editor file could be resolved.")
                self._notify_guard(
                    "Open the file in VS Code/Antigravity or Visual Studio, or set "
                    "target_file_path for other IDEs, then press Ctrl+Shift+F. "
                    "Nothing was bound or typed."
                )
                self.play_sound("aborted")
                return
            self.target_file_path = str(target)
            self.direct_file_sync = True
            self.config["direct_file_sync"] = True
            logger.info(f"Direct File Sync bound to: {target}")
            self.play_sound("done")
            self.update_tray_tooltip(f"ScreenBot: Bound {target.name}")
            self._notify_guard(f"Bound to {target.name}. Syncing lecture code now...")
            self.process_code_shadow(force_full=False)
        except Exception as e:
            logger.error(f"Bind/sync failed: {e}", exc_info=True)
            self.play_sound("error")
        finally:
            with self.lock:
                self.is_busy = False

    def bind_active_editor_file(self):
        """Backwards-compatible menu/hotkey entry point."""
        self.trigger_ctrl_shift_f()

    def _log_shadow_event(self, frame_id: str, event_type: str, merged_code: str,
                          pending=None) -> None:
        """Append the audit event only after the edit actually reached the editor."""
        try:
            self.shadow_master.apply_change(MasterChangeEvent(
                frame_id=frame_id,
                event_type=event_type or "append",
                gates_passed=["stability_gate", "consensus_gate"],
                result_code=merged_code,
                metadata={"pending_lines": len(pending or [])},
            ))
        except Exception as e:
            logger.warning(f"Could not append shadow audit event: {e}")

    def process_code_shadow(self, is_auto: bool = False, force_full: bool = False):
        """Capture a stable lecture frame, extract strict JSON, require a second
        observation, merge append-only, then deliver through the editor bridge
        (preferred) or a guarded caret paste.

        The master advances only after delivery succeeds; a failed delivery
        leaves the previous master intact so undo and the next scan agree with
        the file that is actually on disk.
        """
        hotkey = self.config.get("hotkey", "ctrl+0")
        frame_id = f"frame_{int(time.time() * 1000)}"
        try:
            t0 = time.time()
            if time.time() < self._ai_cooldown_until:
                remaining = int(self._ai_cooldown_until - time.time())
                logger.info(f"[{frame_id}] AI quota cooldown active ({remaining}s left); skipping.")
                return
            if not is_auto:
                self.play_sound("trigger")
                self.update_tray_tooltip(f"ScreenBot: Capturing Lecture Code ({self.current_subject.upper()})...")
                logger.info(f"[{frame_id}] Live Code Follower triggered (subject={self.current_subject}). Snapping lecture...")

            pil_img, is_stable = capture_stable_frame(
                mode="lecture",
                interval=float(self.config.get("stability_interval", 0.35)),
                threshold=float(self.config.get("stability_threshold", 2.0)),
                max_dim=int(self.config.get("max_image_dimension", 1920)),
            )
            t_cap = time.time()
            if pil_img is None:
                logger.info(f"[{frame_id}] Capture returned no frame; skipping.")
                return
            if not is_stable:
                logger.info(f"[{frame_id}] Frame is moving/animating; deferred without an AI call.")
                return
            if is_frame_black_or_blank(pil_img):
                logger.info(f"[{frame_id}] Frame is black/blank (display locked or not rendered); skipping.")
                return

            # The HWND is bound at snapshot time, not at typing time. That is the
            # only way the focus guard can detect that the user alt-tabbed.
            target_hwnd = self._foreground_hwnd()

            if (is_auto and not force_full and self.last_lecture_frame is not None
                    and not self.consensus_tracker.has_pending):
                p_diff = compute_frame_diff(self.last_lecture_frame, pil_img)
                threshold = float(self.config.get("lecture_pixel_diff_threshold", 2.0))
                if p_diff < threshold:
                    logger.debug(f"[{frame_id}] Static screen (diff={p_diff:.2f}); no AI call.")
                    return

            try:
                self.frame_archiver.archive_frame(pil_img, frame_id=frame_id)
            except Exception as e:
                logger.warning(f"[{frame_id}] Frame archive warning: {e}")

            occlusions = self.occlusion_detector.detect_occlusions(pil_img)
            if occlusions:
                logger.info(f"[{frame_id}] Local overlay detector: {len(occlusions)} region(s).")

            img_bytes = encode_image_to_bytes(
                pil_img, quality=int(self.config.get("image_quality", 85))
            )
            prompt = build_extraction_prompt(language_hint=self.current_subject)
            t_ai_start = time.time()
            raw_resp = self.solver.extract_lecture_code_structured(img_bytes, prompt=prompt)
            logger.info(f"[{frame_id}] Vision extraction took {(time.time() - t_ai_start) * 1000:.1f}ms.")

            if getattr(self.solver, "last_error_kind", "") == "quota":
                cooldown = max(15.0, float(self.config.get("quota_cooldown_seconds", 90)))
                self._ai_cooldown_until = time.time() + cooldown
                logger.warning(f"[{frame_id}] Gemini quota exhausted; pausing AI calls for {int(cooldown)}s.")
                if not is_auto:
                    self.play_sound("error")
                return
            if not raw_resp:
                logger.info(f"[{frame_id}] No extraction result; master untouched.")
                return

            max_age = float(self.config.get("max_frame_age_seconds", 12))
            frame_age = time.time() - t_cap
            if max_age > 0 and frame_age > max_age:
                logger.warning(
                    f"[{frame_id}] Frame is {frame_age:.1f}s old (limit {max_age:.0f}s); "
                    "discarding stale extraction instead of applying outdated code."
                )
                return

            parsed = parse_model_extraction_response(raw_resp)
            if parsed.error:
                logger.warning(f"[{frame_id}] Rejected malformed extraction: {parsed.error}")
                return

            self.last_lecture_frame = pil_img
            if not parsed.lines:
                logger.info(f"[{frame_id}] No code visible (slide/webpage/dialog); nothing to merge.")
                return
            if parsed.has_occlusion:
                logger.info(f"[{frame_id}] OCR reports an overlay covering code; holding this frame.")
                return

            file_key = (parsed.file_name or "").strip().lower()
            if file_key and self._consensus_file and file_key != self._consensus_file:
                logger.info(
                    f"[{frame_id}] Editor tab switched ({self._consensus_file} -> {file_key}); "
                    "resetting consensus."
                )
                self.consensus_tracker.reset()
                self.last_injection_ended_with_newline = False
            if file_key:
                self._consensus_file = file_key

            accepted, pending = self.consensus_tracker.feed_frame_lines(parsed.lines)
            self._decay_candidate_reads(parsed.lines)
            logger.info(
                f"[{frame_id}] Consensus: {len(accepted)} accepted, "
                f"{len(pending)} held for a cleaner frame."
            )
            if not accepted:
                return

            explicit_target: Optional[Path] = None
            if self.target_file_path:
                try:
                    candidate = Path(self.target_file_path)
                    if candidate.is_file():
                        explicit_target = candidate
                except Exception:
                    pass

            if self.direct_file_sync:
                if explicit_target is None:
                    logger.warning(
                        f"[{frame_id}] Direct File Sync is on but no file is bound; "
                        "no keystrokes were sent. Press Ctrl+Shift+F to bind."
                    )
                    self._notify_guard("Direct sync needs a bound file. Nothing was typed.")
                    return
                ctx = self._editor_context_for(explicit_target)
                target_fname = explicit_target.name
            else:
                target = explicit_target
                ctx = self._editor_context_for(target)
                if ctx is not None and ctx.file_path:
                    target_fname = Path(ctx.file_path).name
                elif target is not None:
                    target_fname = target.name
                else:
                    target_fname = parsed.file_name or self._default_filename()

            baseline_known = ctx is not None
            baseline = ctx.text if ctx is not None else ""
            if not baseline_known and explicit_target is not None and explicit_target.exists():
                try:
                    baseline = explicit_target.read_text(encoding="utf-8", errors="replace")
                    baseline_known = True
                except Exception as e:
                    logger.debug(f"[{frame_id}] Could not read target baseline: {e}")
            if not baseline_known:
                baseline = self.master_tracker.master_code

            doc = self.doc_registry.get(target_fname)
            if not doc.lines and baseline.strip():
                doc.seed_baseline(baseline)

            doc_lines = [
                DocFrameLine(
                    text=l.text, line_no=l.line_no, visible=l.visible, confidence=l.confidence
                )
                for l in accepted
            ]
            meta = DocMeta(
                file_name=target_fname,
                quality_ok=True,
                overlay=parsed.has_occlusion,
                flags=tuple(parsed.flags),
                top=None,
                bottom="eof_visible" in parsed.flags,
                allow_destructive=False,
            )
            merge_res = doc.merge(doc_lines, meta=meta)
            logger.info(f"[{frame_id}] DocMerge event={merge_res.event} notes={merge_res.notes}")

            if merge_res.event in ("blocked", "low_quality"):
                logger.warning(f"[{frame_id}] Merge refused ({merge_res.event}); file untouched.")
                return

            merged_code = doc.text
            if not merged_code.strip():
                return

            delivery_baseline = ctx.text if ctx is not None else baseline
            steps, conflicts = plan_injection(merged_code, delivery_baseline)
            delta_lines: list[str] = []
            for step in steps:
                if isinstance(step, tuple) and len(step) == 4 and step[0] == "replace":
                    delta_lines.extend(step[3])
                else:
                    delta_lines.extend(step[1])

            if not delta_lines and not conflicts:
                self._commit_master_code(merged_code)
                logger.info(f"[{frame_id}] Target already contains the merged code; master synchronized.")
                return
            if not delta_lines and conflicts and ctx is not None:
                self._queue_pending(
                    ctx, merged_code,
                    f"user code differs from the lecture in {len(conflicts)} place(s)",
                    ctx.diagnostics,
                )
                return

            delta_text = "\n".join(delta_lines).strip("\n")
            delivered = self.inject_lecture_code(
                delta_text, is_delta=True, full_master=merged_code, target_hwnd=target_hwnd
            )
            if delivered:
                self._log_shadow_event(frame_id, merge_res.event, merged_code, pending)
                logger.info(
                    f"[{frame_id}] Delivered {len(delta_lines)} new line(s) through "
                    f"{'direct file sync' if self.direct_file_sync else 'guarded caret'}. "
                    f"Cycle {time.time() - t0:.2f}s."
                )
            else:
                logger.warning(f"[{frame_id}] Delivery aborted or held; master not advanced.")

        except Exception as e:
            logger.error(f"Error in process_code_shadow: {e}", exc_info=True)
            if not is_auto:
                self.play_sound("error")
        finally:
            self.update_tray_tooltip(f"ScreenBot: Ready [{self.current_subject.upper()}] ({hotkey.upper()})")
            with self.lock:
                self.is_busy = False

    def inject_lecture_code(self, code_text: str, is_delta: bool = True,
                            full_master: str = "", target_hwnd: Optional[int] = None) -> bool:
        """Deliver lecture code to the real editor.

        Direct File Sync writes only through a hash-checked, syntax-gated editor
        adapter and never types into the foreground window. Caret mode pastes
        only when the snapshot window still has focus and is not a browser or
        meeting window. The master advances only after delivery succeeds.
        """
        if not code_text and not full_master:
            return False

        # 1. DIRECT FILE SYNC: hash-checked adapter, zero keystrokes.
        if self.direct_file_sync:
            target = self._explicit_target_path()
            if target is None:
                logger.error("Direct File Sync has no bound target file; nothing was written or typed.")
                self._notify_guard(
                    "Focus your editor and press Ctrl+Shift+F to bind the file first. "
                    "Nothing was typed."
                )
                self.play_sound("aborted")
                return False

            master_text = full_master or code_text
            applied = self._apply_master_direct(target, master_text, code_text)
            if not applied:
                logger.warning(f"Direct sync to {target} was held; master not advanced.")
                return False

            self._commit_master_code(master_text)
            self.play_sound("done")
            self.update_tray_tooltip(f"ScreenBot: Synced to {target.name}")
            self._notify_guard(f"Synced lecture code into {target.name}.")
            logger.info(f"Direct File Sync succeeded -> {target} ({len(master_text)} chars).")
            self.last_injection_ended_with_newline = True
            return True

        # 2. GUARDED CARET INJECTION.
        dedented = textwrap.dedent(code_text).strip()
        if not dedented:
            if full_master:
                self._commit_master_code(full_master)
            return True

        valid, reason = validate_code_to_inject(dedented, self.current_subject)
        if not valid:
            logger.warning(f"Caret injection guard rejected payload: {reason}")
            self._notify_guard(f"Code held back by the injection guard: {reason}")
            self.play_sound("aborted")
            return False

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

        if injection_mode == "turbo_type":
            delivered = self.human_type(payload, is_turbo=True, target_hwnd=target_hwnd)
        else:
            delivered = self.instant_paste(payload, target_hwnd=target_hwnd)

        if not delivered:
            logger.warning("Caret injection aborted (focus/guard/clipboard); master not advanced.")
            return False

        self._commit_master_code(full_master or dedented)
        self.play_sound("done")
        self.last_injection_ended_with_newline = True
        self._notify_guard(f"Pasted new lecture code at the caret ({len(dedented)} chars).")
        return True

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
        """Terminate cleanly. Idempotent: tray click and exit hotkey can race.

        Modifiers are released both before and after unhooking. A forced kill
        (taskkill /F) still cannot run this code; stop_bot.bat therefore also
        sends key-ups via PowerShell before killing the process.
        """
        if self._stopping:
            return
        self._stopping = True
        self.auto_scan_enabled = False

        release_all_modifiers(reason="shutdown")
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
        self._stop_bridge()

        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

        release_all_modifiers()
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

        self._stopping = False

        # Every hotkey that can fire while a modifier is still pressed goes
        # through the guard, which force-releases Ctrl/Shift/Alt shortly after
        # the handler returns. Only Ctrl+Shift+F is suppressed: it would
        # otherwise open the editor's "find in files" dialog.
        keyboard.add_hotkey(hotkey, self._guarded_hotkey(self.trigger_concise))
        if hotkey_explain:
            keyboard.add_hotkey(hotkey_explain, self._guarded_hotkey(self.trigger_explain))
        if hotkey_paraphrase:
            keyboard.add_hotkey(hotkey_paraphrase, self._guarded_hotkey(self.trigger_paraphrase))

        # Register Live Code Follower: Incremental Delta (Ctrl+\ and Ctrl+3)
        keyboard.add_hotkey(hotkey_code_shadow, self._guarded_hotkey(self.trigger_code_shadow))
        if hotkey_code_shadow.lower() != "ctrl+\\":
            try:
                keyboard.add_hotkey("ctrl+\\", self._guarded_hotkey(self.trigger_code_shadow))
            except Exception:
                pass
        if hotkey_code_shadow.lower() != "ctrl+3":
            try:
                keyboard.add_hotkey("ctrl+3", self._guarded_hotkey(self.trigger_code_shadow))
            except Exception:
                pass

        # Force Full Code Re-Sync (Ctrl+Shift+\)
        keyboard.add_hotkey(hotkey_force_sync, self._guarded_hotkey(self.trigger_force_code_shadow))

        # Bind Active File to Direct File Sync (Ctrl+Shift+F)
        keyboard.add_hotkey(
            "ctrl+shift+f",
            self._guarded_hotkey(self.trigger_ctrl_shift_f),
            suppress=True,
        )

        # Toggle Auto-Scan Loop (Ctrl+Alt+\)
        keyboard.add_hotkey(hotkey_auto_scan, self._guarded_hotkey(self.toggle_auto_scan))

        # Lecture Note-Taker (Ctrl+4)
        keyboard.add_hotkey(hotkey_note_taker, self._guarded_hotkey(self.trigger_note_taker))

        # Register Highlight-and-Solve / Auto-Typer hotkeys (Primary: Ctrl+2)
        keyboard.add_hotkey("ctrl+2", self._guarded_hotkey(self.handle_type_or_solve))
        if hotkey_type and hotkey_type.lower() != "ctrl+2":
            try:
                keyboard.add_hotkey(hotkey_type.lower(), self._guarded_hotkey(self.handle_type_or_solve))
            except Exception:
                pass
        keyboard.add_hotkey("ctrl+shift+v", self._guarded_hotkey(self.handle_type_or_solve))
        keyboard.add_hotkey("ctrl+alt+v", self._guarded_hotkey(self.handle_type_or_solve))
        keyboard.add_hotkey("ctrl+7", self._guarded_hotkey(self.handle_type_or_solve))

        # Kill switch (Ctrl+Alt+Esc) and undo (Ctrl+Alt+Z). These are never
        # suppressed: suppressing an OS-level kill shortcut can strand modifiers.
        keyboard.add_hotkey("ctrl+alt+esc", lambda: self.stop())
        keyboard.add_hotkey("ctrl+alt+z", self._guarded_hotkey(self.undo_last_edit))
        keyboard.add_hotkey("esc", self.cancel_typing)
        keyboard.add_hotkey("ctrl+shift+x", self.cancel_typing)
        keyboard.add_hotkey("ctrl+shift+q", lambda: self.stop())

        threading.Thread(target=self._modifier_watchdog, daemon=True).start()

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


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def run_running_badge(parent_pid: int, auto_flag: str, subject: str):
    """Separate silent process that only shows the running badge.

    It never registers hotkeys, never touches the clipboard, and exits when
    the parent process disappears, so it cannot become an orphan.
    """
    try:
        icon = pystray.Icon(
            "ScreenBotBadge",
            get_or_create_icon_image(),
            f"ScreenBot running | auto-scan {'ON' if auto_flag == '1' else 'OFF'} | {subject.upper()}",
            pystray.Menu(pystray.MenuItem("ScreenBot is active", None, enabled=False)),
        )

        def watch_parent():
            while True:
                time.sleep(3.0)
                if not _pid_alive(parent_pid):
                    try:
                        icon.stop()
                    except Exception:
                        pass
                    return

        threading.Thread(target=watch_parent, daemon=True).start()
        icon.run()
    except Exception as e:
        logger.debug(f"Running badge exited: {e}")


def main():
    if "--badge" in sys.argv:
        idx = sys.argv.index("--badge")
        try:
            parent_pid = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            parent_pid = 0
        auto_flag = sys.argv[idx + 2] if len(sys.argv) > idx + 2 else "0"
        subject = sys.argv[idx + 3] if len(sys.argv) > idx + 3 else "auto"
        run_running_badge(parent_pid, auto_flag, subject)
        return

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
    bot._start_bridge()
    bot.show_running_badge()
    bot.start()


if __name__ == "__main__":
    main()
