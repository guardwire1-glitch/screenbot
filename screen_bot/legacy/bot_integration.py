"""
bot_integration.py
Drop-in wiring for bot.py integrating:
- Single-instance Windows Named Mutex
- Two-frame 400ms keyframe stability check + Pixel-diff gate
- VisionExtractor -> ScreenBotSmartController (Direct Disk Sync + AST Healer)
- Safe Hotkeys: Ctrl+Alt+Esc (Abort), Ctrl+Alt+Z (Undo), Ctrl+Alt+S (Manual Sync/Heal)
"""

from __future__ import annotations
import ctypes
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageGrab

try:
    import mss
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

try:
    from screen_bot.ai_vision_extractor import VisionExtractor
    from screen_bot.legacy.smart_sync_engine import ScreenBotSmartController
except ModuleNotFoundError:
    from ai_vision_extractor import VisionExtractor
    from smart_sync_engine import ScreenBotSmartController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("screen_bot.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("ScreenBot")


def acquire_single_instance_mutex(mutex_name: str = "Global\\ScreenBotPro_SingleInstance_Mutex") -> bool:
    """Prevents duplicate background instances on Windows."""
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    last_error = kernel32.GetLastError()
    if last_error == 183:  # ERROR_ALREADY_EXISTS
        logger.warning("ScreenBot is already running. Exiting duplicate instance.")
        return False
    # Keep handle alive for process lifetime
    acquire_single_instance_mutex._handle = handle
    return True


class StableScreenCapturer:
    """
    Captures screen using mss first (PIL ImageGrab fallback),
    enforces a pixel-difference gate, and grabs 2 frames 400ms apart
    to prevent reading half-rendered video keyframes.
    """

    def __init__(self, region: Optional[Tuple[int, int, int, int]] = None, diff_threshold: float = 1.8):
        self.region = region  # (left, top, width, height) or None for primary monitor
        self.diff_threshold = diff_threshold
        self._last_accepted_arr: Optional[np.ndarray] = None
        self._backoff_index = 0
        self._backoffs = (5.0, 10.0, 30.0)

    def grab_raw(self) -> Optional[Image.Image]:
        try:
            if HAS_MSS:
                with mss.mss() as sct:
                    mon = (
                        {
                            "left": self.region[0],
                            "top": self.region[1],
                            "width": self.region[2],
                            "height": self.region[3],
                        }
                        if self.region
                        else sct.monitors[1]
                    )
                    shot = sct.grab(mon)
                    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            else:
                bbox = (
                    (
                        self.region[0],
                        self.region[1],
                        self.region[0] + self.region[2],
                        self.region[1] + self.region[3],
                    )
                    if self.region
                    else None
                )
                img = ImageGrab.grab(bbox=bbox, all_screens=False)

            self._backoff_index = 0
            return img
        except OSError:
            wait_s = self._backoffs[min(self._backoff_index, len(self._backoffs) - 1)]
            self._backoff_index += 1
            logger.info(f"Display unavailable/locked. Backing off for {wait_s}s.")
            time.sleep(wait_s)
            return None

    def capture_stable_changed_frame(self) -> Tuple[Optional[Image.Image], str]:
        """
        Returns (PIL.Image, status_reason):
        - Rejects black frames (mean brightness < 3.0)
        - Skips unchanged screens (mean pixel diff < diff_threshold)
        - Verifies frame stability across a 400ms interval
        """
        f1 = self.grab_raw()
        if f1 is None:
            return None, "capture_unavailable"

        a1 = np.asarray(f1, dtype=np.float32)
        if float(a1.mean()) < 3.0:
            return None, "black_frame"

        if self._last_accepted_arr is not None and a1.shape == self._last_accepted_arr.shape:
            diff = float(np.abs(a1 - self._last_accepted_arr).mean())
            if diff < self.diff_threshold:
                return None, "unchanged_gate"

        # Wait 400ms to ensure lecturer's video keyframe finished rendering
        time.sleep(0.4)
        f2 = self.grab_raw()
        if f2 is None:
            return None, "capture_unavailable"

        a2 = np.asarray(f2, dtype=np.float32)
        stability_diff = float(np.abs(a2 - a1).mean())
        if stability_diff > 1.2:
            return None, "unstable_keyframe_in_motion"

        self._last_accepted_arr = a2
        return f2, "ok"


class ScreenBotRuntime:
    def __init__(self, workspace_roots=None):
        self.capturer = StableScreenCapturer()
        self.extractor = VisionExtractor()
        self.controller = ScreenBotSmartController(
            workspace_roots=workspace_roots, require_confirmations=2
        )
        self.auto_scan_enabled = False  # Always OFF on startup per safety spec
        self._stop_event = threading.Event()
        self._last_abort_ts = 0.0

    def trigger_abort(self) -> None:
        now = time.monotonic()
        if now - self._last_abort_ts < 0.5:  # Debounced
            return
        self._last_abort_ts = now
        self.auto_scan_enabled = False
        logger.warning("[ABORT] Ctrl+Alt+Esc pressed -> Auto-scan paused immediately.")

    def trigger_undo(self) -> None:
        restored = self.controller.undo_last()
        logger.info(f"[UNDO] Ctrl+Alt+Z -> Restored previous snapshot: {restored}")

    def heal_active_file_now(self) -> None:
        """Hotkey handler: Immediately repairs syntax & indentation in the active IDE file even without a screen grab."""
        target = self.controller.resolver.resolve_active_file()
        if not target or not target.is_file():
            logger.info("[HEAL] No active file on disk resolved.")
            return
        raw = target.read_text(encoding="utf-8")
        lang = self.controller.healer.detect_language(raw, str(target))
        healed_lines, repairs = self.controller.healer.heal_and_indent(
            raw.splitlines(), lang=lang
        )
        new_code = "\n".join(healed_lines) + ("\n" if raw.endswith("\n") else "")
        if new_code != raw:
            self.controller._save_snapshot(str(target), raw, target)
            self.controller._atomic_write_file(target, new_code)
            logger.info(f"[HEAL] Repaired {target.name}: {repairs}")
        else:
            logger.info(f"[HEAL] {target.name} is already clean and formatted.")

    def run_single_class_tick(self) -> None:
        t_start = time.perf_counter()
        frame, status = self.capturer.capture_stable_changed_frame()
        t_cap = time.perf_counter() - t_start

        if frame is None:
            return

        vision_res = self.extractor.extract_from_image(frame)
        if not vision_res.ok:
            logger.warning(f"Vision error: {vision_res.error}")
            return

        t_merge0 = time.perf_counter()
        sync_res = self.controller.process_frame_and_sync(
            raw_extracted_text=vision_res.code,
            hint_filename=vision_res.tab_filename,
            overlay_detected=vision_res.overlay_detected,
        )
        t_merge = time.perf_counter() - t_merge0

        logger.info(
            f"[TICK] cap={t_cap:.2f}s | gemini={vision_res.latency_sec:.2f}s | "
            f"merge_sync={t_merge:.3f}s | mode={sync_res.delivery_mode} | "
            f"target={sync_res.target_path.name if sync_res.target_path else 'buffer'} | "
            f"added={len(sync_res.added_lines)} | repairs={len(sync_res.repairs_made)} | "
            f"notes={sync_res.notes}"
        )
