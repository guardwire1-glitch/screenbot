"""
Tests for Merge Engine (Pipeline Stage 6):
- Substantive Anchor Matching (ignores lone braces, uses context windows).
- Mid-file insertion (inserts by anchor, not naive append).
- Gated micro-edits (fill empty "", typo fix >= 82%, literal change, max 3 lines).
- Large deletes / block replacements marked NOTIFY ONLY and not applied.
- Idempotency: merging same frame twice changes nothing.
"""

import pytest
from screen_bot.merge_engine import MergeEngine, MergeDecision
from screen_bot.ai_vision_extractor import ExtractedLine


def test_merge_is_strictly_idempotent():
    engine = MergeEngine()
    initial_master = "class Program {\n    static void Main() {}\n}"
    
    frame_lines = [
        ExtractedLine(line_no=1, text="class Program {", indent_width=0, visible="full", confidence=0.99),
        ExtractedLine(line_no=2, text="    static void Main() {}", indent_width=4, visible="full", confidence=0.99),
        ExtractedLine(line_no=3, text="}", indent_width=0, visible="full", confidence=0.99),
    ]
    
    decision1 = engine.merge(initial_master, frame_lines)
    assert decision1.merged_code == initial_master
    assert decision1.change_type == "noop"
    
    decision2 = engine.merge(decision1.merged_code, frame_lines)
    assert decision2.merged_code == initial_master
    assert decision2.change_type == "noop"


def test_merge_never_deletes_on_scroll():
    engine = MergeEngine()
    initial_master = "public class Solution {\n    int a = 1;\n    int b = 2;\n    int c = 3;\n}"
    
    # Lecturer scrolled down so only b, c, and } are visible
    frame_lines = [
        ExtractedLine(line_no=15, text="    int b = 2;", indent_width=4, visible="full", confidence=0.95),
        ExtractedLine(line_no=16, text="    int c = 3;", indent_width=4, visible="full", confidence=0.95),
        ExtractedLine(line_no=17, text="}", indent_width=0, visible="full", confidence=0.95),
    ]
    
    decision = engine.merge(initial_master, frame_lines)
    # Master must still contain line a = 1!
    assert "int a = 1;" in decision.merged_code
    assert "public class Solution {" in decision.merged_code


def test_merge_handles_mid_file_insertion():
    engine = MergeEngine()
    initial_master = "class Demo {\n    void Foo() {}\n    void Bar() {}\n}"
    
    # Lecturer inserts void Middle() {} between Foo and Bar
    frame_lines = [
        ExtractedLine(line_no=None, text="    void Foo() {}", indent_width=4, visible="full", confidence=0.95),
        ExtractedLine(line_no=None, text="    void Middle() {}", indent_width=4, visible="full", confidence=0.95),
        ExtractedLine(line_no=None, text="    void Bar() {}", indent_width=4, visible="full", confidence=0.95),
    ]
    
    decision = engine.merge(initial_master, frame_lines)
    assert "void Middle() {}" in decision.merged_code
    lines = decision.merged_code.splitlines()
    foo_idx = next(i for i, l in enumerate(lines) if "Foo" in l)
    mid_idx = next(i for i, l in enumerate(lines) if "Middle" in l)
    bar_idx = next(i for i, l in enumerate(lines) if "Bar" in l)
    assert foo_idx < mid_idx < bar_idx


def test_merge_allows_micro_edit_typo_fix():
    engine = MergeEngine()
    initial_master = "class Demo {\n    int couunt = 0;\n}"
    
    # Lecturer fixes typo 'couunt' -> 'count' (similarity > 82%)
    frame_lines = [
        ExtractedLine(line_no=None, text="class Demo {", indent_width=0, visible="full", confidence=0.95),
        ExtractedLine(line_no=None, text="    int count = 0;", indent_width=4, visible="full", confidence=0.95),
        ExtractedLine(line_no=None, text="}", indent_width=0, visible="full", confidence=0.95),
    ]
    
    decision = engine.merge(initial_master, frame_lines)
    assert "int count = 0;" in decision.merged_code
    assert decision.change_type == "micro_edit"


def test_merge_rejects_large_destructive_delete_as_notify_only():
    engine = MergeEngine()
    initial_master = "class Big {\n    void M1() {}\n    void M2() {}\n    void M3() {}\n}"
    
    # Frame replaces 3 methods with completely different code -> large replacement
    frame_lines = [
        ExtractedLine(line_no=None, text="class Big {", indent_width=0, visible="full", confidence=0.95),
        ExtractedLine(line_no=None, text="    void ReplacedEverything() {}", indent_width=4, visible="full", confidence=0.95),
        ExtractedLine(line_no=None, text="}", indent_width=0, visible="full", confidence=0.95),
    ]
    
    decision = engine.merge(initial_master, frame_lines)
    assert decision.is_notify_only is True
    # Master code remains unchanged
    assert "void M1() {}" in decision.merged_code
