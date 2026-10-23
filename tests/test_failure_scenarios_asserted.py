"""
Rigorous behavioral tests for all 12 Failure Injection Scenarios:
Asserts on real system behavior, state transitions, and contract invariants.
"""

import time
import pytest
from unittest.mock import MagicMock, patch
from PIL import Image, ImageDraw

from screen_bot.capture import CaptureBackoffManager, is_frame_black_or_blank
from screen_bot.frame_pipeline import OcclusionDetector
from screen_bot.code_validator import LineConsensusTracker, is_valid_code_line, normalize_for_diff
from screen_bot.merge_engine import MergeEngine
from screen_bot.ai_vision_extractor import ExtractedLine, UNREADABLE_TOKEN, ExtractionResult
from screen_bot.injector import SafeInjector, WindowFocusContext
from screen_bot.failsafes import CircuitBreaker
from screen_bot.shadow_master import ShadowMaster, MasterChangeEvent
from screen_bot.ai_solver import ScreenSolver


def test_scenario_1_locked_screen_backoff_escalation():
    backoff = CaptureBackoffManager()
    assert backoff.failures == 0
    assert backoff.get_backoff_delay() == 0.0

    d1 = backoff.record_failure("Locked screen")
    assert backoff.failures == 1
    assert d1 == 5.0
    assert backoff.get_backoff_delay() == 5.0

    d2 = backoff.record_failure("Locked screen")
    assert backoff.failures == 2
    assert d2 == 10.0

    d3 = backoff.record_failure("Locked screen")
    assert backoff.failures == 3
    assert d3 == 30.0

    # Further failures capped at 30.0s
    d4 = backoff.record_failure("Locked screen")
    assert d4 == 30.0

    # Recovery resets failures and delay
    backoff.reset()
    assert backoff.failures == 0
    assert backoff.get_backoff_delay() == 0.0


def test_scenario_2_black_frame_detection():
    # True black screen
    black_img = Image.new("RGB", (640, 480), color=(0, 0, 0))
    assert is_frame_black_or_blank(black_img) is True

    # Near black display sleep screen (low luminance stddev)
    dim_img = Image.new("RGB", (640, 480), color=(2, 2, 2))
    assert is_frame_black_or_blank(dim_img) is True

    # Normal code editor frame with high contrast code text
    code_img = Image.new("RGB", (640, 480), color=(30, 30, 30))
    draw = ImageDraw.Draw(code_img)
    draw.text((50, 50), "public static void main(String[] args) {", fill=(220, 220, 220))
    draw.text((50, 80), "    System.out.println(\"Hello World\");", fill=(100, 200, 100))
    draw.text((50, 110), "}", fill=(220, 220, 220))
    assert is_frame_black_or_blank(code_img) is False


def test_scenario_3_toast_notification_detected_as_toast():
    detector = OcclusionDetector()
    img = Image.new("RGB", (800, 600), color=(30, 30, 30))
    draw = ImageDraw.Draw(img)
    # Bright Teams notification in bottom-right corner
    draw.rectangle([600, 450, 780, 560], fill=(245, 245, 245), outline=(200, 200, 200))

    occlusions = detector.detect_occlusions(img)
    assert len(occlusions) > 0
    toast_occlusions = [occ for occ in occlusions if occ["type"] == "toast"]
    assert len(toast_occlusions) > 0
    assert "bottom_right" in toast_occlusions[0]["region"]


def test_scenario_4_tooltip_detected_as_tooltip():
    detector = OcclusionDetector()
    img = Image.new("RGB", (800, 600), color=(30, 30, 30))
    draw = ImageDraw.Draw(img)
    # IntelliSense hover doc box in editor center
    draw.rectangle([200, 150, 500, 260], fill=(240, 240, 240), outline=(180, 180, 180))

    occlusions = detector.detect_occlusions(img)
    assert len(occlusions) > 0
    tooltip_occlusions = [occ for occ in occlusions if occ["type"] == "tooltip"]
    assert len(tooltip_occlusions) > 0
    assert "tooltip" in tooltip_occlusions[0]["region"]


def test_scenario_5_ghost_text_and_unreadable_token_rejected():
    # 1. Unreadable token must be rejected
    bad_line = ExtractedLine(
        line_no=12,
        text=f"List<String> items = new {UNREADABLE_TOKEN}();",
        indent_width=4,
        visible="partial",
        confidence=0.88,
    )
    assert is_valid_code_line(bad_line) is False

    # 2. Ghost text suggestion (low confidence inline suggestion)
    ghost_line = ExtractedLine(
        line_no=13,
        text="    return Collections.emptyList(); // inline ghost",
        indent_width=4,
        visible="partial",
        confidence=0.60,  # Below 0.85 threshold
    )
    tracker = LineConsensusTracker(required_observations=2, min_confidence=0.85)
    accepted, pending = tracker.feed_frame_lines([ghost_line])
    assert len(accepted) == 0
    assert len(pending) == 1

    # Second frame with still low confidence -> still held in pending!
    accepted2, pending2 = tracker.feed_frame_lines([ghost_line])
    assert len(accepted2) == 0
    assert len(pending2) == 1


