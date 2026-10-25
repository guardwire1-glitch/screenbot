"""
Safe Injection Engine (Pipeline Stage 7): QUARANTINED legacy helper.

Only its clipboard/focus utilities survive for their unit tests. The unsafe
select-all buffer read/write path was removed. The live bot does not import
this module.
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
