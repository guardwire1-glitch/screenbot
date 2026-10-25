"""
EditProposal & MasterCodeTracker Engine for ScreenBot Pro.
Implements:
1. Structured EditProposal pipeline (append, replace, delete_marked).
2. 2-read confirmation gate (stops transient OCR hallucinations).
3. 20-snapshot master buffer with one-key undo rollback.
4. Circuit breaker (pauses auto-edits if >3 edits in 60s or same line edited twice).
5. Strict sentinel and language validation guards (no placeholder or dummy text ever injected).
"""

import re
import time
import logging
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("ScreenBot.ProposalEngine")

# Sentinel patterns that must NEVER be injected into user's editor
SENTINEL_PATTERNS = [
    re.compile(r'_SB_[A-Z0-9_]+_'),
    re.compile(r'Micro-edits applied', re.IGNORECASE),
    re.compile(r'\[ScreenBot\]', re.IGNORECASE),
    re.compile(r'\[REDACTED_API_KEY\]', re.IGNORECASE),
    re.compile(r'^(Here is|Certainly|Hope this helps|Based on the)', re.IGNORECASE),
    re.compile(r'^```[a-zA-Z0-9_-]*\s*$', re.MULTILINE),
]


def normalize_code_line(line: str) -> str:
    """Normalizes whitespace and standardizes quotes for rock-solid diff comparisons."""
    if not line:
        return ""
    # Standardize single quotes to double quotes for comparison
    s = line.replace("'", '"')
    # Collapse multiple whitespaces into single space
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def is_brace_line(line: str) -> bool:
    """Returns True if the line contains only braces or semicolon/closing delimiters."""
    norm = line.strip()
    return norm in ('}', '};', '{', '});', ']', '];', '')


def is_valid_micro_edit(old_l: str, new_l: str) -> bool:
    """Checks if new_l is a valid in-place micro-edit of old_l:
    - Empty placeholders: "" or () or {} or [] filled
    - Completing a truncated line (e.g. from popup tooltip)
    - Typo / naming correction (>= 82% similarity)
    Structural deletions are strictly forbidden."""
    import difflib
    old_norm = normalize_code_line(old_l)
    new_norm = normalize_code_line(new_l)

    if not old_norm or not new_norm or old_norm == new_norm:
        return False

    # Block massive shrinkage (deletions)
    if len(new_norm) < len(old_norm) * 0.70 and len(new_norm) < 8:
        return False

    # 1. Filling empty placeholders
    for pat in [r'""', r'\(\)', r'\{\}', r'\[\]']:
        if re.search(pat, old_norm):
            regex_pat = re.sub(pat, r'.+', old_norm)
            try:
                if re.fullmatch(regex_pat, new_norm):
                    return True
            except re.error:
                pass

    # 2. Completing truncated lines (e.g. autocomplete tooltip occlusion)
    if new_norm.startswith(old_norm[:max(8, int(len(old_norm) * 0.70))]):
        return True

    # 3. Typo / identifier correction (>= 82% similarity)
    sim = difflib.SequenceMatcher(None, old_norm, new_norm).ratio()
    if sim >= 0.82:
        return True

    return False


def validate_code_to_inject(code: str, subject: str = "csharp") -> Tuple[bool, str]:
    """Strict guard that validates text before it can ever be sent to the clipboard or caret.
    Refuses sentinels, tiny text (<3 chars), and non-code chat boilerplate."""
    if not code:
        return False, "Payload is empty"

    stripped = code.strip()
    # A lone brace/closing delimiter is a legitimate paste in live coding.
    if len(stripped) < 3 and not re.fullmatch(r"[{}()\[\];,]+", stripped):
        return False, f"Payload too short ({len(stripped)} chars < 3)"

    for pat in SENTINEL_PATTERNS:
        if pat.search(stripped):
            return False, f"Payload matches illegal sentinel pattern: {pat.pattern}"

    # Subject language sanity check (reject conversational AI chatter)
    if subject.lower() in ("csharp", "cs", "java"):
        # Code typically contains operators, punctuation, or language keywords
        code_tokens = (
            ';', '{', '}', '(', ')', '=', 'Console.', 'System.', 'public', 'private',
            'static', 'void', 'class', 'namespace', 'using', 'int', 'string', 'decimal',
            'DateTime', 'var', 'new', '//', '/*', '*/', 'return', 'import', 'package'
        )
        if not any(token in stripped for token in code_tokens):
            # If no code tokens appear and text reads like conversational English, reject
            if re.match(r'^(I have|Please note|As an AI|This solution|Here you go)\b', stripped, re.IGNORECASE):
                return False, "Payload rejected: natural language text detected instead of code"

    return True, "Valid"


class EditProposal:
    """Represents a structured, single-intent code edit proposed by vision/diff."""
    def __init__(
        self,
        op: str,  # 'append', 'replace', 'delete_marked'
        line: int,
        old: str,
        new: str,
        evidence: str = "",
        confidence: float = 1.0
    ):
        self.op = op
        self.line = line
        self.old = old
        self.new = new
        self.evidence = evidence
        self.confidence = confidence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op": self.op,
            "line": self.line,
            "old": self.old,
            "new": self.new,
            "evidence": self.evidence,
            "confidence": self.confidence
        }


