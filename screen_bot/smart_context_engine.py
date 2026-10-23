"""
smart_context_engine.py
Context-aware AST/Scope merger, syntax healer, and pro-indenter for ScreenBot.
Replaces blind cursor-pasting with full-buffer scope-aware synchronization.
"""

from __future__ import annotations
import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 1. OCR & HALLUCINATION SANITIZER
# ---------------------------------------------------------------------------

SENTINELS = (
    "_SB_NO_SELECTION_",
    "Micro-edits applied",
    "[ScreenBot]",
    "<<<UNREADABLE>>>",
    "UNREADABLE",
    "[PARTIAL]",
)

_FENCE_RE = re.compile(r"^```[\w#+.-]*\s*$|^```\s*$")
# Matches accidental OCR line numbers at start of line, e.g. "12 | public void..." or "14   int x = 1;"
_LINE_NUM_PREFIX_RE = re.compile(r"^\s*\d{1,4}\s*(?:\||:)\s*")

_QUOTE_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u00a0": " ", "\u200b": "",
})

CTRL_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "else", "return",
    "new", "try", "do", "throw", "case", "using", "lock",
    "foreach", "synchronized", "fixed", "checked", "unchecked",
}

TYPE_DECL_RE = re.compile(
    r"(?:^|[\s])(namespace|class|interface|enum|record|struct)\s+([A-Za-z_]\w*)"
)

METHOD_DECL_RE = re.compile(
    r"^(?:(?:public|private|protected|internal|static|final|virtual|override|abstract|async|synchronized|native)\s+)*"
    r"(?:[\w<>\[\],.?]+\s+)+([A-Za-z_]\w*)\s*\(([^()]*)\)\s*(?:throws\s+[\w.,\s]+)?\s*\{?$"
)


def norm(line: str) -> str:
    """Normalize a line for fuzzy comparison (ignores whitespace & smart quotes)."""
    cleaned, _ = strip_comments_and_strings(line, False, keep_literals=True)
    return re.sub(r"\s+", "", cleaned.translate(_QUOTE_MAP)).strip()


def is_structural_only(line: str) -> bool:
    """True if line is only braces, semicolons, or whitespace."""
    return re.fullmatch(r"[{}()\[\];,\s]*", line.strip()) is not None


def sanitize_vision_output(raw_text: str) -> List[str]:
    """
    Cleans raw LLM / OCR output:
    - Removes markdown fences
    - Rejects sentinel strings or hallucinated commentary
    - Normalizes smart quotes and zero-width spaces
    - Strips accidental line-number prefixes ("14: ")
    """
    if not raw_text:
        return []

    clean_lines: List[str] = []
    for raw_line in raw_text.translate(_QUOTE_MAP).splitlines():
        s = raw_line.strip()
        if _FENCE_RE.match(s):
            continue
        if any(bad in raw_line for bad in SENTINELS):
            continue
        # Skip conversational LLM hallucinations
        if s.lower().startswith(("here is the", "sure,", "note:", "explanation:")):
            continue
        line = _LINE_NUM_PREFIX_RE.sub("", raw_line.rstrip())
        clean_lines.append(line)

    # Trim leading/trailing blank lines
    while clean_lines and not clean_lines[0].strip():
        clean_lines.pop(0)
    while clean_lines and not clean_lines[-1].strip():
        clean_lines.pop()

    return clean_lines


# ---------------------------------------------------------------------------
# 2. MULTI-DEPTH SCOPE & ENTITY PARSER (C#, Java, C++, JS/TS)
# ---------------------------------------------------------------------------

def strip_comments_and_strings(
    line: str, in_block_comment: bool, keep_literals: bool = False
) -> Tuple[str, bool]:
    """Strips comments (and optionally string contents) so braces inside strings don't corrupt scope depth."""
    out: List[str] = []
    i, n = 0, len(line)
    quote_char: Optional[str] = None

    while i < n:
        ch = line[i]
        if in_block_comment:
            if line.startswith("*/", i):
                in_block_comment = False
                i += 2
            else:
                i += 1
        elif quote_char:
            if ch == "\\":
                if keep_literals:
                    out.append(line[i : i + 2])
                i += 2
                continue
            if ch == quote_char:
                quote_char = None
                out.append(ch)
            elif keep_literals:
                out.append(ch)
            i += 1
        elif line.startswith("//", i):
            break
        elif line.startswith("/*", i):
            in_block_comment = True
            i += 2
        else:
            if ch in ('"', "'"):
                quote_char = ch
            out.append(ch)
            i += 1

    return "".join(out), in_block_comment


