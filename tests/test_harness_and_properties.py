"""
Tests for Replay Harness, Property Tests, and Fuzzing (Pipeline Stage 10):
- Synthetic Frame Generator with transforms (scroll, compression, toast, tooltip, ghost text, res drop).
- Invariant property tests:
  * Master never shrinks on missing lines (Principle 1).
  * Reprocessing a frame is a no-op (Idempotency).
  * Legitimate repeated lines are not dropped.
  * No sentinel text is ever injected into master.
- Fuzz test runner (1,000 simulated sessions with 0 deleted, duplicated, or invented lines).
- Self-test routine with PASS/FAIL checks.
"""

import pytest
from screen_bot.harness import (
    SyntheticFrameGenerator,
    run_property_invariants_check,
    run_fuzz_sessions,
    run_selftest,
)


def test_synthetic_frame_generator_renders_and_transforms():
    gen = SyntheticFrameGenerator()
    code = [
        "public class Demo {",
        "    int a = 1;",
        "    int b = 2;",
        "}",
    ]
    frame = gen.render_code_frame(code, scroll_offset=1)
    assert frame.size == (1280, 720)
    
    # Apply transforms
    with_toast = gen.apply_toast_overlay(frame)
    with_tooltip = gen.apply_tooltip(with_toast)
    with_drop = gen.apply_resolution_drop(with_tooltip, scale=0.5)
    
    assert with_drop.size == (640, 360)


def test_property_invariants():
    results = run_property_invariants_check()
    assert results["master_never_shrinks"] is True
    assert results["reprocessing_is_noop"] is True
    assert results["repeated_line_preserved"] is True
    assert results["no_sentinels_injected"] is True


def test_fuzz_sessions_zero_errors():
    # Run 100 fast fuzz sessions in unit test (and 1000 in full suite)
    fuzz_result = run_fuzz_sessions(session_count=100)
    assert fuzz_result["deleted_lines"] == 0
    assert fuzz_result["duplicated_lines"] == 0
    assert fuzz_result["invented_lines"] == 0
    assert fuzz_result["passed"] is True


def test_selftest_checks():
    report = run_selftest(check_api=False)  # offline unit check
    assert report["clipboard_access"] == "PASS"
    assert report["write_access"] == "PASS"
    assert report["capture_system"] == "PASS"
