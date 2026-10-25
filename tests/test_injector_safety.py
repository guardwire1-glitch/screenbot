"""
Tests for Injection Safety (Pipeline Stage 7):
- Clipboard backup and restore.
- Target focus & window title verification.
- Rejection of placeholder or sentinel text.
- Modifier key release.
- Concurrency throttle preventing overlapping pastes.
"""

import time
import pytest
import pyperclip
from screen_bot.legacy.injector import SafeInjector, WindowFocusContext
from screen_bot.ai_vision_extractor import UNREADABLE_TOKEN


def test_clipboard_save_and_restore(monkeypatch):
    original_clipboard = "MY_ORIGINAL_CLIPBOARD_TEXT_12345"
    pyperclip.copy(original_clipboard)
    
    injector = SafeInjector()
    
    # Simulate a paste action that uses clipboard
    with injector.atomic_clipboard_paste():
        pyperclip.copy("TEMP_PASTE_CODE")
        assert pyperclip.paste() == "TEMP_PASTE_CODE"
        
    # After context exits, original clipboard text MUST be restored
    assert pyperclip.paste() == original_clipboard


def test_aborts_if_window_focus_changes():
    injector = SafeInjector()
    
    expected_ctx = WindowFocusContext(hwnd=12345, pid=5000, title="Visual Studio Code")
    # Simulate focus change to Chrome/Teams
    changed_ctx = WindowFocusContext(hwnd=99999, pid=6000, title="Microsoft Teams")
    
    can_proceed = injector.verify_focus(expected=expected_ctx, current=changed_ctx)
    assert can_proceed is False


def test_refuses_to_inject_placeholder_or_sentinels():
    injector = SafeInjector()
    
    bad_code_1 = f"int x = {UNREADABLE_TOKEN};"
    bad_code_2 = "[ScreenBot Error] quota exceeded"
    good_code = "Console.WriteLine(123);"
    
    assert injector.is_safe_to_inject(bad_code_1) is False
    assert injector.is_safe_to_inject(bad_code_2) is False
    assert injector.is_safe_to_inject(good_code) is True


def test_injection_throttle_blocks_concurrent_paste():
    injector = SafeInjector()
    
    assert injector.acquire_lock() is True
    # Attempting to acquire again while busy must fail/throttle
    assert injector.acquire_lock() is False
    injector.release_lock()
    assert injector.acquire_lock() is True
    injector.release_lock()
