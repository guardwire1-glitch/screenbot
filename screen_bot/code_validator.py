"""
Code Validator (Pipeline Stage 5):
Deterministic Python validation gates.
- normalize_for_diff(): used ONLY for diff comparison; never written to master or IDE.
- LineConsensusTracker: a line must be observed in consecutive frames before admission.
- Rejection of unreadable tokens, ScreenBot sentinels, and browser chrome.
- Deterministic bracket/string validation for all supported languages.

Language-specific syntax checking lives in syntax_gate.py (tree-sitter). The
scanner here is deliberately conservative: it only rejects delimiters that are
provably unbalanced, so valid code is never blocked by a false positive.
"""

import re
from typing import Dict, List, Optional, Tuple

try:
    from screen_bot.ai_vision_extractor import ExtractedLine, UNREADABLE_TOKEN
except ModuleNotFoundError:
    from ai_vision_extractor import ExtractedLine, UNREADABLE_TOKEN

# These are ScreenBot/pipeline artefacts, not source code. They must never be
# injected into an editor. Ordinary keywords such as null/undefined/NaN are
# valid code and are deliberately NOT listed here.
SENTINEL_PATTERNS = [
    r"_SB_[A-Z0-9_]+_",
    r"Micro-edits applied",
    r"\[ScreenBot",
    r"\[REDACTED_API_KEY\]",
    r"^```[\w+-]*\s*$",
    r"^Here is (the|your) (solution|code)",
    r"^Certainly[!,]",
]

_BROWSER_CHROME = re.compile(
    r"^(https?://|www\.|meet\.google\.com|teams\.(microsoft|live)\.com|"
    r"youtube\.com/watch|classroom\.google\.com)",
    re.IGNORECASE,
)
_CODEISH = re.compile(r"[;{}()\[\]=<>]|\b(class|void|return|import|using|def|public)\b")


def normalize_for_diff(text: str) -> str:
    """Normalizes text for comparison only. Original formatting is never written."""
    return re.sub(r"\s+", " ", text.strip())


def is_valid_code_line(line: ExtractedLine, min_confidence: Optional[float] = None) -> bool:
    """Rejects placeholders, pipeline sentinels, and browser chrome.
    Confidence is only enforced when the extractor actually provided one;
    a missing confidence is not silently treated as certainty."""
    text = getattr(line, "text", "") or ""
    if not text.strip():
        return True

    if UNREADABLE_TOKEN in text:
        return False

    stripped = text.strip()
    for pat in SENTINEL_PATTERNS:
        if re.search(pat, stripped, re.IGNORECASE):
            return False

    # A bare URL is browser chrome; a URL inside a string literal is code.
    if _BROWSER_CHROME.match(stripped) and not _CODEISH.search(stripped):
        return False

    # Partial/low-confidence lines are not rejected here: they are held as
    # pending by LineConsensusTracker so a later clean frame can decide.
    conf = getattr(line, "confidence", None)
    if min_confidence is not None and conf is not None and conf < min_confidence:
        return False

    return True


def is_weak_line(text: str) -> bool:
    """Blank lines and lone punctuation carry no merge signal."""
    return re.fullmatch(r"[{}()\[\];,\s]*", text or "") is not None


_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CONTINUATION_TAIL = set("=+-*/%&|^!?:,.([{")
_DANGLING_WORD = re.compile(
    r"(?:^|\s)(?:if|for|while|else|switch|case|catch|return|throw|new|await|"
    r"yield|import|from|using|do|try|finally)$",
    re.IGNORECASE,
)
_CONTROL_HEADER = re.compile(
    r"^(?:else\s+)?(?:if|for|foreach|while|switch|catch|using|lock|fixed|"
    r"synchronized)\b",
    re.IGNORECASE,
)


def _header_without_body(text: str) -> bool:
    """True for a control header whose condition ends the line (`if (ready)`).

    The line is already known to be balanced, so the first paren group must
    start right after the keyword and close at the last non-space character;
    anything after it is a body and the statement may be complete.
    """
    match = _CONTROL_HEADER.match(text)
    if not match:
        return False
    rest = text[match.end():].lstrip()
    if not rest.startswith("("):
        return False

    depth = 0
    quote = ""
    escaped = False
    for pos, char in enumerate(rest):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'`":
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return not rest[pos + 1:].strip()
    return False


