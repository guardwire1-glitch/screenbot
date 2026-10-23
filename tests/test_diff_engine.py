"""
Pytest replay tests using saved lecture frames to verify diff engine stability
against all past bug classes.
"""

import sys
from pathlib import Path

# Add screen_bot to path
sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

from bot import compute_code_delta
from proposal_engine import (
    validate_code_to_inject,
    normalize_code_line,
    is_valid_micro_edit,
    MasterCodeTracker,
    EditProposal,
)
from fixtures_lecture import (
    FRAME_SCROLL_TOP,
    FRAME_SCROLL_BOTTOM,
    FRAME_TOOLTIP_OCCLUDED,
    FRAME_TOOLTIP_CLEARED,
    FRAME_LONE_BRACE_MASTER,
    FRAME_LONE_BRACE_NEW,
    FRAME_QUOTES_MASTER,
    FRAME_QUOTES_SINGLE,
)


def test_scroll_off_preserves_earlier_code():
    """Bug #11 / #14: When lecturer scrolls down, lines 1-10 leave the viewport.
    The diff engine must NOT delete lines 1-10 from the master code."""
    delta, updated_master = compute_code_delta(FRAME_SCROLL_TOP, FRAME_SCROLL_BOTTOM)

    # Top variable declarations must STILL be in the master code
    assert "decimal decValue = 123456.789m;" in updated_master
    assert "int intValue = 42;" in updated_master
    assert 'Console.WriteLine("Currency: {0:C}", decValue);' in updated_master

    # New lines from bottom viewport must be appended
    assert 'Console.WriteLine("Exponential: {0:E}", decValue);' in updated_master
    assert 'Console.WriteLine("Fixed-point: {0:F}", decValue);' in updated_master
    assert 'Console.WriteLine("General: {0:G}", decValue);' in updated_master

    # Delta text must contain ONLY the newly added lines, not the whole file
    assert "decValue = 123456.789m" not in delta
    assert 'Exponential: {0:E}' in delta


def test_tooltip_occlusion_and_completion():
    """Bug #12: Tooltip occludes line 2, which then clears.
    Must complete the line without duplicate lines or syntax damage."""
    delta, updated_master = compute_code_delta(FRAME_TOOLTIP_OCCLUDED, FRAME_TOOLTIP_CLEARED)
    assert 'Console.WriteLine("Decimal: {0:D}", intValue);' in updated_master
    # No duplicate currency line
    assert updated_master.count("Currency: {0:C}") == 1


def test_lone_brace_anchor_does_not_return_dummy_or_empty():
    """Bug #17 / #18: File ends in closing braces '}'.
    Diff engine must NOT match '}' as anchor and must NOT return empty delta
    or dummy text like 'Micro-edits applied'."""
    delta, updated_master = compute_code_delta(FRAME_LONE_BRACE_MASTER, FRAME_LONE_BRACE_NEW)

    # Delta must contain the new statements
    assert 'Console.WriteLine("d format: {0:d}", dt);' in delta
    assert 'Console.WriteLine("D format: {0:D}", dt);' in delta
    assert 'Console.WriteLine("t format: {0:t}", dt);' in delta

    # Delta must NEVER contain placeholder text
    assert "Micro-edits applied" not in delta
    assert "_SB_" not in delta

    # Master must have all statements inside Main before the closing braces
    assert 'Console.WriteLine("d format: {0:d}", dt);' in updated_master
    assert updated_master.strip().endswith("}")


def test_quote_and_whitespace_differences_prevent_duplicate_typing():
    """Bug #13 / #16: Slight whitespace or quote difference (e.g. ' vs ")
    must normalize and produce zero delta instead of duplicate re-typing."""
    delta, updated_master = compute_code_delta(FRAME_QUOTES_MASTER, FRAME_QUOTES_SINGLE)
    # Since they normalize to the same code statement, delta must be empty
    assert delta.strip() == ""


def test_no_duplicate_retyping_on_identical_consecutive_frames():
    """Bug #5: Scanning identical screens must result in 0 injected characters."""
    delta, updated_master = compute_code_delta(FRAME_SCROLL_TOP, FRAME_SCROLL_TOP)
    assert delta == ""
    assert updated_master == FRAME_SCROLL_TOP


def test_sentinel_guard_rejects_placeholders_and_short_text():
    """Sentinel guard refuses dummy text, short payloads (<3 chars), or AI markers."""
    bad_payloads = [
        "",
        "  ",
        "ab",
        "_SB_NO_SELECTION_",
        "Micro-edits applied",
        "[ScreenBot] Error: Not ready",
        "[REDACTED_API_KEY]",
        "Here is the solution for your code:",
        "Certainly! Let me help you with that.",
        "```csharp\nConsole.WriteLine();\n```",
    ]
    for p in bad_payloads:
        valid, reason = validate_code_to_inject(p, subject="csharp")
        assert not valid, f"Expected '{p}' to be rejected, but got valid with reason: {reason}"

    # Valid C# statement must pass
    valid, reason = validate_code_to_inject('Console.WriteLine("Total: " + total);', subject="csharp")
    assert valid, f"Valid code was unexpectedly rejected: {reason}"


def test_master_tracker_snapshots_and_undo():
    """Verifies that MasterCodeTracker keeps up to 20 snapshots and supports one-key undo."""
    tracker = MasterCodeTracker("line 1;\nline 2;")
    prop1 = EditProposal(op="append", line=3, old="", new="line 3;")
    applied, msg, _ = tracker.apply_proposal(prop1)
    assert applied
    assert "line 3;" in tracker.master_code

    # Perform undo (Ctrl+Alt+Z)
    restored = tracker.undo()
    assert restored is not None
    assert "line 3;" not in restored
    assert restored == "line 1;\nline 2;"


def test_circuit_breaker_trips_on_excessive_edits():
    """Verifies circuit breaker trips if >3 edits happen in 60s or same line edited twice."""
    tracker = MasterCodeTracker("int a = 1;\nint b = 2;\nint c = 3;\nint d = 4;\nint e = 5;")

    # Apply 3 rapid micro-edits to different lines
    tracker.apply_proposal(EditProposal(op="replace", line=1, old="int a = 1;", new="int a = 10;"))
    tracker.apply_proposal(EditProposal(op="replace", line=2, old="int b = 2;", new="int b = 20;"))
    tracker.apply_proposal(EditProposal(op="replace", line=3, old="int c = 3;", new="int c = 30;"))

    # 4th edit should be blocked by circuit breaker
    applied, msg, _ = tracker.apply_proposal(EditProposal(op="replace", line=4, old="int d = 4;", new="int d = 40;"))
    assert not applied
    assert tracker.circuit_breaker_tripped
    assert "Circuit breaker" in msg
