"""
smart_sync_engine.py
Production-grade Context-Aware Code Merger, AST Syntax Healer, Active File Resolver,
and Direct Disk Synchronizer for ScreenBot.
"""

from __future__ import annotations
import ctypes
import difflib
import hashlib
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# 0. WINDOWS DPI AWARENESS & HARDWARE KEY POLLING
# ---------------------------------------------------------------------------

if os.name == "nt":
    try:
        # Per-Monitor DPI Aware v2 (prevents blurry/offset captures on 125%/150% scaling)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def wait_for_physical_keys_released(timeout_sec: float = 0.6) -> None:
    """
    Polls Win32 GetAsyncKeyState to ensure physical Ctrl, Shift, Alt, and Win keys
    are completely released by the user before sending any synthetic keystrokes.
    Eliminates the stuck-modifier bug (fix_stuck_keys.ps1).
    """
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    # VK_SHIFT (0x10), VK_CONTROL (0x11), VK_MENU/ALT (0x12), VK_LWIN (0x5B), VK_RWIN (0x5C)
    vk_mods = (0x10, 0x11, 0x12, 0x5B, 0x5C)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in vk_mods):
            return
        time.sleep(0.015)


# Optional Tree-Sitter support for compiler-grade AST error coordinates
try:
    from tree_sitter import Language, Parser, Node
    import tree_sitter_c_sharp as ts_csharp
    import tree_sitter_java as ts_java
    import tree_sitter_python as ts_python
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False


# ---------------------------------------------------------------------------
# 1. LEXER UTILITIES, STRING-SAFE NORMALIZATION & SANITIZER
# ---------------------------------------------------------------------------

SENTINELS = (
    "_SB_NO_SELECTION_",
    "Micro-edits applied",
    "[ScreenBot]",
    "<<<UNREADABLE>>>",
    "UNREADABLE",
    "[PARTIAL]",
)

_FENCE_RE = re.compile(r"^\s*```[\w#+.-]*\s*$|^\s*```\s*$")
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
    "yield", "await",
}

IMPORT_RE = re.compile(
    r"^\s*(?:using\s+(?:static\s+)?[A-Za-z_][\w.]*\s*;|import\s+(?:static\s+)?[A-Za-z_][\w.*]*\s*;)\s*$"
)

TYPE_DECL_RE = re.compile(
    r"(?:^|[\s])(namespace|class|interface|enum|record|struct)\s+([A-Za-z_]\w*)"
)

# Matches classic methods, generic methods, constructors with : base()/this(), and expression-bodied methods
METHOD_DECL_RE = re.compile(
    r"^(?:(?:public|private|protected|internal|static|final|virtual|override|abstract|async|synchronized|native|new|partial)\s+)*"
    r"(?:[\w<>\[\],.?]+\s+)?([A-Za-z_]\w*)\s*(?:<[^>{};]+>)?\s*\(([^()]*)\)"
    r"(?:\s*:\s*(?:base|this)\s*\([^()]*\))?"
    r"(?:\s*where\s+[^\{;]+?)?"
    r"(?:\s*throws\s+[\w.,\s]+)?"
    r"\s*(?:\{|=>|$)"
)

# Matches C# properties: e.g., `public int Count { get; set; }` or `public string Name => _name;`
PROPERTY_DECL_RE = re.compile(
    r"^(?:(?:public|private|protected|internal|static|virtual|override|abstract|new|required)\s+)+"
    r"[\w<>\[\],.?]+\s+([A-Za-z_]\w*)\s*(?:\{\s*(?:get|set|init)\b|=>)"
)


def strip_comments_and_mask_strings(
    line: str, in_block_comment: bool, mask_strings: bool = True
) -> Tuple[str, bool]:
    """
    Strips // and /* */ comments.
    If `mask_strings=True`, replaces string literals with deterministic tokens `"__STR_<hash>__"`
    so braces inside strings never corrupt scope depth, AND string literal edits are never lost!
    """
    out: List[str] = []
    i, n = 0, len(line)
    quote_char: Optional[str] = None
    str_buf: List[str] = []

    while i < n:
        ch = line[i]
        if in_block_comment:
            if line.startswith("*/", i):
                in_block_comment = False
                i += 2
            else:
                i += 1
        elif quote_char:
            if ch == "\\" and i + 1 < n:
                str_buf.append(line[i : i + 2])
                i += 2
                continue
            if ch == quote_char:
                raw_lit = "".join(str_buf)
                if mask_strings:
                    h = hashlib.md5(raw_lit.encode("utf-8")).hexdigest()[:8]
                    out.append(f"{quote_char}__STR_{h}__{quote_char}")
                else:
                    out.append(f"{quote_char}{raw_lit}{quote_char}")
                quote_char = None
                str_buf = []
            else:
                str_buf.append(ch)
            i += 1
        elif line.startswith("//", i):
            break
        elif line.startswith("/*", i):
            in_block_comment = True
            i += 2
        else:
            if ch in ('"', "'"):
                quote_char = ch
                str_buf = []
            else:
                out.append(ch)
            i += 1

    # If line had an unclosed string literal, flush what we saw
    if quote_char is not None:
        raw_lit = "".join(str_buf)
        if mask_strings:
            h = hashlib.md5(raw_lit.encode("utf-8")).hexdigest()[:8]
            out.append(f"{quote_char}__STR_{h}__")
        else:
            out.append(f"{quote_char}{raw_lit}")

    return "".join(out), in_block_comment