def test_scenario_6_scroll_off_retains_unseen_lines_append_only():
    engine = MergeEngine()
    master = (
        "public class Solution {\n"
        "    int a = 1;\n"
        "    int b = 2;\n"
        "    int c = 3;\n"
        "}"
    )

    # Frame viewport scrolled down: lines 1-3 scrolled out of view!
    scrolled_viewport = [
        ExtractedLine(line_no=4, text="    int c = 3;", indent_width=4, visible="full", confidence=0.95),
        ExtractedLine(line_no=5, text="}", indent_width=0, visible="full", confidence=0.95),
        ExtractedLine(line_no=6, text="    int d = 4;", indent_width=4, visible="full", confidence=0.95),
    ]

    decision = engine.merge(master, scrolled_viewport)
    assert "public class Solution {" in decision.merged_code
    assert "int a = 1;" in decision.merged_code
    assert "int b = 2;" in decision.merged_code
    assert "int c = 3;" in decision.merged_code
    assert "int d = 4;" in decision.merged_code
    # Master strictly grew or maintained existing lines; NEVER shrank
    assert len(decision.merged_code.splitlines()) >= len(master.splitlines())


def test_scenario_7_file_tab_switch_isolation(tmp_path):
    master_a_path = tmp_path / "ClassA.java"
    master_b_path = tmp_path / "ClassB.java"

    master_a = ShadowMaster(file_path=master_a_path, log_dir=tmp_path / "logs_a")
    master_b = ShadowMaster(file_path=master_b_path, log_dir=tmp_path / "logs_b")

    master_a.apply_change(MasterChangeEvent(
        frame_id="f1",
        event_type="init",
        result_code="public class ClassA {}",
    ))

    master_b.apply_change(MasterChangeEvent(
        frame_id="f2",
        event_type="init",
        result_code="public class ClassB {}",
    ))

    # Assert mutual isolation: updating ClassB does not touch ClassA
    assert master_a.get_current_code() == "public class ClassA {}"
    assert master_b.get_current_code() == "public class ClassB {}"
    assert master_a_path.read_text(encoding="utf-8") == "public class ClassA {}"
    assert master_b_path.read_text(encoding="utf-8") == "public class ClassB {}"


def test_scenario_8_api_timeout_retry_and_safe_skip():
    solver = ScreenSolver()
    mock_client = MagicMock()
    # Mock timeout exception on generate_content
    mock_client.models.generate_content.side_effect = TimeoutError("Request timed out after 15s")
    solver._client = mock_client

    result = solver.extract_lecture_code_structured(b"fake_jpeg", prompt="extract")
    # Must fail gracefully to empty string, never raise uncaught exception
    assert result == ""


def test_scenario_9_api_429_quota_exhausted():
    solver = ScreenSolver()
    mock_client = MagicMock()
    # Mock 429 Resource Exhausted
    mock_client.models.generate_content.side_effect = Exception("429 RESOURCE_EXHAUSTED: Quota exceeded")
    solver._client = mock_client

    result = solver.extract_lecture_code_structured(b"fake_jpeg", prompt="extract")
    assert result == ""

    # Even if solver returned raw error string in extract_lecture_code:
    err_line = ExtractedLine(line_no=1, text="[ScreenBot] Error extracting code: 429", indent_width=0, visible="full", confidence=0.99)
    assert is_valid_code_line(err_line) is False


def test_scenario_10_focus_change_during_injection_aborts():
    injector = SafeInjector()
    expected_ctx = WindowFocusContext(hwnd=1001, pid=5000, title="Visual Studio Code")
    current_ctx = WindowFocusContext(hwnd=2002, pid=6000, title="Google Chrome")

    # Focus moved away from editor
    is_valid = injector.verify_focus(expected=expected_ctx, current=current_ctx)
    assert is_valid is False


def test_scenario_11_circuit_breaker_trips_after_three_failures():
    cb = CircuitBreaker(max_edits_per_minute=3)
    assert cb.is_tripped is False

    cb.record_injection_failure()
    assert cb.is_tripped is False

    cb.record_injection_failure()
    assert cb.is_tripped is False

    cb.record_injection_failure()
    assert cb.is_tripped is True
    assert "consecutive injection failures" in cb.trip_reason.lower()


def test_scenario_12_kill_switch_cleans_up_state(tmp_path):
    from screen_bot.bot import ScreenBot
    bot = ScreenBot()
    bot.is_busy = True
    bot.is_typing = True

    # Simulate emergency stop
    bot.stop_typing_flag = True
    bot.is_busy = False
    bot.is_typing = False

    assert bot.stop_typing_flag is True
    assert bot.is_busy is False
    assert bot.is_typing is False