@dataclass
class ScopeBlock:
    kind: str          # "namespace", "class", "method"
    name: str          # e.g. "Main" or "add(int,int)"
    full_key: str      # e.g. "class:Main::method:add(int,int)"
    start_line: int    # 0-indexed declaration line
    open_brace_line: int
    end_line: int      # 0-indexed closing brace line
    depth: int         # brace depth at declaration


def _extract_signature(code_line: str) -> Optional[Tuple[str, str]]:
    """Returns (kind, canonical_name) if the line declares a namespace, type, or method."""
    s = code_line.strip()
    if not s or s.startswith(("//", "/*", "*", "#", "@", "[")):
        return None

    m_type = TYPE_DECL_RE.search(s)
    if m_type:
        kind = "namespace" if m_type.group(1) == "namespace" else "class"
        return kind, m_type.group(2)

    # Check method declaration
    first_word = s.split("(")[0].split()[:1]
    if first_word and first_word[0] in CTRL_KEYWORDS:
        return None
    if s.endswith(";") and "{" not in s:
        return None

    m_func = METHOD_DECL_RE.match(s)
    if not m_func:
        return None

    func_name = m_func.group(1)
    if func_name in CTRL_KEYWORDS:
        return None

    # Canonicalize parameter types (strip parameter variable names so "add(int a, int b)" == "add(int x, int y)")
    raw_params = [p.strip() for p in m_func.group(2).split(",") if p.strip()]
    param_types = []
    for p in raw_params:
        tokens = p.split()
        param_types.append(" ".join(tokens[:-1]) if len(tokens) > 1 else tokens[0])
    return "method", f"{func_name}({','.join(param_types)})"


def parse_scopes(lines: List[str]) -> List[ScopeBlock]:
    """
    Finds all complete namespace, class, and method blocks at ANY nesting depth.
    Fixes the column-0 limitation so methods inside classes are matched by identity.
    """
    blocks: List[ScopeBlock] = []
    open_stack: List[dict] = []
    pending_decl: Optional[dict] = None
    depth = 0
    in_blk = False

    for idx, raw in enumerate(lines):
        code, in_blk = strip_comments_and_strings(raw, in_blk, keep_literals=False)
        s = code.strip()
        if not s:
            continue

        sig = _extract_signature(s)
        if sig:
            kind, name = sig
            parent_prefix = "::".join(item["key"] for item in open_stack)
            full_key = f"{parent_prefix}::{kind}:{name}" if parent_prefix else f"{kind}:{name}"
            pending_decl = {
                "kind": kind,
                "name": name,
                "key": f"{kind}:{name}",
                "full_key": full_key,
                "start_line": idx,
                "depth": depth,
            }

        for ch in s:
            if ch == "{":
                if pending_decl and depth == pending_decl["depth"]:
                    pending_decl["open_brace_line"] = idx
                    open_stack.append(pending_decl)
                    pending_decl = None
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
                if open_stack and depth == open_stack[-1]["depth"]:
                    finished = open_stack.pop()
                    blocks.append(
                        ScopeBlock(
                            kind=finished["kind"],
                            name=finished["name"],
                            full_key=finished["full_key"],
                            start_line=finished["start_line"],
                            open_brace_line=finished["open_brace_line"],
                            end_line=idx,
                            depth=finished["depth"],
                        )
                    )

        # Clear pending declaration if a semicolon was hit before an opening brace
        if pending_decl and ";" in s and "{" not in s:
            pending_decl = None

    return blocks


# ---------------------------------------------------------------------------
# 3. SYNTAX VALIDATOR, HEALER & PRO INDENTER
# ---------------------------------------------------------------------------

@dataclass
class SyntaxReport:
    unbalanced_braces: int      # >0 means unclosed '{', <0 means extra '}'
    unbalanced_parens: int
    unbalanced_brackets: int
    unclosed_string_lines: List[int]
    doubled_punct_lines: List[int]

    @property
    def total_errors(self) -> int:
        return (
            abs(self.unbalanced_braces)
            + abs(self.unbalanced_parens)
            + abs(self.unbalanced_brackets)
            + len(self.unclosed_string_lines)
            + len(self.doubled_punct_lines)
        )


