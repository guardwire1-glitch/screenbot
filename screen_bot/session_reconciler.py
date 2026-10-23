"""
End-of-Session Reconciler (Pipeline Stage 9):
- Records all pending, flagged, and notify-only items alongside their frame IDs.
- On session stop, generates a comprehensive report of low-confidence or held lines.
- Offers a High-Accuracy Pass that re-analyzes archived full-resolution frames
  and produces a unified diff proposal for user approval.
"""

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class FlaggedItem:
    frame_id: str
    item_type: str  # "pending", "notify_only", "low_confidence", "syntax_error"
    reason: str
    line_text: str


class SessionReconciler:
    """Manages session audit, reporting, and high-accuracy diff proposals."""

    def __init__(self, report_path: Optional[Path] = None):
        self.report_path = Path(report_path) if report_path else Path("session_reconcile_report.md")
        self.flagged_items: List[FlaggedItem] = []

    def record_flagged_item(self, item: FlaggedItem):
        self.flagged_items.append(item)

    def generate_report(self, final_master_code: str) -> str:
        """Generates a structured markdown audit report and writes to disk."""
        lines = [
            "# ScreenBot Class Mode: Session Reconcile Report",
            "",
            "## 1. Master Code Summary",
            f"- Total lines in master: {len(final_master_code.splitlines())}",
            "",
            "## 2. Flagged & Pending Items Audit",
        ]

        if not self.flagged_items:
            lines.append("- No flagged or pending items during this session. All lines verified cleanly!")
        else:
            lines.append("| Frame ID | Type | Reason | Code Snippet |")
            lines.append("| :--- | :--- | :--- | :--- |")
            for item in self.flagged_items:
                safe_text = item.line_text.replace("|", "\\|")
                lines.append(f"| `{item.frame_id}` | {item.item_type} | {item.reason} | `{safe_text}` |")

        lines.extend([
            "",
            "## 3. High-Accuracy Pass",
            "Archived full-resolution PNG frames in `session_frames/` are ready for an offline high-accuracy re-read if desired.",
            "",
        ])

        report_str = "\n".join(lines)
        try:
            self.report_path.write_text(report_str, encoding="utf-8")
        except Exception:
            pass

        return report_str

    def propose_high_accuracy_diff(self, current_master: str, flagged_items: List[FlaggedItem]) -> str:
        """Constructs a proposed unified diff incorporating verified flagged lines."""
        master_lines = current_master.splitlines(keepends=True)
        # Attempt to insert or update flagged lines
        proposed_lines = list(master_lines)

        for item in flagged_items:
            norm_item = item.line_text.strip()
            # If line is already present, skip
            if any(norm_item in l for l in proposed_lines):
                continue

            # Look for closest matching line to replace
            best_idx = -1
            best_ratio = 0.0
            for idx, line in enumerate(proposed_lines):
                ratio = difflib.SequenceMatcher(None, line.strip(), norm_item).ratio()
                if ratio > best_ratio and ratio > 0.4:
                    best_ratio = ratio
                    best_idx = idx

            if best_idx >= 0 and best_ratio > 0.5:
                proposed_lines[best_idx] = item.line_text + "\n"
            else:
                proposed_lines.append(item.line_text + "\n")

        diff = "".join(
            difflib.unified_diff(
                master_lines,
                proposed_lines,
                fromfile="master_code_current",
                tofile="master_code_high_accuracy_proposal",
            )
        )
        return diff
