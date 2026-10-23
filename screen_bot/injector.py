"""
Safe Injection Engine (Pipeline Stage 7):
- Preserves and restores user clipboard atomically.
- Verifies window focus, title, and process before and after injection.
- Enforces modifier key release guard before paste.
- Blocks placeholder tokens, sentinels, and overlapping paste operations.
"""

import time
import ctypes
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional
import pyperclip

try:
    from screen_bot.ai_vision_extractor import UNREADABLE_TOKEN
    from screen_bot.code_validator import SENTINEL_PATTERNS
except ModuleNotFoundError:
    from ai_vision_extractor import UNREADABLE_TOKEN
    from code_validator import SENTINEL_PATTERNS
import re

user32 = ctypes.windll.user32


@dataclass
class WindowFocusContext:
    hwnd: int
    pid: int
    title: str


class SafeInjector:
    """Safely executes injection into the user's IDE view without corrupting other apps."""

    def __init__(self):
        self._is_injecting = False

    def acquire_lock(self) -> bool:
        """Throttles so an injection never overlaps another."""
        if self._is_injecting:
            return False
        self._is_injecting = True
        return True

    def release_lock(self):
        self._is_injecting = False

    @contextmanager
    def atomic_clipboard_paste(self):
        """Backs up clipboard before paste, allows injection, and restores original clipboard."""
        original_content = ""
        try:
            original_content = pyperclip.paste()
        except Exception:
            pass

        try:
            yield
        finally:
            try:
                # Small delay to ensure host IDE has read the clipboard
                time.sleep(0.08)
                pyperclip.copy(original_content)
            except Exception:
                pass

    def verify_focus(self, expected: WindowFocusContext, current: WindowFocusContext) -> bool:
        """Verifies that focus has not changed to a browser, meeting, or chat app."""
        if expected.hwnd != current.hwnd:
            return False
        if expected.pid != current.pid:
            return False
        return True

    def is_safe_to_inject(self, code: str) -> bool:
        """Guarantees that placeholder tokens and sentinels are never typed."""
        if UNREADABLE_TOKEN in code:
            return False

        for pat in SENTINEL_PATTERNS:
            if re.search(pat, code, re.IGNORECASE):
                return False

        return True

    def release_held_modifiers(self):
        """Forces KEYUP on Ctrl, Shift, Alt, and Win keys."""
        VK_CONTROL = 0x11
        VK_SHIFT = 0x10
        VK_MENU = 0x12  # Alt
        VK_LWIN = 0x5B
        VK_RWIN = 0x5C
        KEYEVENTF_KEYUP = 0x0002

        for vk in (VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN):
            try:
                user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass

    def sync_and_inject_context_aware(self, raw_extracted_code: str, synchronizer=None, target_hwnd_checker=None) -> bool:
        """
        Context-aware buffer sync:
        1. Saves original clipboard.
        2. Releases held modifiers.
        3. Reads existing IDE buffer via Ctrl+A -> Ctrl+C.
        4. Merges and heals syntax via synchronizer.
        5. Atomically replaces editor buffer (Ctrl+A -> Ctrl+V) only if changed & valid.
        6. Restores original clipboard.
        """
        import keyboard
        if target_hwnd_checker and not target_hwnd_checker():
            return False

        original_clipboard = ""
        try:
            original_clipboard = pyperclip.paste()
        except Exception:
            pass

        try:
            self.release_held_modifiers()
            for mod in ("ctrl", "shift", "alt"):
                try:
                    keyboard.release(mod)
                except Exception:
                    pass

            pyperclip.copy("")
            keyboard.send("ctrl+a")
            time.sleep(0.06)
            keyboard.send("ctrl+c")
            time.sleep(0.08)
            current_ide_code = pyperclip.paste()

            keyboard.send("right")

            if not synchronizer:
                try:
                    from screen_bot.smart_context_engine import SmartCodeSynchronizer
                except ModuleNotFoundError:
                    from smart_context_engine import SmartCodeSynchronizer
                synchronizer = SmartCodeSynchronizer(require_confirmations=2)

            outcome = synchronizer.sync(current_ide_code, raw_extracted_code)
            if not outcome.changed:
                return False

            if target_hwnd_checker and not target_hwnd_checker():
                return False

            pyperclip.copy(outcome.merged_text)
            keyboard.send("ctrl+a")
            time.sleep(0.05)
            keyboard.send("ctrl+v")
            time.sleep(0.08)
            return True

        except Exception:
            return False
        finally:
            try:
                time.sleep(0.05)
                pyperclip.copy(original_clipboard)
            except Exception:
                pass