def analyze_syntax(lines: List[str]) -> SyntaxReport:
    """Deterministic structural syntax check across the buffer."""
    braces = parens = brackets = 0
    unclosed_strings: List[int] = []
    doubled_punct: List[int] = []
    in_blk = False

    for idx, raw in enumerate(lines):
        # Check for unclosed string literal on a single line (ignoring C# verbatim @"")
        stripped = raw.strip()
        if not in_blk and not stripped.startswith("//"):
            q = None
            for i, ch in enumerate(raw):
                if ch == "\\" and q:
                    continue
                if ch in ('"', "'"):
                    if q is None:
                        q = ch
                    elif q == ch:
                        q = None
            if q is not None and not stripped.startswith('@"'):
                unclosed_strings.append(idx)

        code, in_blk = strip_comments_and_strings(raw, in_blk, keep_literals=False)
        for ch in code:
            if ch == "{":
                braces += 1
            elif ch == "}":
                braces -= 1
            elif ch == "(":
                parens += 1
            elif ch == ")":
                parens -= 1
            elif ch == "[":
                brackets += 1
            elif ch == "]":
                brackets -= 1

        # Detect IDE auto-complete / typing collisions like ";;" (outside for-loops)
        if ";;" in code and not re.search(r"\bfor\s*\(", code):
            doubled_punct.append(idx)

    return SyntaxReport(
        unbalanced_braces=braces,
        unbalanced_parens=parens,
        unbalanced_brackets=brackets,
        unclosed_string_lines=unclosed_strings,
        doubled_punct_lines=doubled_punct,
    )


def detect_indent_style(lines: List[str]) -> str:
    """Detects whether the existing file uses tabs, 2 spaces, or 4 spaces."""
    tabs = 0
    space_widths: List[int] = []
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("\t"):
            tabs += 1
        elif line.startswith(" "):
            width = len(line) - len(line.lstrip(" "))
            if 0 < width <= 16:
                space_widths.append(width)

    if tabs > len(space_widths):
        return "\t"
    if space_widths:
        twos = sum(1 for w in space_widths if w % 4 != 0 and w % 2 == 0)
        if twos > len(space_widths) // 2:
            return "  "
    return "    "


def heal_and_format(lines: List[str], reference_lines: Optional[List[str]] = None) -> List[str]:
    """
    1. Repairs common syntax errors (double semicolons, missing closing braces at EOF).
    2. Re-indents the entire file like a senior developer based on lexical scope depth.
    """
    indent_unit = detect_indent_style(reference_lines or lines)
    healed: List[str] = []
    in_blk = False

    # Pass 1: Line-level syntax repairs
    for raw in lines:
        code, in_blk = strip_comments_and_strings(raw, in_blk, keep_literals=False)
        fixed = raw.rstrip()
        # Fix accidental double semicolons outside `for (;;)`
        if ";;" in code and not re.search(r"\bfor\s*\(", code):
            fixed = re.sub(r";\s*;+", ";", fixed)
        healed.append(fixed)

    # Pass 2: Balance missing closing braces at EOF if safe (up to 3 missing braces)
    report = analyze_syntax(healed)
    if 0 < report.unbalanced_braces <= 3:
        for _ in range(report.unbalanced_braces):
            healed.append("}")

    # Pass 3: Scope-based pro indentation
    formatted: List[str] = []
    depth = 0
    in_blk = False
    pending_single_line_indent = False

    for raw in healed:
        s = raw.strip()
        if not s:
            formatted.append("")
            continue

        code, in_blk = strip_comments_and_strings(s, in_blk, keep_literals=False)
        clean_s = code.strip()

        # Leading closing braces shift left before indenting this line
        leading_closes = 0
        for ch in clean_s:
            if ch == "}":
                leading_closes += 1
            else:
                break

        effective_depth = max(0, depth - leading_closes)
        # Switch case/default labels sit 1 level inside switch
        if re.match(r"^(case\s+.+|default)\s*:", clean_s):
            effective_depth = max(0, effective_depth - 1)

        extra = 1 if (pending_single_line_indent and not clean_s.startswith("{")) else 0
        formatted.append(f"{indent_unit * (effective_depth + extra)}{s}")

        # Update brace depth for subsequent lines
        net_change = 0
        for i, ch in enumerate(clean_s):
            if ch == "{":
                net_change += 1
            elif ch == "}":
                if i >= leading_closes:
                    net_change -= 1

        depth = max(0, effective_depth + net_change)

        # Track braceless control statements (e.g. `if (x > 0)` without `{` on same line)
        if re.match(r"^(if|for|foreach|while|else\b)\b", clean_s) and not clean_s.endswith((";", "{", "}")):
            pending_single_line_indent = True
        else:
            pending_single_line_indent = False

    return formatted


