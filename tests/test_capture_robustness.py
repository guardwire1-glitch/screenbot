"""
Tests for Capture Robustness (Pipeline Stage 1):
- Uses mss first, ImageGrab as fallback.
- Handles locked/minimized/sleeping screens with backoff (5s, 10s, 30s) and zero traceback spam.
- Rejects completely black frames.
- Supports static region or target window handle capture.
"""

import pytest
from PIL import Image
from screen_bot.capture import (
    CaptureBackoffManager,
    is_frame_black_or_blank,
)


def test_capture_backoff_progression():
    mgr = CaptureBackoffManager()
    assert mgr.get_backoff_delay() == 0.0
    assert mgr.should_skip_tick() is False
    
    # 1st failure -> 5s backoff
    delay1 = mgr.record_failure("Screen locked or display sleeping")
    assert delay1 == 5.0
    assert mgr.should_skip_tick() is True
    
    # 2nd failure -> 10s backoff
    delay2 = mgr.record_failure("Screen locked or display sleeping")
    assert delay2 == 10.0
    
    # 3rd failure -> 30s backoff
    delay3 = mgr.record_failure("Screen locked or display sleeping")
    assert delay3 == 30.0
    
    # On success -> resets cleanly to 0
    mgr.record_success()
    assert mgr.get_backoff_delay() == 0.0
    assert mgr.should_skip_tick() is False


def test_black_frame_detection():
    black_img = Image.new("RGB", (200, 200), color=(0, 0, 0))
    white_img = Image.new("RGB", (200, 200), color=(255, 255, 255))
    code_img = Image.new("RGB", (200, 200), color=(30, 30, 30))
    
    assert is_frame_black_or_blank(black_img) is True
    assert is_frame_black_or_blank(white_img) is True  # flat white / no content
    # Normal code editor has text contrast
    from PIL import ImageDraw
    draw = ImageDraw.Draw(code_img)
    draw.text((10, 10), "public class Test {}", fill=(255, 255, 255))
    assert is_frame_black_or_blank(code_img) is False
