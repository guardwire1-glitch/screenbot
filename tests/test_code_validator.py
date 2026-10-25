"""
Tests for Code Validator (Pipeline Stage 5):
- normalize_for_diff(): normalizes for comparison only; never writes normalized text.
- 2 Consecutive Stable Frames Gate (confidence >= 0.85).
- Rejection of unreadable placeholders («UNREADABLE»), sentinels, and non-code noise.
- Syntax verification: holds lines as pending if syntax fails; never injects pending lines.
"""

import pytest
from screen_bot.code_validator import (
    normalize_for_diff,
    LineConsensusTracker,
    is_valid_code_line,
    looks_incomplete,
    validate_syntax,
)
from screen_bot.ai_vision_extractor import ExtractedLine, UNREADABLE_TOKEN


def test_normalize_for_diff_strips_for_comparison_only():
    raw1 = "    int count = 10;   "
    raw2 = "int count = 10;"
    assert normalize_for_diff(raw1) == normalize_for_diff(raw2)
    # Ensure original text formatting is preserved when retrieving raw
    assert raw1 != raw2


def test_two_consecutive_frames_gate():
    tracker = LineConsensusTracker(required_observations=2, min_confidence=0.85)
    
    line = ExtractedLine(line_no=1, text="int x = 42;", indent_width=0, visible="full", confidence=0.90)
    
    # First frame: held as pending (seen 1 time)
    accepted, pending = tracker.feed_frame_lines([line])
    assert len(accepted) == 0
    assert len(pending) == 1
    
    # Second frame with same line: accepted into master!
    accepted, pending = tracker.feed_frame_lines([line])
    assert len(accepted) == 1
    assert accepted[0].text == "int x = 42;"


def test_rejects_unreadable_token_and_sentinels():
    line_unreadable = ExtractedLine(line_no=1, text=f"void test({UNREADABLE_TOKEN})", indent_width=0, visible="partial", confidence=0.90)
    line_sentinel = ExtractedLine(line_no=2, text="[ScreenBot Error] 429 quota", indent_width=0, visible="full", confidence=0.99)
    line_valid = ExtractedLine(line_no=3, text="Console.WriteLine(\"Hello\");", indent_width=0, visible="full", confidence=0.95)
    
    assert is_valid_code_line(line_unreadable) is False
    assert is_valid_code_line(line_sentinel) is False
    assert is_valid_code_line(line_valid) is True


def test_syntax_validation():
    valid_csharp = "class Program { static void Main() { System.Console.WriteLine(); } }"
    invalid_csharp = "class Program { static void Main() { System.Console.WriteLine(); "  # Unbalanced
    
    is_valid_ok, err_ok = validate_syntax(valid_csharp, language="csharp")
    is_valid_bad, err_bad = validate_syntax(invalid_csharp, language="csharp")
    
    assert is_valid_ok is True
    assert is_valid_bad is False
    assert "unbalanced" in err_bad.lower()


def test_repeated_lines_in_same_frame_never_advance_count_to_accepted():
    tracker = LineConsensusTracker(required_observations=2, min_confidence=0.85)
    
    # Frame 1 contains two closing braces on separate lines: '}' and '}'
    frame1 = [
        ExtractedLine(line_no=10, text="    }", indent_width=4, visible="full", confidence=0.95),
        ExtractedLine(line_no=11, text="}", indent_width=0, visible="full", confidence=0.95),
    ]
    
    accepted1, pending1 = tracker.feed_frame_lines(frame1)
    # A line seen once in Frame 1 MUST NOT reach accepted, even if repeated!
    assert len(accepted1) == 0, f"Expected 0 accepted on frame 1, but got {len(accepted1)}: {[l.text for l in accepted1]}"
    assert len(pending1) == 2


def _line(no: int, text: str) -> ExtractedLine:
    return ExtractedLine(
        line_no=no, text=text, indent_width=0, visible="full", confidence=0.95
    )


