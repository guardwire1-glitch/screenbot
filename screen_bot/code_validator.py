"""
Code Validator (Pipeline Stage 5):
Deterministic Python validation gates.
- normalize_for_diff(): used ONLY for diff comparison; never written to master or IDE.
- LineConsensusTracker: enforces that a line must be observed in 2 consecutive stable frames
  with identical normalized text and confidence >= 0.85 before admission to master.
- Rejection of unreadable tokens («UNREADABLE»), sentinels, and non-code artifacts.
- Deterministic syntax verification for all supported languages.
"""

import re
from typing import List, Tuple, Dict, Optional
try:
    from screen_bot.ai_vision_extractor import ExtractedLine, UNREADABLE_TOKEN
except ModuleNotFoundError:
    from ai_vision_extractor import ExtractedLine, UNREADABLE_TOKEN

SENTINEL_PATTERNS = [
    r"\[ScreenBot.*?\]",
    r"undefined",
    r"null",
    r"NaN",
    r"```",
    r"AI Vision",
    r"Aptech",
    r"temperature: 0",
]


def normalize_for_diff(text: str) -> str:
    """Normalizes string for comparison only. Strips all leading/trailing whitespace and collapses spaces."""
    return re.sub(r"\s+", " ", text.strip())


def is_valid_code_line(line: ExtractedLine) -> bool:
    """Rejects placeholder tokens, sentinel patterns, and obvious UI labels."""
    if not line.text or not line.text.strip():
        return True  # Empty lines are valid code separators

    if UNREADABLE_TOKEN in line.text:
        return False

    for pat in SENTINEL_PATTERNS:
        if re.search(pat, line.text, re.IGNORECASE):
            return False

    # Check for obvious non-code UI artifacts (e.g. browser URL bars, Teams meeting banners)
    lower = line.text.lower()
    if any(k in lower for k in ["http://", "https://", "meet.google.com", "teams.microsoft.com", "youtube.com/watch"]):
        return False

    return True


class LineConsensusTracker:
    """Tracks consecutive observations across frames. A line must be observed in 2 consecutive
    stable frames with identical normalized text and confidence >= 0.85 before being accepted."""

    def __init__(self, required_observations: int = 2, min_confidence: float = 0.85):
        self.required_observations = required_observations
        self.min_confidence = min_confidence
        # Map: normalized_line -> {"count": int, "last_raw": ExtractedLine, "last_seen_frame": int}
        self._history: Dict[str, Dict] = {}
        self._frame_counter: int = 0

    def feed_frame_lines(self, lines: List[ExtractedLine]) -> Tuple[List[ExtractedLine], List[ExtractedLine]]:
        """
        Feeds a frame's lines.
        Returns (accepted_lines, pending_lines).
        """
        self._frame_counter += 1
        accepted = []
        pending = []

        frame_occ_counts: Dict[str, int] = {}

        for l in lines:
            if not is_valid_code_line(l):
                continue

            norm = normalize_for_diff(l.text)
            occ_idx = frame_occ_counts.get(norm, 0)
            frame_occ_counts[norm] = occ_idx + 1

            # Non-blank lines must meet confidence threshold
            if norm and l.confidence < self.min_confidence:
                pending.append(l)
                continue

            key = (norm, occ_idx)
            entry = self._history.get(key)
            if entry and (self._frame_counter - entry["last_seen_frame"] == 1):
                # Consecutive frame observation
                entry["count"] += 1
                entry["last_seen_frame"] = self._frame_counter
                entry["last_raw"] = l
            else:
                entry = {
                    "count": 1,
                    "last_seen_frame": self._frame_counter,
                    "last_raw": l,
                }
                self._history[key] = entry

            if entry["count"] >= self.required_observations:
                accepted.append(l)
            else:
                pending.append(l)

        # Decay/prune history entries not seen in this frame
        dead_keys = [k for k, v in self._history.items() if self._frame_counter - v["last_seen_frame"] > 4]
        for k in dead_keys:
            del self._history[k]

        return accepted, pending


def validate_syntax(code: str, language: str = "general") -> Tuple[bool, str]:
    """
    Validates code syntax deterministically:
    - Balanced braces, brackets, and parentheses.
    - String literal termination.
    - Never injects broken syntax into IDE.
    """
    if not code.strip():
        return True, ""

    stack = []
    pairs = {"{": "}", "(": ")", "[": "]"}
    in_string = False
    string_char = ""
    in_single_line_comment = False
    in_multi_line_comment = False

    i = 0
    n = len(code)

    while i < n:
        char = code[i]
        next_char = code[i + 1] if i + 1 < n else ""

        if in_single_line_comment:
            if char == "\n":
                in_single_line_comment = False
            i += 1
            continue

        if in_multi_line_comment:
            if char == "*" and next_char == "/":
                in_multi_line_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_string:
            if char == "\\" and i + 1 < n:
                i += 2
                continue
            if char == string_char:
                in_string = False
            i += 1
            continue

        # Check comment start
        if char == "/" and next_char == "/":
            in_single_line_comment = True
            i += 2
            continue
        if char == "/" and next_char == "*":
            in_multi_line_comment = True
            i += 2
            continue

        # Check string start
        if char in ('"', "'", "`"):
            in_string = True
            string_char = char
            i += 1
            continue

        # Check delimiters
        if char in pairs:
            stack.append((char, i))
        elif char in pairs.values():
            if not stack:
                return False, f"Unmatched closing bracket '{char}' at index {i}"
            opening, _ = stack.pop()
            if pairs[opening] != char:
                return False, f"Mismatched bracket '{opening}' closed with '{char}' at index {i}"

        i += 1

    if in_string:
        return False, "Unterminated string literal"
    if in_multi_line_comment:
        return False, "Unterminated multi-line comment"
    if stack:
        unclosed, idx = stack[-1]
        return False, f"Unbalanced delimiter '{unclosed}' opened at index {idx}"

    return True, ""