def looks_incomplete(text: str) -> bool:
    """A cheap, language-agnostic signal that a line is still being written.

    Used only for the bottom-most line on screen. A multi-line call's first
    line also looks locally incomplete, which is why the active-line hold is
    released as soon as another line appears below it or the user forces a scan.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.startswith(("//", "#", "--", "/*")):
        return False

    stack: List[str] = []
    quote = ""
    escaped = False
    idx = 0
    while idx < len(stripped):
        char = stripped[idx]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            idx += 1
            continue
        if stripped.startswith("//", idx):
            break
        if char in "\"'`":
            quote = char
            idx += 1
            continue
        if char in _OPENERS:
            stack.append(_OPENERS[char])
        elif char in ")]}":
            if stack and stack.pop() != char:
                return True
        idx += 1

    if quote or stack:
        return True
    if stripped[-1] in _CONTINUATION_TAIL or stripped.endswith("\\"):
        return True
    if _header_without_body(stripped):
        return True
    return _DANGLING_WORD.search(stripped) is not None


class LineConsensusTracker:
    """Tracks consecutive observations across frames. A line is admitted only
    after it has been seen in `required_observations` consecutive frames with
    identical normalized text. A line seen twice inside one frame counts once.

    `hold_active_line` adds an explicit, opt-in hold for the bottom-most line:
    that is where a lecturer's caret is in a live-coding session. A balanced
    bottom line waits `active_line_hold_frames` extra clean frames; an
    incomplete one is withheld until it stops being the bottom line or the user
    forces a sync (`hold_active_line=False`). Closers/blank lines are exempt so
    the end of a block is never withheld.
    """

    def __init__(self, required_observations: int = 2, min_confidence: float = 0.85,
                 active_line_hold_frames: int = 2):
        self.required_observations = max(1, int(required_observations))
        self.min_confidence = min_confidence
        self.active_line_hold_frames = max(0, int(active_line_hold_frames))
        self._history: Dict[Tuple[str, int], Dict] = {}
        self._frame_counter = 0
        self._last_pending_count = 0

    @property
    def has_pending(self) -> bool:
        """True when the last frame left lines that a second look could accept.
        The live path uses this to keep scanning instead of skipping a static
        screen after a single observation."""
        return self._last_pending_count > 0

    def feed_frame_lines(
        self, lines: List[ExtractedLine], hold_active_line: bool = False
    ) -> Tuple[List[ExtractedLine], List[ExtractedLine]]:
        self._frame_counter += 1
        accepted: List[ExtractedLine] = []
        pending: List[ExtractedLine] = []
        frame_occ: Dict[str, int] = {}

        active_idx = None
        if hold_active_line:
            for idx, line in enumerate(lines):
                if is_valid_code_line(line):
                    active_idx = idx
            if active_idx is not None:
                active_text = getattr(lines[active_idx], "text", "") or ""
                if not normalize_for_diff(active_text) or is_weak_line(active_text):
                    active_idx = None

        for idx, line in enumerate(lines):
            if not is_valid_code_line(line):
                continue

            conf = getattr(line, "confidence", None)
            visible = getattr(line, "visible", "full")
            low_signal = (
                (conf is not None and conf < self.min_confidence)
                or visible not in (None, "full")
            )
            if low_signal:
                # Never admit, never count: an uncertain or partially covered
                # line must be seen cleanly in consecutive frames first.
                pending.append(line)
                continue

            norm = normalize_for_diff(line.text)
            occ = frame_occ.get(norm, 0)
            frame_occ[norm] = occ + 1
            key = (norm, occ)

            entry = self._history.get(key)
            if entry and self._frame_counter - entry["last_seen_frame"] == 1:
                entry["count"] += 1
                entry["last_seen_frame"] = self._frame_counter
                entry["last_raw"] = line
                entry["blank"] = not norm
            else:
                entry = {
                    "count": 1,
                    "last_seen_frame": self._frame_counter,
                    "last_raw": line,
                    "blank": not norm,
                }
                self._history[key] = entry

            # Blank separator lines still need two sightings: a single blank row
            # may just be an OCR gap, not a real blank line in the file.
            needs = self.required_observations
            hold_forever = False
            if idx == active_idx:
                text = getattr(line, "text", "") or ""
                if looks_incomplete(text):
                    # Never write a half-typed caret line automatically. It is
                    # released when it stops being the bottom line (the next
                    # line was typed) or when the user forces a sync.
                    hold_forever = True
                else:
                    needs = max(needs, self.required_observations + self.active_line_hold_frames)
            if not hold_forever and entry["count"] >= needs:
                accepted.append(line)
            else:
                pending.append(line)

        dead = [k for k, v in self._history.items() if self._frame_counter - v["last_seen_frame"] > 4]
        for key in dead:
            del self._history[key]

        self._last_pending_count = len(pending)
        return accepted, pending

    def reset(self) -> None:
        self._history.clear()
        self._frame_counter = 0
        self._last_pending_count = 0


_STRING_STARTS = {'"', "'", "`"}


def _language_comment_style(language: str) -> Tuple[str, bool]:
    lang = (language or "").lower()
    hash_comment = lang in {"python", "py", "ruby", "shell", "bash", "powershell", "r"}
    dash_comment = lang in {"sql", "plsql", "tsql", "mysql", "postgresql"}
    return ("hash" if hash_comment else "dash" if dash_comment else "slash"), hash_comment


def validate_syntax(code: str, language: str = "general") -> Tuple[bool, str]:
    """Deterministic delimiter/string validation.

    Handles // and /* */ comments, C# verbatim and interpolated strings,
    Python triple-quoted strings, shell/Python # comments and SQL -- comments.
    Delimiters inside strings/comments are ignored. It cannot prove that code
    compiles; it only rejects provably broken text before it reaches an editor.
    """
    if not code or not code.strip():
        return True, ""

    style, _ = _language_comment_style(language)
    lang = (language or "").lower()
    triple = lang in {"python", "py"}
    verbatim_ok = lang in {"csharp", "cs"}

    stack: List[Tuple[str, int]] = []
    pairs = {"{": "}", "(": ")", "[": "]"}
    closers = {v: k for k, v in pairs.items()}
    i, n = 0, len(code)
    line = 1
    quote: Optional[str] = None
    triple_quote: Optional[str] = None
    in_block_comment = False
    at_line_start = True

    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""

        if ch == "\n":
            line += 1
            at_line_start = True
            if quote in ("'", '"') and not verbatim_ok:
                # C-like single/double quoted strings do not span raw newlines.
                return False, f"Unterminated string literal starting on line {line - 1}"
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if triple_quote:
            if code.startswith(triple_quote, i):
                i += 3
                triple_quote = None
                quote = None
                continue
            if ch == "\\":
                i += 2
                continue
            i += 1
            continue

        if quote:
            if ch == "\\" and not (verbatim_ok and quote == '"'):
                i += 2
                continue
            if verbatim_ok and quote == '"' and ch == '"' and nxt == '"':
                i += 2  # "" escape inside a verbatim C# string
                continue
            if ch == quote:
                quote = None
            i += 1
            continue

        # Comments (only when not inside a string)
        if style == "slash" and ch == "/" and nxt == "/":
            while i < n and code[i] != "\n":
                i += 1
            continue
        if style == "slash" and ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if style == "hash" and ch == "#" and (at_line_start or code[i - 1:i].isspace()):
            while i < n and code[i] != "\n":
                i += 1
            continue
        if style == "dash" and ch == "-" and nxt == "-":
            while i < n and code[i] != "\n":
                i += 1
            continue

        if triple and (code.startswith('"""', i) or code.startswith("'''", i)):
            triple_quote = code[i:i + 3]
            quote = triple_quote[0]
            i += 3
            continue

        if ch in _STRING_STARTS:
            quote = ch
            i += 1
            continue

        if ch in pairs:
            stack.append((ch, line))
        elif ch in closers:
            if not stack:
                return False, f"Unmatched closing bracket '{ch}' on line {line}"
            opening, opened_line = stack.pop()
            if pairs[opening] != ch:
                return False, (
                    f"Mismatched bracket '{opening}' from line {opened_line} "
                    f"closed with '{ch}' on line {line}"
                )

        at_line_start = False
        i += 1

    if triple_quote:
        return False, "Unterminated triple-quoted string literal"
    if quote:
        return False, "Unterminated string literal"
    if in_block_comment:
        return False, "Unterminated multi-line comment"
    if stack:
        unclosed, opened_line = stack[-1]
        return False, f"Unbalanced delimiter '{unclosed}' opened on line {opened_line}"

    return True, ""