def test_active_line_hold_waits_extra_frames_for_balanced_final_line():
    tracker = LineConsensusTracker(
        required_observations=2, min_confidence=0.85, active_line_hold_frames=2
    )
    frame = [_line(1, "int x = 1;"), _line(2, "int y = 2;")]

    first, _ = tracker.feed_frame_lines(frame, hold_active_line=True)
    assert [l.text for l in first] == []
    second, pending = tracker.feed_frame_lines(frame, hold_active_line=True)
    assert [l.text for l in second] == ["int x = 1;"]
    assert [l.text for l in pending] == ["int y = 2;"]
    third, pending = tracker.feed_frame_lines(frame, hold_active_line=True)
    assert [l.text for l in third] == ["int x = 1;"]
    assert [l.text for l in pending] == ["int y = 2;"]
    fourth, pending = tracker.feed_frame_lines(frame, hold_active_line=True)
    assert [l.text for l in fourth] == ["int x = 1;", "int y = 2;"]
    assert pending == []


def test_active_line_hold_releases_when_a_line_appears_below():
    tracker = LineConsensusTracker(
        required_observations=2, min_confidence=0.85, active_line_hold_frames=10
    )
    first = [_line(1, "int a = 1;")]
    tracker.feed_frame_lines(first, hold_active_line=True)
    _, pending = tracker.feed_frame_lines(first, hold_active_line=True)
    assert [l.text for l in pending] == ["int a = 1;"]   # still the caret line

    second = [_line(1, "int a = 1;"), _line(2, "int b = 2;")]
    accepted, pending = tracker.feed_frame_lines(second, hold_active_line=True)
    assert [l.text for l in accepted] == ["int a = 1;"]
    assert [l.text for l in pending] == ["int b = 2;"]


def test_active_line_hold_exempts_structural_closers():
    tracker = LineConsensusTracker(
        required_observations=2, min_confidence=0.85, active_line_hold_frames=10
    )
    frame = [_line(1, "int x = 1;"), _line(2, "}")]
    tracker.feed_frame_lines(frame, hold_active_line=True)
    accepted, pending = tracker.feed_frame_lines(frame, hold_active_line=True)
    assert [l.text for l in accepted] == ["int x = 1;", "}"]
    assert pending == []


def test_incomplete_caret_line_is_never_admitted_automatically():
    tracker = LineConsensusTracker(
        required_observations=2, min_confidence=0.85, active_line_hold_frames=2
    )
    frame = [_line(1, "int x = 1;"), _line(2, "int total =")]

    for _ in range(8):
        accepted, pending = tracker.feed_frame_lines(frame, hold_active_line=True)
        assert "int total =" not in [l.text for l in accepted]
        assert "int total =" in [l.text for l in pending]

    # The user pressed the manual/force hotkey: normal consensus applies.
    accepted, _ = tracker.feed_frame_lines(frame, hold_active_line=False)
    assert "int total =" in [l.text for l in accepted]


def test_looks_incomplete_distinguishes_typed_code_from_finished_lines():
    assert looks_incomplete("int total =")
    assert looks_incomplete('System.out.print("Hello')
    assert looks_incomplete("foo(")
    assert looks_incomplete("if (ready)")
    assert looks_incomplete("public void F() {")
    assert looks_incomplete("return")
    assert looks_incomplete("value = a +")
    assert not looks_incomplete("int total = 0;")
    assert not looks_incomplete("System.out.println(\"Hello\");")
    assert not looks_incomplete("}")
    assert not looks_incomplete("// a comment being typed")
    assert not looks_incomplete("")


def test_blank_lines_require_two_consecutive_frames_to_accept():
    tracker = LineConsensusTracker(required_observations=2, min_confidence=0.85)

    # Frame 1 has blank lines and code lines
    frame1 = [
        ExtractedLine(line_no=1, text="", indent_width=0, visible="full", confidence=1.0),
        ExtractedLine(line_no=2, text="public class Test {", indent_width=0, visible="full", confidence=0.95),
        ExtractedLine(line_no=3, text="", indent_width=0, visible="full", confidence=1.0),
    ]

    accepted1, pending1 = tracker.feed_frame_lines(frame1)
    # ZERO lines allowed on observation 1
    assert len(accepted1) == 0
    assert len(pending1) == 3

    # Frame 2: identical second observation -> all accepted together
    accepted2, pending2 = tracker.feed_frame_lines(frame1)
    assert len(accepted2) == 3
    assert len(pending2) == 0

