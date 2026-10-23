"""
Tests for End-of-Session Reconcile (Pipeline Stage 9):
- Final master summary report.
- Audit list of pending/flagged/notify-only items with frame IDs.
- High-accuracy pass proposing fixes in diff format for approval.
"""

import pytest
from pathlib import Path
from screen_bot.session_reconciler import SessionReconciler, FlaggedItem


def test_session_report_generation(tmp_path):
    report_file = tmp_path / "session_reconcile_report.md"
    reconciler = SessionReconciler(report_path=report_file)
    
    reconciler.record_flagged_item(
        FlaggedItem(
            frame_id="frame_015",
            item_type="pending",
            reason="Low confidence 0.65",
            line_text="int total = calculate();",
        )
    )
    reconciler.record_flagged_item(
        FlaggedItem(
            frame_id="frame_042",
            item_type="notify_only",
            reason="Large block replacement detected",
            line_text="void Replaced() {}",
        )
    )
    
    report_content = reconciler.generate_report(final_master_code="class Solution {}")
    assert "frame_015" in report_content
    assert "frame_042" in report_content
    assert report_file.exists()


def test_high_accuracy_pass_proposes_diff():
    reconciler = SessionReconciler()
    
    current_master = "class Solution {\n    int a = 0;\n}"
    
    # Lecturer had written int a = 10; which was low confidence in frame 15
    flagged = [
        FlaggedItem(
            frame_id="frame_015",
            item_type="pending",
            reason="confidence 0.60",
            line_text="    int a = 10;",
        )
    ]
    
    diff = reconciler.propose_high_accuracy_diff(current_master, flagged)
    assert "-    int a = 0;" in diff or "+    int a = 10;" in diff