def norm(line: str) -> str:
    """
    String-safe normalization:
    Collapses code whitespace while preserving exact string literal hashes.
    """
    s = line.translate(_QUOTE_MAP).strip()
    if not s:
        return ""
    masked, _ = strip_comments_and_mask_strings(s, False, mask_strings=True)
    return re.sub(r"\s+", "", masked).strip()


def is_weak_anchor(line: str) -> bool:
    """True if a line is only punctuation/braces/keywords too generic to anchor a diff."""
    n = norm(line)
    if not n:
        return True
    if re.fullmatch(r"[{}()\[\];,\s]*", n):
        return True
    if n in ("else", "else{", "}else{", "try", "try{", "finally", "finally{", "get;", "set;", "get;set;"):
        return True
    return False


def sanitize_vision_output(raw_text: str) -> List[str]:
    """Cleans raw LLM / OCR output and strips markdown fences, line numbers, and sentinels."""
    if not raw_text:
        return []
    out: List[str] = []
    for raw in raw_text.translate(_QUOTE_MAP).splitlines():
        s = raw.strip()
        if _FENCE_RE.match(s):
            continue
        if any(bad in raw for bad in SENTINELS):
            continue
        if s.lower().startswith(("here is the", "sure,", "note:", "explanation:")):
            continue
        cleaned = _LINE_NUM_PREFIX_RE.sub("", raw.rstrip())
        out.append(cleaned)

    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


# ---------------------------------------------------------------------------
# 2. MULTI-DEPTH AST/SCOPE ENTITY PARSER
# ---------------------------------------------------------------------------

@dataclass
class ScopeEntity:
    kind: str          # "namespace", "class", "method", "property"
    name: str          # e.g. "Main" or "Add(int,int)"
    full_key: str      # e.g. "namespace:App::class:Main::method:Add(int,int)"
    short_key: str     # e.g. "method:Add(int,int)"
    start_line: int    # 0-indexed declaration start
    end_line: int      # 0-indexed closing brace or semicolon line
    depth: int


def _extract_member_sig(clean_code: str) -> Optional[Tuple[str, str]]:
    s = clean_code.strip()
    if not s or s.startswith(("//", "/*", "*", "#", "@", "[")):
        return None
    if IMPORT_RE.match(s):
        return None

    m_type = TYPE_DECL_RE.search(s)
    if m_type:
        kind = "namespace" if m_type.group(1) == "namespace" else "class"
        return kind, m_type.group(2)

    m_prop = PROPERTY_DECL_RE.match(s)
    if m_prop:
        return "property", m_prop.group(1)

    first_word = s.split("(")[0].split()[:1]
    if first_word and first_word[0] in CTRL_KEYWORDS:
        return None
    # Reject plain method calls ending with `;` unless expression-bodied `=>`
    if s.endswith(";") and "=>" not in s and "{" not in s:
        return None

    m_meth = METHOD_DECL_RE.match(s)
    if not m_meth:
        return None

    name = m_meth.group(1)
    if name in CTRL_KEYWORDS:
        return None

    raw_params = [p.strip() for p in m_meth.group(2).split(",") if p.strip()]
    param_types = []
    for p in raw_params:
        # Remove default value `int x = 0` -> `int x`
        p_no_def = p.split("=")[0].strip()
        tokens = [t for t in p_no_def.split() if t not in ("ref", "out", "in", "params", "final", "this")]
        param_types.append(" ".join(tokens[:-1]) if len(tokens) > 1 else (tokens[0] if tokens else ""))
    return "method", f"{name}({','.join(param_types)})"


def parse_entities(lines: List[str]) -> List[ScopeEntity]:
    """
    Extracts complete namespaces, classes, methods, and properties at ANY brace depth,
    including single-line expression-bodied members (`=> ...;`) and auto-properties.
    """
    entities: List[ScopeEntity] = []
    stack: List[dict] = []
    pending: Optional[dict] = None
    depth = 0
    in_blk = False

    for idx, raw in enumerate(lines):
        masked, in_blk = strip_comments_and_mask_strings(raw, in_blk, mask_strings=True)
        s = masked.strip()
        if not s:
            continue

        sig = _extract_member_sig(s)
        if sig:
            kind, name = sig
            prefix = "::".join(item["short_key"] for item in stack)
            short_key = f"{kind}:{name}"
            full_key = f"{prefix}::{short_key}" if prefix else short_key

            # Expression-bodied single-line method/property ending in `;`
            if "=>" in s and s.endswith(";") and "{" not in s:
                entities.append(
                    ScopeEntity(
                        kind=kind,
                        name=name,
                        full_key=full_key,
                        short_key=short_key,
                        start_line=idx,
                        end_line=idx,
                        depth=depth,
                    )
                )
            else:
                pending = {
                    "kind": kind,
                    "name": name,
                    "short_key": short_key,
                    "full_key": full_key,
                    "start_line": idx,
                    "depth": depth,
                }

        for ch in s:
            if ch == "{":
                if pending and depth == pending["depth"]:
                    stack.append(pending)
                    pending = None
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
                if stack and depth == stack[-1]["depth"]:
                    fin = stack.pop()
                    entities.append(
                        ScopeEntity(
                            kind=fin["kind"],
                            name=fin["name"],
                            full_key=fin["full_key"],
                            short_key=fin["short_key"],
                            start_line=fin["start_line"],
                            end_line=idx,
                            depth=fin["depth"],
                        )
                    )

        if pending and ";" in s and "{" not in s:
            pending = None

    return entities


