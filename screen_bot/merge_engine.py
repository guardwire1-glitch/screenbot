"""
Merge Engine (Pipeline Stage 6):
Anchor-based merge with gated micro-edits, middle-of-file insertion,
strict idempotency, and non-destructive scroll handling (Principle 1).
"""

import difflib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
try:
    from screen_bot.ai_vision_extractor import ExtractedLine
    from screen_bot.code_validator import normalize_for_diff
except ModuleNotFoundError:
    from ai_vision_extractor import ExtractedLine
    from code_validator import normalize_for_diff


@dataclass
class MergeDecision:
    merged_code: str
    change_type: str  # "noop", "append", "insert", "micro_edit", "notify_only"
    lines_added: List[int] = field(default_factory=list)
    lines_modified: List[int] = field(default_factory=list)
    is_notify_only: bool = False
    notification_reason: str = ""


class MergeEngine:
    """Merges newly observed frame lines into authoritative master code."""

    def __init__(self, max_micro_edit_lines: int = 3, min_similarity: float = 0.82):
        self.max_micro_edit_lines = max_micro_edit_lines
        self.min_similarity = min_similarity

    @staticmethod
    def is_substantive(text: str) -> bool:
        """Substantive lines are non-blank and not lone braces."""
        stripped = text.strip()
        if not stripped:
            return False
        if stripped in ("{", "}", "};", "});", "{};"):
            return False
        return True

    def merge(self, master_code: str, frame_lines: List[ExtractedLine]) -> MergeDecision:
        """Merges frame lines into master according to Core Design Principles."""
        raw_frame_texts = [l.text for l in frame_lines]
        if not master_code.strip():
            # Initial populate
            joined = "\n".join(raw_frame_texts)
            return MergeDecision(
                merged_code=joined,
                change_type="append",
                lines_added=list(range(1, len(raw_frame_texts) + 1)),
            )

        master_lines = master_code.splitlines()

        # Check 1: Idempotency (exact subset or exact match)
        joined_frame = "\n".join(raw_frame_texts)
        if joined_frame in master_code:
            return MergeDecision(merged_code=master_code, change_type="noop")

        # Normalize lines for diffing
        norm_master = [normalize_for_diff(l) for l in master_lines]
        norm_frame = [normalize_for_diff(l) for l in raw_frame_texts]

        matcher = difflib.SequenceMatcher(None, norm_master, norm_frame)
        opcodes = matcher.get_opcodes()

        # Check for destructive deletions (lines in master missing from frame)
        # Core Principle 1: Missing lines from frame are 'unseen', never 'deleted'
        deletions = [op for op in opcodes if op[0] == "delete"]
        replacements = [op for op in opcodes if op[0] == "replace"]
        inserts = [op for op in opcodes if op[0] == "insert"]

        # Case A: Check if this is a gated micro-edit (typo fix, completed truncated line, literal change)
        if replacements and not inserts:
            total_replaced_lines = sum(op[2] - op[1] for op in replacements)
            if total_replaced_lines <= self.max_micro_edit_lines:
                # Check similarity of replaced lines
                all_sim_high = True
                new_master = list(master_lines)
                for tag, i1, i2, j1, j2 in replacements:
                    old_sub = master_lines[i1:i2]
                    new_sub = raw_frame_texts[j1:j2]
                    if len(old_sub) == len(new_sub):
                        for o, n in zip(old_sub, new_sub):
                            sim = difflib.SequenceMatcher(None, o, n).ratio()
                            if sim < self.min_similarity and not (o.endswith("...") or o.strip().endswith('""')):
                                all_sim_high = False
                                break
                    else:
                        all_sim_high = False

                if all_sim_high:
                    for tag, i1, i2, j1, j2 in replacements:
                        new_master[i1:i2] = raw_frame_texts[j1:j2]
                    return MergeDecision(
                        merged_code="\n".join(new_master),
                        change_type="micro_edit",
                        lines_modified=list(range(replacements[0][1] + 1, replacements[-1][2] + 1)),
                    )
            # Replaced lines exceeds limit or is a big signature/block change -> NOTIFY ONLY
            return MergeDecision(
                merged_code=master_code,
                change_type="notify_only",
                is_notify_only=True,
                notification_reason=f"Detected large block modification ({total_replaced_lines} lines). Held for user approval.",
            )

        # Case B: Mid-file insertion or append
        if inserts and not replacements:
            new_master = list(master_lines)
            added_line_indices = []
            # Apply inserts in reverse order so indices remain stable
            for tag, i1, i2, j1, j2 in reversed(inserts):
                lines_to_insert = raw_frame_texts[j1:j2]
                new_master[i1:i1] = lines_to_insert
                added_line_indices.extend(range(i1 + 1, i1 + 1 + len(lines_to_insert)))

            return MergeDecision(
                merged_code="\n".join(new_master),
                change_type="insert" if any(op[1] < len(master_lines) for op in inserts) else "append",
                lines_added=added_line_indices,
            )

        # Case C: Overlapping scroll window (Principle 1)
        # Find largest suffix of master matching prefix of frame
        max_overlap = 0
        overlap_frame_idx = 0
        for k in range(min(len(master_lines), len(raw_frame_texts)), 0, -1):
            if norm_master[-k:] == norm_frame[:k]:
                max_overlap = k
                overlap_frame_idx = k
                break

        if max_overlap > 0:
            new_lines = raw_frame_texts[overlap_frame_idx:]
            if new_lines:
                combined = master_lines + new_lines
                return MergeDecision(
                    merged_code="\n".join(combined),
                    change_type="append",
                    lines_added=list(range(len(master_lines) + 1, len(combined) + 1)),
                )
            return MergeDecision(merged_code=master_code, change_type="noop")

        # If unsure: do nothing and notify (Core Design Principle 4)
        return MergeDecision(
            merged_code=master_code,
            change_type="noop",
            is_notify_only=False,
            notification_reason="",
        )