# ---------------------------------------------------------------------------
# 4. CONTEXT-AWARE MERGER (SOLVES EXISTING CODE & INNER-SCOPE INJECTION)
# ---------------------------------------------------------------------------

@dataclass
class SyncOutcome:
    changed: bool
    merged_text: str
    event: str                   # "noop", "updated_entities", "spliced_lines", "blocked_syntax"
    added_lines: List[str] = field(default_factory=list)
    repaired_errors: int = 0
    notes: List[str] = field(default_factory=list)


class SmartCodeSynchronizer:
    """
    Maintains synchronization between what exists in the user's IDE file
    and what appears on the lecturer's screen.
    """

    def __init__(self, require_confirmations: int = 2):
        self.require_confirmations = require_confirmations
        self._candidate_hash: Optional[str] = None
        self._candidate_count: int = 0
        self.snapshots: List[str] = []

    def sync(self, current_ide_text: str, raw_vision_text: str) -> SyncOutcome:
        """
        Takes the FULL text currently in the user's IDE file and the raw text extracted
        from the screen, merges them without repeating existing code, heals syntax,
        and formats indentation.
        """
        frame_lines = sanitize_vision_output(raw_vision_text)
        if not frame_lines:
            return SyncOutcome(False, current_ide_text, "noop", notes=["empty_or_filtered_frame"])

        # Stability confirmation gate (prevents single-frame OCR glitches)
        frame_sig = "\n".join(norm(x) for x in frame_lines if norm(x))
        if frame_sig == self._candidate_hash:
            self._candidate_count += 1
        else:
            self._candidate_hash = frame_sig
            self._candidate_count = 1

        if self._candidate_count < self.require_confirmations:
            return SyncOutcome(False, current_ide_text, "noop", notes=["awaiting_second_confirmation"])

        ide_lines = current_ide_text.splitlines() if current_ide_text.strip() else []

        # Case A: IDE buffer is empty -> format and heal frame directly
        if not ide_lines:
            healed = heal_and_format(frame_lines)
            new_text = "\n".join(healed)
            self._save_snapshot(current_ide_text)
            return SyncOutcome(True, new_text, "initial_seed", added_lines=healed)

        # Case B: Check if all substantive lines in frame already exist in the IDE file
        ide_norms = [norm(x) for x in ide_lines]
        frame_substantive = [x for x in frame_lines if not is_structural_only(x)]
        if frame_substantive and self._is_subsequence(
            [norm(x) for x in frame_substantive], ide_norms
        ):
            # Even if no new code was added, check if the user's file has syntax/indent issues we can heal
            before_err = analyze_syntax(ide_lines).total_errors
            if before_err > 0:
                healed_ide = heal_and_format(ide_lines, reference_lines=ide_lines)
                after_err = analyze_syntax(healed_ide).total_errors
                if after_err < before_err:
                    self._save_snapshot(current_ide_text)
                    return SyncOutcome(
                        True,
                        "\n".join(healed_ide),
                        "syntax_repair_only",
                        repaired_errors=before_err - after_err,
                    )
            return SyncOutcome(False, current_ide_text, "noop", notes=["already_in_file"])

        # Case C: Scope & Entity-Aware Merge (Methods & Classes at any depth)
        merged_lines, added = self._merge_by_scope_and_anchors(ide_lines, frame_lines)

        # Format and heal the merged result
        healed_merged = heal_and_format(merged_lines, reference_lines=ide_lines)

        # Syntax safety gate: never allow a merge that increases structural syntax errors
        before_report = analyze_syntax(ide_lines)
        after_report = analyze_syntax(healed_merged)

        if after_report.total_errors > before_report.total_errors:
            return SyncOutcome(
                False,
                current_ide_text,
                "blocked_syntax",
                notes=[f"errors_would_increase_{before_report.total_errors}_to_{after_report.total_errors}"],
            )

        new_text = "\n".join(healed_merged)
        if norm(new_text) == norm(current_ide_text) and new_text == current_ide_text:
            return SyncOutcome(False, current_ide_text, "noop")

        self._save_snapshot(current_ide_text)
        return SyncOutcome(
            changed=True,
            merged_text=new_text,
            event="merged_and_formatted",
            added_lines=added,
            repaired_errors=max(0, before_report.total_errors - after_report.total_errors),
        )

    def undo(self) -> Optional[str]:
        return self.snapshots.pop() if self.snapshots else None

    def _save_snapshot(self, text: str) -> None:
        if not self.snapshots or self.snapshots[-1] != text:
            self.snapshots.append(text)
            if len(self.snapshots) > 50:
                self.snapshots.pop(0)

    @staticmethod
    def _is_subsequence(small: List[str], big: List[str]) -> bool:
        """True if all normalized lines in `small` already appear in order inside `big`."""
        it = iter(big)
        return all(item in it for item in small if item)

    def _merge_by_scope_and_anchors(
        self, ide_lines: List[str], frame_lines: List[str]
    ) -> Tuple[List[str], List[str]]:
        """
        1. Matches methods and classes by name/signature at any depth.
           - If an entity exists in both, updates it in-place (preserving scrolled-off siblings).
           - If a new method appears inside an existing class, inserts it inside that class's closing brace.
        2. Falls back to anchored SequenceMatcher splicing for partial/scrolled viewports.
        """
        result = list(ide_lines)
        ide_scopes = {b.full_key: b for b in parse_scopes(result)}
        frame_scopes = parse_scopes(frame_lines)

        # Also map by short key ("method:add(int,int)") in case the class header scrolled off-screen
        ide_short_map: Dict[str, ScopeBlock] = {
            f"{b.kind}:{b.name}": b for b in ide_scopes.values()
        }

        added_lines: List[str] = []

        # If the frame contains complete methods or classes, merge them by identity
        if frame_scopes:
            # Sort innermost/bottom-most first so line indices stay valid during replacement
            handled_frame_indices = set()
            replacements: List[Tuple[int, int, List[str]]] = []

            for f_block in frame_scopes:
                # Skip outer class block if we can merge its inner methods more granularly,
                # UNLESS the entire class was rewritten from scratch
                short_key = f"{f_block.kind}:{f_block.name}"
                target_block = ide_scopes.get(f_block.full_key) or ide_short_map.get(short_key)

                f_slice = frame_lines[f_block.start_line : f_block.end_line + 1]

                if f_block.kind == "class" and target_block is not None:
                    # Check if inner methods exist; if so, let method-level or anchor merge handle it
                    inner_methods = [
                        m for m in frame_scopes
                        if m.kind == "method" and m.start_line > f_block.start_line and m.end_line < f_block.end_line
                    ]
                    if inner_methods:
                        continue

                if target_block is not None:
                    # Entity exists in IDE -> merge lines inside this entity so scrolled lines aren't lost
                    t_slice = result[target_block.start_line : target_block.end_line + 1]
                    merged_entity, newly_added = self._anchored_line_splice(t_slice, f_slice)
                    replacements.append((target_block.start_line, target_block.end_line + 1, merged_entity))
                    added_lines.extend(newly_added)
                    handled_frame_indices.update(range(f_block.start_line, f_block.end_line + 1))
                elif f_block.kind == "method":
                    # New method! Insert it right before the enclosing class's closing brace in the IDE
                    enclosing_classes = [b for b in ide_scopes.values() if b.kind == "class"]
                    if enclosing_classes:
                        insert_pos = enclosing_classes[-1].end_line
                        replacements.append((insert_pos, insert_pos, [""] + f_slice))
                        added_lines.extend(f_slice)
                        handled_frame_indices.update(range(f_block.start_line, f_block.end_line + 1))

            if replacements:
                # Deduplicate overlapping replacements (prefer more specific inner blocks)
                replacements.sort(key=lambda r: (r[1] - r[0], r[0]))
                accepted_reps: List[Tuple[int, int, List[str]]] = []
                occupied: List[Tuple[int, int]] = []
                for s, e, new_chunk in replacements:
                    if any(s < oe and e > os for os, oe in occupied):
                        continue
                    accepted_reps.append((s, e, new_chunk))
                    if e > s:
                        occupied.append((s, e))

                for s, e, new_chunk in sorted(accepted_reps, key=lambda r: r[0], reverse=True):
                    result[s:e] = new_chunk
                return result, added_lines

        # Fallback / Partial Viewport: Anchored line splice across the file
        return self._anchored_line_splice(result, frame_lines)

    def _anchored_line_splice(
        self, base_lines: List[str], incoming_lines: List[str]
    ) -> Tuple[List[str], List[str]]:
        """
        Aligns `incoming_lines` against `base_lines` using substantive anchors
        (never lone braces `{` or `}`). Inserts new lines at their exact context position
        and updates modified lines in-place without deleting scrolled-off code.
        """
        base_subst = [(i, norm(line)) for i, line in enumerate(base_lines) if not is_structural_only(line)]
        inc_subst = [(j, norm(line)) for j, line in enumerate(incoming_lines) if not is_structural_only(line)]

        if not inc_subst:
            return base_lines, []
        if not base_subst:
            return incoming_lines, list(incoming_lines)

        sm = difflib.SequenceMatcher(
            None,
            [n for _, n in base_subst],
            [n for _, n in inc_subst],
            autojunk=False,
        )
        matches = sm.get_matching_blocks()

        # Build anchor pairs: (base_line_idx, incoming_line_idx)
        anchors: List[Tuple[int, int]] = []
        for a, b, size in matches:
            for k in range(size):
                anchors.append((base_subst[a + k][0], inc_subst[b + k][0]))

        # If zero anchors matched, check if it's a new snippet to insert inside the deepest open block
        if not anchors:
            insert_idx = self._find_innermost_insertion_point(base_lines)
            out = base_lines[:insert_idx] + incoming_lines + base_lines[insert_idx:]
            return out, list(incoming_lines)

        out: List[str] = []
        added: List[str] = []

        first_base_idx, first_inc_idx = anchors[0]
        # Preserve scrolled-off top of base file
        out.extend(base_lines[:first_base_idx])
        # If incoming frame has new lines above the first anchor (and first_base_idx == 0), prepend them
        if first_base_idx == 0 and first_inc_idx > 0:
            leading_new = incoming_lines[:first_inc_idx]
            out.extend(leading_new)
            added.extend(leading_new)

        # Walk between matched anchors
        for idx in range(len(anchors)):
            b_curr, i_curr = anchors[idx]
            out.append(base_lines[b_curr])

            if idx + 1 < len(anchors):
                b_next, i_next = anchors[idx + 1]
                base_gap = base_lines[b_curr + 1 : b_next]
                inc_gap = incoming_lines[i_curr + 1 : i_next]

                if inc_gap:
                    # Incoming frame has code between these two matched anchors -> use incoming version
                    out.extend(inc_gap)
                    base_gap_norms = {norm(x) for x in base_gap}
                    added.extend([x for x in inc_gap if norm(x) not in base_gap_norms])
                else:
                    # Keep structural braces if any existed between adjacent anchors
                    out.extend(base_gap)

        # Handle lines after the last matched anchor
        last_base_idx, last_inc_idx = anchors[-1]
        trailing_inc = [
            x for x in incoming_lines[last_inc_idx + 1 :] if not is_structural_only(x)
        ]

        if trailing_inc:
            # Splice new trailing statements right after the last matched anchor in base
            inc_tail = incoming_lines[last_inc_idx + 1 :]
            # Strip trailing closing braces from inc_tail if base already has closing braces
            base_tail = base_lines[last_base_idx + 1 :]
            base_closing_count = sum(1 for x in base_tail if x.strip() in ("}", "};"))
            while base_closing_count > 0 and inc_tail and inc_tail[-1].strip() in ("}", "};"):
                inc_tail.pop()
                base_closing_count -= 1

            out.extend(inc_tail)
            added.extend(inc_tail)
            out.extend(base_tail)
        else:
            # Preserve scrolled-off bottom of base file
            out.extend(base_lines[last_base_idx + 1 :])

        return out, added

    @staticmethod
    def _find_innermost_insertion_point(lines: List[str]) -> int:
        """
        Finds the line right before the closing brace of the last method or class in the file,
        so new loose statements land INSIDE the active method/class instead of outside the file.
        """
        scopes = parse_scopes(lines)
        methods = [s for s in scopes if s.kind == "method"]
        if methods:
            return methods[-1].end_line
        classes = [s for s in scopes if s.kind == "class"]
        if classes:
            return classes[-1].end_line
        return len(lines)