# ---------------------------------------------------------------------------
# 3. COMPILER-GRADE AST DIAGNOSTICS & DETERMINISTIC HEALER
# ---------------------------------------------------------------------------

@dataclass
class SyntaxDiagnostic:
    line: int
    column: int
    kind: str
    snippet: str
    suggestion: str


class ASTHealer:
    def __init__(self, default_lang: str = "csharp"):
        self.default_lang = default_lang
        self._parsers: Dict[str, "Parser"] = {}
        if HAS_TREE_SITTER:
            for name, fn in (
                ("csharp", ts_csharp.language),
                ("java", ts_java.language),
                ("python", ts_python.language),
            ):
                try:
                    self._parsers[name] = Parser(Language(fn()))
                except Exception:
                    pass

    def detect_language(self, code: str, filename: str = "") -> str:
        fn = (filename or "").lower()
        if fn.endswith(".cs"):
            return "csharp"
        if fn.endswith(".java"):
            return "java"
        if fn.endswith(".py"):
            return "python"
        if "using System" in code or "Console." in code or "namespace " in code:
            return "csharp"
        if "public static void main" in code or "System.out." in code:
            return "java"
        return self.default_lang

    def diagnose(self, code: str, lang: Optional[str] = None) -> List[SyntaxDiagnostic]:
        lang = lang or self.detect_language(code)
        lines = code.splitlines()
        diags: List[SyntaxDiagnostic] = []

        # 1. Tree-sitter concrete syntax tree errors
        if HAS_TREE_SITTER and lang in self._parsers:
            tree = self._parsers[lang].parse(code.encode("utf-8"))
            self._walk_tree_errors(tree.root_node, lines, diags)

        # 2. Structural brace/paren/string-literal checks
        braces = parens = brackets = 0
        in_blk = False
        for idx, raw in enumerate(lines):
            s = raw.strip()
            if not in_blk and not s.startswith("//"):
                q = None
                for i, ch in enumerate(raw):
                    if ch == "\\" and q:
                        continue
                    if ch in ('"', "'"):
                        q = ch if q is None else (None if q == ch else q)
                if q is not None and not s.startswith('@"'):
                    diags.append(
                        SyntaxDiagnostic(idx + 1, len(raw), "UNCLOSED_STRING", s, f"Close string literal {q}")
                    )

            masked, in_blk = strip_comments_and_mask_strings(raw, in_blk, mask_strings=True)
            for ch in masked:
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

            if lang in ("csharp", "java") and self._missing_semicolon(s):
                diags.append(SyntaxDiagnostic(idx + 1, len(raw), "MISSING_SEMICOLON", s, "Add trailing ';'"))

        if braces != 0:
            diags.append(
                SyntaxDiagnostic(
                    len(lines), 1, "UNBALANCED_BRACES", "", f"Brace imbalance: {braces:+d}"
                )
            )
        if parens != 0:
            diags.append(
                SyntaxDiagnostic(
                    len(lines), 1, "UNBALANCED_PARENS", "", f"Paren imbalance: {parens:+d}"
                )
            )
        return diags

    def _walk_tree_errors(
        self, node: "Node", lines: List[str], out: List[SyntaxDiagnostic]
    ) -> None:
        if node.is_missing or node.type == "ERROR":
            ln = node.start_point[0] + 1
            col = node.start_point[1] + 1
            snip = lines[ln - 1].strip() if 0 <= ln - 1 < len(lines) else ""
            kind = "MISSING_TOKEN" if node.is_missing else "AST_SYNTAX_ERROR"
            out.append(SyntaxDiagnostic(ln, col, kind, snip, f"Check syntax near line {ln}:{col}"))
        for child in node.children:
            self._walk_tree_errors(child, lines, out)

    def heal_and_indent(
        self, lines: List[str], reference_lines: Optional[List[str]] = None, lang: str = "csharp"
    ) -> Tuple[List[str], List[str]]:
        """
        Deterministically repairs mechanical syntax errors across the whole buffer
        and formats indentation according to the file's style.
        """
        repairs: List[str] = []
        indent_unit = self._detect_indent_unit(reference_lines or lines)
        healed: List[str] = []
        in_blk = False

        # Pass 1: Line-level deterministic repairs
        for idx, raw in enumerate(lines):
            ln = idx + 1
            masked, in_blk = strip_comments_and_mask_strings(raw, in_blk, mask_strings=True)
            s = raw.strip()

            # 1a. Collapse doubled semicolons outside for(;;)
            if ";;" in masked and not re.search(r"\bfor\s*\(", masked):
                raw = re.sub(r";\s*;+", ";", raw)
                repairs.append(f"Line {ln}: collapsed duplicate ';;'")
                s = raw.strip()

            # 1b. Fix extra closing paren on method header from IDE auto-close: `void Foo()) {`
            if re.search(r"\([^{};()]*\)\)\s*\{?$", masked) and masked.count(")") > masked.count("("):
                raw = re.sub(r"\)\)(\s*\{?)$", r")\1", raw)
                repairs.append(f"Line {ln}: removed extra ')'")
                s = raw.strip()

            # 1c. Append missing semicolon on unambiguous statements
            if lang in ("csharp", "java") and self._missing_semicolon(s):
                if "//" in raw:
                    code_pt, comm_pt = raw.split("//", 1)
                    raw = f"{code_pt.rstrip()}; // {comm_pt.strip()}"
                else:
                    raw = f"{raw.rstrip()};"
                repairs.append(f"Line {ln}: inserted missing ';'")

            healed.append(raw.rstrip())

        # Pass 2: Balance braces safely
        if lang in ("csharp", "java"):
            healed = self._balance_braces(healed, repairs)
            healed = self._reindent_brace_code(healed, indent_unit)

        return healed, repairs

    @staticmethod
    def _detect_indent_unit(lines: List[str]) -> str:
        tabs = 0
        spaces: List[int] = []
        for l in lines:
            if not l.strip():
                continue
            if l.startswith("\t"):
                tabs += 1
            elif l.startswith(" "):
                w = len(l) - len(l.lstrip(" "))
                if 0 < w <= 12:
                    spaces.append(w)
        if tabs > len(spaces):
            return "\t"
        if spaces and sum(1 for w in spaces if w % 4 != 0 and w % 2 == 0) > len(spaces) // 2:
            return "  "
        return "    "

    @staticmethod
    def _missing_semicolon(s: str) -> bool:
        if not s or s.startswith(("//", "/*", "*", "#", "[", "@")):
            return False
        if s.endswith((";", "{", "}", ",", ":", "\\")):
            return False
        if re.match(
            r"^(if|else|for|foreach|while|switch|try|catch|finally|namespace|class|interface|enum|struct|record)\b",
            s,
        ):
            return False
        if METHOD_DECL_RE.match(s) or PROPERTY_DECL_RE.match(s):
            return False
        if re.match(r"^(return|break|continue|throw|using)\b", s):
            return True
        if re.match(
            r"^(Console\.|System\.out\.|Debug\.|var\s+\w+\s*=|[A-Za-z_][\w<>\[\]]*\s+[A-Za-z_]\w*\s*=)",
            s,
        ):
            return True
        if re.search(r"(\+\+|--|\+=|-=|\*=|/=)\s*$", s):
            return True
        return False

    @staticmethod
    def _balance_braces(lines: List[str], repairs: List[str]) -> List[str]:
        out: List[str] = []
        depth = 0
        in_blk = False
        for idx, raw in enumerate(lines):
            masked, in_blk = strip_comments_and_mask_strings(raw, in_blk, mask_strings=True)
            clean = masked.strip()
            if clean == "}" and depth == 0:
                repairs.append(f"Line {idx + 1}: removed unmatched extra '}}'")
                continue
            for ch in clean:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth = max(0, depth - 1)
            out.append(raw)

        if 0 < depth <= 4:
            for _ in range(depth):
                out.append("}")
            repairs.append(f"EOF: closed {depth} unclosed '{{' block(s)")
        return out

    @staticmethod
    def _reindent_brace_code(lines: List[str], indent_unit: str) -> List[str]:
        formatted: List[str] = []
        depth = 0
        in_blk = False
        single_line_indent = False

        for raw in lines:
            s = raw.strip()
            if not s:
                formatted.append("")
                continue

            masked, in_blk = strip_comments_and_mask_strings(s, in_blk, mask_strings=True)
            clean = masked.strip()

            leading_closes = 0
            for ch in clean:
                if ch == "}":
                    leading_closes += 1
                else:
                    break

            eff_depth = max(0, depth - leading_closes)
            if re.match(r"^(case\s+.+|default)\s*:", clean):
                eff_depth = max(0, eff_depth - 1)

            extra = 1 if (single_line_indent and not clean.startswith("{")) else 0
            formatted.append(f"{indent_unit * (eff_depth + extra)}{s}")

            net = 0
            for i, ch in enumerate(clean):
                if ch == "{":
                    net += 1
                elif ch == "}":
                    if i >= leading_closes:
                        net -= 1
            depth = max(0, eff_depth + net)

            if re.match(r"^(if|for|foreach|while|else)\b", clean) and not clean.endswith((";", "{", "}")):
                single_line_indent = True
            else:
                single_line_indent = False

        return formatted


