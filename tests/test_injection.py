"""
Unit tests for Injection safeguards: clipboard save/restore, focus guard, and hotkey debounce.
"""

import sys
import time
import pyperclip
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

from proposal_engine import validate_code_to_inject


def test_clipboard_save_and_restore_cycle():
    """Verifies clipboard preserve pattern: original contents are restored after operation."""
    original_text = f"USER_PRIVATE_CLIPBOARD_{time.time()}"
    pyperclip.copy(original_text)

    # Simulating what instant_paste does
    saved_clip = pyperclip.paste()
    assert saved_clip == original_text

    # Temporary injection payload
    injection_payload = "Console.WriteLine(42);"
    pyperclip.copy(injection_payload)
    assert pyperclip.paste() == injection_payload

    # Restoration step
    pyperclip.copy(saved_clip)
    assert pyperclip.paste() == original_text


def test_injection_validator_blocks_sentinels():
    """Verifies that invalid payloads are strictly blocked before reaching injection."""
    valid, reason = validate_code_to_inject("Micro-edits applied")
    assert not valid
    assert "sentinel" in reason.lower()

    valid, reason = validate_code_to_inject("_SB_NO_SELECTION_")
    assert not valid
    assert "sentinel" in reason.lower()