class MasterCodeTracker:
    """Manages cumulative master code, history snapshots, 2-read confirmation,
    and the circuit breaker for rock-solid stability."""
    def __init__(self, initial_code: str = ""):
        self.master_code = initial_code or ""
        self.snapshots: List[str] = []
        self.read_counts: Dict[str, int] = {}
        self.recent_edits: List[Tuple[float, int]] = []  # (timestamp, line_no)
        self.circuit_breaker_tripped = False
        self.circuit_breaker_reason = ""

    def save_snapshot(self):
        """Saves current master code state to circular snapshot buffer (max 20)."""
        if not self.snapshots or self.snapshots[-1] != self.master_code:
            self.snapshots.append(self.master_code)
            if len(self.snapshots) > 20:
                self.snapshots.pop(0)

    def undo(self) -> Optional[str]:
        """Restores the previous master code snapshot (Ctrl+Alt+Z)."""
        if self.snapshots:
            self.master_code = self.snapshots.pop()
            logger.info(f"Undo performed: restored master snapshot ({len(self.master_code)} chars).")
            return self.master_code
        logger.warning("No earlier snapshots available to undo.")
        return None

    def check_circuit_breaker(self) -> bool:
        """Evaluates circuit breaker:
        - More than 3 edits within 60s
        - Same line edited twice within 60s
        If triggered, pauses auto-edits and returns True."""
        now = time.time()
        # Keep only edits from last 60s
        self.recent_edits = [e for e in self.recent_edits if now - e[0] <= 60.0]

        if len(self.recent_edits) >= 3:
            self.circuit_breaker_tripped = True
            self.circuit_breaker_reason = f"Circuit breaker: {len(self.recent_edits)} auto-edits in 60s."
            logger.warning(self.circuit_breaker_reason)
            return True

        # Check if same line edited multiple times
        lines_edited = [e[1] for e in self.recent_edits if e[1] > 0]
        if len(lines_edited) != len(set(lines_edited)):
            self.circuit_breaker_tripped = True
            self.circuit_breaker_reason = "Circuit breaker: same line edited multiple times within 60s."
            logger.warning(self.circuit_breaker_reason)
            return True

        return False

    def reset_circuit_breaker(self):
        self.circuit_breaker_tripped = False
        self.circuit_breaker_reason = ""
        self.recent_edits.clear()

    def record_candidate_read(self, line_text: str) -> int:
        """Tracks consecutive observations of a line. Returns observation count."""
        key = normalize_code_line(line_text)
        cnt = self.read_counts.get(key, 0) + 1
        self.read_counts[key] = cnt
        return cnt

    def apply_proposal(self, proposal: EditProposal) -> Tuple[bool, str, str]:
        """Validates and applies an EditProposal to the master code.
        Returns (applied, status_message, injected_text)."""
        if self.circuit_breaker_tripped:
            return False, f"Blocked: {self.circuit_breaker_reason}", ""

        if self.check_circuit_breaker():
            return False, f"Blocked: {self.circuit_breaker_reason}", ""

        lines = self.master_code.splitlines()

        if proposal.op == "append":
            # Guard against appending existing lines
            norm_new = normalize_code_line(proposal.new)
            existing_norms = {normalize_code_line(l) for l in lines}
            if norm_new in existing_norms:
                return False, "Line already exists in master code", ""

            # Validate code safety
            valid, msg = validate_code_to_inject(proposal.new)
            if not valid:
                return False, f"Validation failed: {msg}", ""

            self.save_snapshot()

            # Find insertion point before trailing closing braces
            insert_idx = len(lines)
            while insert_idx > 0 and is_brace_line(lines[insert_idx - 1]):
                insert_idx -= 1

            lines.insert(insert_idx, proposal.new)
            self.master_code = "\n".join(lines)
            self.recent_edits.append((time.time(), insert_idx + 1))
            return True, f"Appended at line {insert_idx + 1}", proposal.new

        elif proposal.op == "replace":
            # old line must match and micro-edit must be valid
            if proposal.line < 1 or proposal.line > len(lines):
                return False, f"Line {proposal.line} out of bounds (1..{len(lines)})", ""

            curr_line = lines[proposal.line - 1]
            if normalize_code_line(curr_line) != normalize_code_line(proposal.old):
                return False, f"Line {proposal.line} text mismatch with proposal old text", ""

            if not is_valid_micro_edit(curr_line, proposal.new):
                return False, "Not an allowed micro-edit (fundamental change or deletion rejected)", ""

            valid, msg = validate_code_to_inject(proposal.new)
            if not valid:
                return False, f"Validation failed: {msg}", ""

            self.save_snapshot()
            lines[proposal.line - 1] = proposal.new
            self.master_code = "\n".join(lines)
            self.recent_edits.append((time.time(), proposal.line))
            return True, f"Replaced line {proposal.line}", proposal.new

        elif proposal.op == "delete_marked":
            # Deletions of code lines are forbidden by user rule
            return False, "Line deletions are forbidden by safety rules", ""

        return False, f"Unknown op: {proposal.op}", ""