# ---------------------------------------------------------------------------
# 4. ACTIVE FILE RESOLVER & DIRECT DISK SYNCHRONIZER
# ---------------------------------------------------------------------------

class ActiveFileResolver:
    """
    Resolves the exact file on disk currently open in VS Code, Visual Studio,
    IntelliJ, Cursor, or Notepad++ without touching the clipboard.
    """

    TITLE_PATTERNS = [
        # VS Code / Cursor: "Program.cs - MyProject - Visual Studio Code"
        re.compile(r"^[●•*]?\s*(?P<file>[\w.\-]+?\.(?:cs|java|py|js|ts|cpp|c|h|html|css|sql))\s*[-—]\s*(?P<folder>[^-—]+?)\s*[-—]", re.I),
        # Visual Studio: "MyProject - Program.cs*" or "Program.cs - Microsoft Visual Studio"
        re.compile(r"(?P<file>[\w.\-]+?\.(?:cs|java|py|js|ts|cpp|c|h))\b", re.I),
    ]

    def __init__(self, search_roots: Optional[List[str | Path]] = None):
        self.explicit_target: Optional[Path] = None
        self.search_roots: List[Path] = []
        if search_roots:
            self.search_roots.extend(Path(p) for p in search_roots if Path(p).exists())

        # Add common developer directories automatically
        home = Path.home()
        for candidate in (
            Path.cwd(),
            home / "source" / "repos",
            home / "IdeaProjects",
            home / "PycharmProjects",
            home / "Desktop",
            home / "Documents",
            home / "Projects",
        ):
            if candidate.exists() and candidate not in self.search_roots:
                self.search_roots.append(candidate)

        self._cache: Dict[str, Path] = {}

    def pin_target_file(self, path: str | Path) -> Path:
        p = Path(path).resolve()
        self.explicit_target = p
        return p

    def get_foreground_window_title(self) -> str:
        if os.name != "nt":
            return ""
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def resolve_active_file(self, window_title: Optional[str] = None, hint_filename: Optional[str] = None) -> Optional[Path]:
        """
        1. Returns `explicit_target` if pinned by user.
        2. Parses active IDE window title (or `hint_filename` from Gemini tab extraction).
        3. Searches workspace roots for the most recently modified matching source file.
        """
        if self.explicit_target and self.explicit_target.is_file():
            return self.explicit_target

        title = window_title if window_title is not None else self.get_foreground_window_title()
        target_name = hint_filename
        folder_hint = None

        if title:
            # Check if full path is in window title (e.g. Notepad++ or Python IDLE)
            full_path_m = re.search(r"([A-Za-z]:[\\/][^\r\n\t*?\"<>|]+?\.(?:cs|java|py|js|ts|cpp|h))", title)
            if full_path_m:
                p = Path(full_path_m.group(1))
                if p.is_file():
                    return p

            for pat in self.TITLE_PATTERNS:
                m = pat.search(title)
                if m:
                    target_name = m.group("file")
                    folder_hint = m.groupdict().get("folder")
                    break

        if not target_name:
            return None

        cache_key = f"{folder_hint or ''}::{target_name}".lower()
        if cache_key in self._cache and self._cache[cache_key].is_file():
            return self._cache[cache_key]

        matches: List[Path] = []
        for root in self.search_roots:
            try:
                for p in root.rglob(target_name):
                    # Skip build/cache/venv directories
                    parts_lower = {pt.lower() for pt in p.parts}
                    if parts_lower & {"bin", "obj", "node_modules", ".git", ".vs", "venv", ".venv", "__pycache__", "build", "target"}:
                        continue
                    if p.is_file():
                        matches.append(p)
            except Exception:
                continue

        if not matches:
            return None

        # Prefer matches whose parent path contains `folder_hint`, then sort by most recently modified
        if folder_hint:
            fh = folder_hint.strip().lower()
            folder_matches = [m for m in matches if fh in str(m.parent).lower()]
            if folder_matches:
                matches = folder_matches

        best = max(matches, key=lambda p: p.stat().st_mtime)
        self._cache[cache_key] = best
        return best


