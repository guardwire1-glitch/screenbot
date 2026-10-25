"""
Syntax gate for edits that are about to reach a real editor.

The gate never claims "this compiles". It answers a narrower, reliable question:
does the proposed buffer introduce more parser errors than the buffer the user
already has? tree-sitter is used where a grammar is installed; otherwise the
deterministic delimiter scanner from code_validator is the backstop.

This is what lets ScreenBot auto-apply lecture code while refusing a change
that would break a file that was previously clean.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from .code_validator import validate_syntax
except ImportError:
    from code_validator import validate_syntax

_GRAMMAR_MODULES = {
    "csharp": "tree_sitter_c_sharp",
    "java": "tree_sitter_java",
    "python": "tree_sitter_python",
}


def _optional_import(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _load_tree_sitter() -> Tuple[Any, Dict[str, Any]]:
    """Load the parser core and each grammar independently.

    A missing or broken grammar must never disable the languages whose parser
    is installed.
    """
    core = _optional_import("tree_sitter")
    if core is None or not hasattr(core, "Language") or not hasattr(core, "Parser"):
        return None, {}
    grammars = {}
    for language, module_name in _GRAMMAR_MODULES.items():
        module = _optional_import(module_name)
        if module is not None:
            grammars[language] = module
    return core, grammars


_TS_CORE, _GRAMMARS = _load_tree_sitter()
HAS_TREE_SITTER = bool(_GRAMMARS)
AVAILABLE_GRAMMARS = frozenset(_GRAMMARS)

_ALIASES = {
    "cs": "csharp", "c#": "csharp", "c_sharp": "csharp", "csharp": "csharp",
    "java": "java", "py": "python", "python": "python",
    "tsql": "sql", "plsql": "sql", "sql": "sql",
    "js": "javascript", "javascript": "javascript", "typescript": "typescript",
    "cpp": "cpp", "c++": "cpp", "c": "c", "go": "go", "rust": "rust",
}

_PARSERS = {}


def _grammar(language: str):
    lang = _ALIASES.get((language or "").lower(), (language or "").lower())
    if _TS_CORE is None:
        return None
    if lang in _PARSERS:
        return _PARSERS[lang]
    module = _GRAMMARS.get(lang)
    if module is None:
        _PARSERS[lang] = None
        return None
    try:
        _PARSERS[lang] = _TS_CORE.Parser(_TS_CORE.Language(module.language()))
    except Exception:
        _PARSERS[lang] = None
    return _PARSERS[lang]


@dataclass
class SyntaxStatus:
    language: str
    parser: str
    errors: List[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def clean(self) -> bool:
        return not self.errors


def _collect(node, code: bytes, out: List[str], limit: int = 60) -> None:
    if len(out) >= limit:
        return
    if node.is_missing or node.type == "ERROR":
        row = node.start_point[0] + 1
        col = node.start_point[1] + 1
        text = code.splitlines()[row - 1].decode("utf-8", "replace").strip() if code else ""
        kind = "missing" if node.is_missing else "error"
        out.append(f"{kind} at {row}:{col}: {text[:120]}")
    for child in node.children:
        _collect(child, code, out, limit)


def check(code: str, language: str = "unknown", filename: Optional[str] = None) -> SyntaxStatus:
    lang = _ALIASES.get((language or "").lower(), (language or "unknown").lower())
    if filename and lang in {"unknown", ""}:
        low = filename.lower()
        if low.endswith(".cs"):
            lang = "csharp"
        elif low.endswith(".java"):
            lang = "java"
        elif low.endswith(".py"):
            lang = "python"

    parser = _grammar(lang)
    if parser is not None:
        try:
            raw = (code or "").encode("utf-8")
            tree = parser.parse(raw)
            errors: List[str] = []
            _collect(tree.root_node, raw, errors)
            return SyntaxStatus(language=lang, parser="tree-sitter", errors=errors)
        except Exception:
            pass

    valid, reason = validate_syntax(code, language=lang)
    return SyntaxStatus(
        language=lang,
        parser="delimiter-scanner",
        errors=[] if valid else [reason],
    )


def gate(
    baseline_text: str,
    proposed_text: str,
    language: str = "unknown",
    filename: Optional[str] = None,
) -> Tuple[bool, str, SyntaxStatus]:
    """Returns (ok, reason, proposed_status).

    ok is True only when the proposal does not increase the parser error count.
    A file that already contains user errors can still receive lecture additions,
    but those additions may not make the file worse.
    """
    baseline = check(baseline_text, language, filename)
    proposed = check(proposed_text, language, filename)

    if proposed.error_count > baseline.error_count:
        first = proposed.errors[0] if proposed.errors else "unknown parse error"
        return False, (
            f"proposed code adds {proposed.error_count - baseline.error_count} parser "
            f"error(s) (baseline {baseline.error_count}); first: {first}"
        ), proposed

    if baseline.clean and not proposed.clean:
        return False, f"proposed code is not parseable: {proposed.errors[0]}", proposed

    return True, "", proposed