# ---------------------------------------------------------------------------
# 5. UNIFIED SMART MERGE & DIRECT SYNC CONTROLLER
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    changed: bool
    delivery_mode: str           # "direct_disk", "clipboard_fallback", "noop", "blocked"
    merged_code: str
    target_path: Optional[Path] = None
    added_lines: List[str] = field(default_factory=list)
    repairs_made: List[str] = field(default_factory=list)
    remaining_diagnostics: List[SyntaxDiagnostic] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class ScreenBotSmartController:
    """
    Drop-in replacement controller for ScreenBot Class Mode & Practice Mode:
    - Confirmations Gate (2 consecutive stable reads)
    - Pre-Apply Circuit Breaker (blocks flapping before mutating)
    - Top-of-file Import/Using Hoisting
    - Any-Depth Entity & Anchored Line Merging (never repeats existing code)
    - AST Healer & Scope Indenter
    - Direct Disk Atomic Write (with automatic Clipboard Fallback if file is unsaved)
    """

    def __init__(
        self,
        workspace_roots: Optional[List[str | Path]] = None,
        require_confirmations: int = 2,
        history_dir: str | Path = ".screenbot_history",
    ):
        self.resolver = ActiveFileResolver(workspace_roots)
        self.healer = ASTHealer()
        self.require_confirmations = require_confirmations
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)

        self._last_frame_sig: Optional[str] = None
        self._sig_count: int = 0
        self._frame_counter: int = 0
        self._destructive_ticks: deque = deque(maxlen=10)
        self._freeze_until_tick: int = 0
        self._memory_snapshots: Dict[str, List[str]] = {}

    def process_frame_and_sync(
        self,
        raw_extracted_text: str,
        hint_filename: Optional[str] = None,
        overlay_detected: bool = False,
        low_quality: bool = False,
        allow_clipboard_fallback: bool = True,
    ) -> SyncResult:
        """
        End-to-end execution for a single capture tick:
        1. Sanitizes OCR/LLM text & checks 2-frame stability.
        2. Resolves target file on disk (or reads active IDE buffer via fallback).
        3. Merges imports, classes, methods, and loose lines without repeating existing code.
        4. Heals syntax & indentation across the whole file.
        5. Verifies AST health does not degrade.
        6. Atomically writes to disk (0 keystrokes) or clipboard-syncs.
        """
        self._frame_counter += 1

        if low_quality:
            return SyncResult(False, "noop", "", notes=["skipped_low_quality_frame"])

        incoming_lines = sanitize_vision_output(raw_extracted_text)
        if not incoming_lines:
            return SyncResult(False, "noop", "", notes=["empty_frame_after_sanitize"])

        # 2-Frame Consensus Gate
        sig = "\n".join(norm(x) for x in incoming_lines if not is_weak_anchor(x))
        if sig == self._last_frame_sig:
            self._sig_count += 1
        else:
            self._last_frame_sig = sig
            self._sig_count = 1

        if self._sig_count < self.require_confirmations:
            return SyncResult(False, "noop", "", notes=["awaiting_second_confirmation"])

        # Step 1: Resolve active file on disk for zero-keystroke Direct Sync
        target_path = self.resolver.resolve_active_file(hint_filename=hint_filename)
        existing_code = ""
        use_disk = False

        if target_path and target_path.is_file():
            try:
                existing_code = target_path.read_text(encoding="utf-8")
                use_disk = True
            except Exception:
                use_disk = False

        if not use_disk and allow_clipboard_fallback:
            existing_code = self._read_ide_via_clipboard()

        existing_lines = existing_code.splitlines() if existing_code.strip() else []
        lang = self.healer.detect_language(
            existing_code or "\n".join(incoming_lines),
            filename=str(target_path or hint_filename or ""),
        )

        # Step 2: Merge existing file + incoming screen code
        merged_lines, added_lines, is_destructive = self._smart_merge(
            existing_lines, incoming_lines
        )

        # Step 3: Pre-Apply Circuit Breaker for destructive changes
        if is_destructive:
            if overlay_detected or self._frame_counter < self._freeze_until_tick:
                return SyncResult(
                    False,
                    "blocked",
                    existing_code,
                    target_path=target_path,
                    notes=["destructive_edit_blocked_by_overlay_or_freeze"],
                )
            recent = [t for t in self._destructive_ticks if self._frame_counter - t <= 10]
            if len(recent) >= 3:
                self._freeze_until_tick = self._frame_counter + 15
                return SyncResult(
                    False,
                    "blocked",
                    existing_code,
                    target_path=target_path,
                    notes=["circuit_breaker_tripped_too_many_destructive_edits"],
                )
            self._destructive_ticks.append(self._frame_counter)

        # Step 4: Deterministic Syntax Healer & Scope Indenter across the whole file
        healed_lines, repairs = self.healer.heal_and_indent(
            merged_lines, reference_lines=existing_lines or merged_lines, lang=lang
        )
        final_code = "\n".join(healed_lines)
        if existing_code.endswith("\n") and not final_code.endswith("\n"):
            final_code += "\n"

        # If nothing changed at all, return noop
        if final_code.strip() == existing_code.strip():
            return SyncResult(
                False, "noop", existing_code, target_path=target_path, notes=["already_up_to_date"]
            )

        # Step 5: AST Health Gate (never inject if syntax errors increased)
        before_diags = self.healer.diagnose(existing_code, lang) if existing_code.strip() else []
        after_diags = self.healer.diagnose(final_code, lang)

        if existing_code.strip() and len(after_diags) > len(before_diags):
            return SyncResult(
                False,
                "blocked",
                existing_code,
                target_path=target_path,
                remaining_diagnostics=after_diags,
                notes=[f"ast_gate_blocked_errors_increased_{len(before_diags)}_to_{len(after_diags)}"],
            )

        # Step 6: Snapshot & Deliver (Direct Disk Write preferred, Clipboard Fallback second)
        key = str(target_path or "clipboard_buffer")
        self._save_snapshot(key, existing_code, target_path)

        if use_disk and target_path:
            self._atomic_write_file(target_path, final_code)
            return SyncResult(
                changed=True,
                delivery_mode="direct_disk",
                merged_code=final_code,
                target_path=target_path,
                added_lines=added_lines,
                repairs_made=repairs,
                remaining_diagnostics=after_diags,
            )

        if allow_clipboard_fallback:
            ok = self._write_ide_via_clipboard(final_code)
            return SyncResult(
                changed=ok,
                delivery_mode="clipboard_fallback" if ok else "blocked",
                merged_code=final_code,
                added_lines=added_lines,
                repairs_made=repairs,
                remaining_diagnostics=after_diags,
            )

        return SyncResult(False, "noop", final_code, target_path=target_path)

    def undo_last(self, target_path: Optional[Path] = None) -> bool:
        """Restores the previous snapshot on disk (Ctrl+Alt+Z handler)."""
        tp = target_path or self.resolver.resolve_active_file()
        key = str(tp or "clipboard_buffer")
        stack = self._memory_snapshots.get(key, [])
        if not stack:
            return False
        prev_code = stack.pop()
        if tp and tp.is_file():
            self._atomic_write_file(tp, prev_code)
            return True
        return self._write_ide_via_clipboard(prev_code)

    # -----------------------------------------------------------------------
    # INTERNAL MERGE ENGINE (IMPORTS + ENTITIES + ANCHORED SPLICING)
    # -----------------------------------------------------------------------
    def _smart_merge(
        self, base_lines: List[str], frame_lines: List[str]
    ) -> Tuple[List[str], List[str], bool]:
        if not base_lines:
            return list(frame_lines), list(frame_lines), False

        # 1. Separate top-level `using` / `import` statements from frame so they hoist cleanly
        frame_imports: List[str] = []
        frame_body: List[str] = []
        for l in frame_lines:
            if IMPORT_RE.match(l):
                frame_imports.append(l.strip())
            else:
                frame_body.append(l)

        working_base = list(base_lines)
        added: List[str] = []
        is_destructive = False

        # Hoist any new imports to the top of `working_base` without duplicating
        if frame_imports:
            existing_import_norms = {norm(x) for x in working_base if IMPORT_RE.match(x)}
            new_imports = [imp for imp in frame_imports if norm(imp) not in existing_import_norms]
            if new_imports:
                last_imp_idx = -1
                for i, l in enumerate(working_base):
                    if IMPORT_RE.match(l):
                        last_imp_idx = i
                insert_at = last_imp_idx + 1 if last_imp_idx >= 0 else 0
                working_base[insert_at:insert_at] = new_imports
                added.extend(new_imports)

        if not frame_body:
            return working_base, added, False

        # Check if all substantive lines in `frame_body` already exist in order
        base_subst_norms = [norm(x) for x in working_base if not is_weak_anchor(x)]
        frame_subst_norms = [norm(x) for x in frame_body if not is_weak_anchor(x)]
        if frame_subst_norms and self._is_subsequence(frame_subst_norms, base_subst_norms):
            return working_base, added, False

        # 2. Entity-Level Matching (Namespaces, Classes, Methods, Properties at ANY depth)
        base_entities = parse_entities(working_base)
        frame_entities = parse_entities(frame_body)

        base_by_full = {e.full_key: e for e in base_entities}
        base_by_short = {e.short_key: e for e in base_entities}

        if frame_entities:
            replacements: List[Tuple[int, int, List[str]]] = []
            for fe in frame_entities:
                target_ent = base_by_full.get(fe.full_key) or base_by_short.get(fe.short_key)
                f_slice = frame_body[fe.start_line : fe.end_line + 1]

                # If a class is on screen AND both files have inner methods, merge at the method level
                # unless the lecturer completely rewrote the class with different methods
                if fe.kind in ("namespace", "class") and target_ent is not None:
                    inner_f = [
                        m for m in frame_entities
                        if m.start_line > fe.start_line and m.end_line < fe.end_line
                    ]
                    if inner_f:
                        continue

                if target_ent is not None:
                    t_slice = working_base[target_ent.start_line : target_ent.end_line + 1]
                    spliced, new_l, destr = self._anchored_splice(t_slice, f_slice)
                    is_destructive = is_destructive or destr
                    replacements.append((target_ent.start_line, target_ent.end_line + 1, spliced))
                    added.extend(new_l)
                elif fe.kind in ("method", "property"):
                    # New method/property -> insert inside the enclosing class in `working_base`
                    classes = [e for e in base_entities if e.kind == "class"]
                    if classes:
                        ins_pt = classes[-1].end_line
                        replacements.append((ins_pt, ins_pt, [""] + f_slice))
                        added.extend(f_slice)

            if replacements:
                replacements.sort(key=lambda r: (r[1] - r[0], r[0]))
                filtered: List[Tuple[int, int, List[str]]] = []
                used_ranges: List[Tuple[int, int]] = []
                for s, e, chunk in replacements:
                    if any(s < ue and e > us for us, ue in used_ranges):
                        continue
                    filtered.append((s, e, chunk))
                    if e > s:
                        used_ranges.append((s, e))

                for s, e, chunk in sorted(filtered, key=lambda r: r[0], reverse=True):
                    working_base[s:e] = chunk
                return working_base, added, is_destructive

        # 3. Fallback: Anchored Line Splicing across viewport
        spliced_all, new_l, destr = self._anchored_splice(working_base, frame_body)
        added.extend(new_l)
        return spliced_all, added, destr

    def _anchored_splice(
        self, base_lines: List[str], inc_lines: List[str]
    ) -> Tuple[List[str], List[str], bool]:
        base_s = [(i, norm(l)) for i, l in enumerate(base_lines) if not is_weak_anchor(l)]
        inc_s = [(j, norm(l)) for j, l in enumerate(inc_lines) if not is_weak_anchor(l)]

        if not inc_s:
            return base_lines, [], False
        if not base_s:
            return inc_lines, list(inc_lines), False

        sm = difflib.SequenceMatcher(
            None, [n for _, n in base_s], [n for _, n in inc_s], autojunk=False
        )
        anchors: List[Tuple[int, int]] = [
            (base_s[a + k][0], inc_s[b + k][0])
            for a, b, size in sm.get_matching_blocks()
            for k in range(size)
        ]

        if not anchors:
            # No shared anchors: insert inside the deepest active method/class instead of outside EOF
            entities = parse_entities(base_lines)
            methods = [e for e in entities if e.kind == "method"]
            classes = [e for e in entities if e.kind == "class"]
            ins_idx = methods[-1].end_line if methods else (classes[-1].end_line if classes else len(base_lines))
            merged = base_lines[:ins_idx] + inc_lines + base_lines[ins_idx:]
            return merged, list(inc_lines), False

        out: List[str] = []
        added: List[str] = []
        destructive = False

        b0, i0 = anchors[0]
        # Keep scrolled-off top of base file
        out.extend(base_lines[:b0])
        if b0 == 0 and i0 > 0:
            out.extend(inc_lines[:i0])
            added.extend(inc_lines[:i0])

        for idx, (b_curr, i_curr) in enumerate(anchors):
            out.append(base_lines[b_curr])
            if idx + 1 < len(anchors):
                b_next, i_next = anchors[idx + 1]
                base_gap = base_lines[b_curr + 1 : b_next]
                inc_gap = inc_lines[i_curr + 1 : i_next]
                if inc_gap:
                    out.extend(inc_gap)
                    base_norms = {norm(x) for x in base_gap}
                    added.extend([x for x in inc_gap if norm(x) not in base_norms])
                    if len([x for x in base_gap if not is_weak_anchor(x)]) > len(
                        [x for x in inc_gap if not is_weak_anchor(x)]
                    ):
                        destructive = True
                else:
                    # Keep structural braces between anchors
                    out.extend(base_gap)

        b_last, i_last = anchors[-1]
        inc_tail = inc_lines[i_last + 1 :]
        if any(not is_weak_anchor(x) for x in inc_tail):
            base_tail = base_lines[b_last + 1 :]
            base_closes = sum(1 for x in base_tail if x.strip() in ("}", "};"))
            trimmed_inc_tail = list(inc_tail)
            while base_closes > 0 and trimmed_inc_tail and trimmed_inc_tail[-1].strip() in ("}", "};"):
                trimmed_inc_tail.pop()
                base_closes -= 1
            out.extend(trimmed_inc_tail)
            added.extend(trimmed_inc_tail)
            out.extend(base_tail)
        else:
            out.extend(base_lines[b_last + 1 :])

        return out, added, destructive

    @staticmethod
    def _is_subsequence(small: List[str], big: List[str]) -> bool:
        it = iter(big)
        return all(x in it for x in small if x)

    # -----------------------------------------------------------------------
    # ATOMIC DISK WRITE & SAFE CLIPBOARD FALLBACK
    # -----------------------------------------------------------------------
    @staticmethod
    def _atomic_write_file(target_path: Path, content: str) -> None:
        """Writes to a temp file in the same directory and atomically replaces target_path."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target_path.parent), prefix=f".{target_path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            os.replace(tmp_name, target_path)
        except Exception:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
            raise

    def _save_snapshot(self, key: str, old_code: str, target_path: Optional[Path]) -> None:
        if not old_code.strip():
            return
        stack = self._memory_snapshots.setdefault(key, [])
        if not stack or stack[-1] != old_code:
            stack.append(old_code)
            if len(stack) > 50:
                stack.pop(0)

        if target_path:
            stamp = int(time.time() * 1000)
            snap_file = self.history_dir / f"{target_path.name}.{stamp}.bak"
            try:
                snap_file.write_text(old_code, encoding="utf-8")
            except Exception:
                pass

    @staticmethod
    def _read_ide_via_clipboard() -> str:
        try:
            import pyperclip
            import keyboard
        except ImportError:
            return ""

        saved = ""
        try:
            saved = pyperclip.paste()
        except Exception:
            pass

        try:
            wait_for_physical_keys_released()
            pyperclip.copy("")
            keyboard.send("ctrl+a")
            time.sleep(0.05)
            keyboard.send("ctrl+c")
            time.sleep(0.07)
            keyboard.send("right")
            return pyperclip.paste() or ""
        finally:
            try:
                pyperclip.copy(saved)
            except Exception:
                pass

    @staticmethod
    def _write_ide_via_clipboard(new_code: str) -> bool:
        try:
            import pyperclip
            import keyboard
        except ImportError:
            return False

        saved = ""
        try:
            saved = pyperclip.paste()
        except Exception:
            pass

        try:
            wait_for_physical_keys_released()
            pyperclip.copy(new_code)
            keyboard.send("ctrl+a")
            time.sleep(0.05)
            keyboard.send("ctrl+v")
            time.sleep(0.07)
            return True
        finally:
            try:
                pyperclip.copy(saved)
            except Exception:
                pass
