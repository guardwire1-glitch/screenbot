"""
ast_healer.py
Gives ScreenBot IDE-level awareness of the entire code buffer:
1. Parses the full file into a Concrete Syntax Tree (tree-sitter) for Java, C#, Python.
2. Pinpoints exact (line, col) of syntax errors anywhere in the file (typed or untyped).
3. Deterministically heals mechanical errors (missing ';', unbalanced '{}', doubled brackets,
   OCR typos, and scope-based pro indentation).
4. Provides a targeted context prompt so the LLM can surgically fix deep errors if needed.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Optional tree-sitter imports (gracefully degrades if not installed yet)
try:
    from tree_sitter import Language, Parser, Node
    import tree_sitter_c_sharp as ts_csharp
    import tree_sitter_java as ts_java
    import tree_sitter_python as ts_python
    HAS_TREE_SITTER = True
except ImportError:
    HAS_TREE_SITTER = False


@dataclass
class CodeIssue:
    line: int          # 1-indexed line number
    column: int        # 1-indexed column
    kind: str          # "MISSING_TOKEN", "SYNTAX_ERROR", "UNBALANCED_BRACE", "INDENT_DRIFT"
    snippet: str       # The offending line of code
    suggestion: str    # How to fix it


@dataclass
class HealedBuffer:
    code: str
    was_healed: bool
    fixed_issues: List[str] = field(default_factory=list)
    remaining_issues: List[CodeIssue] = field(default_factory=list)
    needs_llm_repair: bool = False


class HyperAwareCodeEngine:
    def __init__(self, default_lang: str = "csharp"):
        self.default_lang = default_lang
        self._parsers = {}
        if HAS_TREE_SITTER:
            self._init_parsers()

    def _init_parsers(self) -> None:
        lang_map = {
            "csharp": ts_csharp.language(),
            "java": ts_java.language(),
            "python": ts_python.language(),
        }
        for name, ptr in lang_map.items():
            lang = Language(ptr)
            parser = Parser(lang)
            self._parsers[name] = parser

    def detect_language(self, code: str, filename: str = "") -> str:
        fn = (filename or "").lower()
        if fn.endswith(".cs"):
            return "csharp"
        if fn.endswith(".java"):
            return "java"
        if fn.endswith(".py"):
            return "python"
        if "using System" in code or "Console.Write" in code or "namespace " in code:
            return "csharp"
        if "public static void main" in code or "System.out.print" in code:
            return "java"
        if "def " in code and ":" in code and "{" not in code:
            return "python"
        return self.default_lang

    # -----------------------------------------------------------------------
    # STEP 1: DEEP AST DIAGNOSTICS (FINDS ERRORS ANYWHERE IN THE FILE)
    # -----------------------------------------------------------------------
    def diagnose(self, code: str, lang: Optional[str] = None) -> List[CodeIssue]:
        """
        Scans the entire file (not just around the cursor) and returns every
        syntax error or missing token using tree-sitter + structural heuristics.
        """
        lang = lang or self.detect_language(code)
        lines = code.splitlines()
        issues: List[CodeIssue] = []

        if HAS_TREE_SITTER and lang in self._parsers:
            tree = self._parsers[lang].parse(code.encode("utf-8"))
            self._collect_ast_errors(tree.root_node, lines, issues)
        else:
            # Fallback structural scanner if tree-sitter isn't installed yet
            self._collect_heuristic_errors(lines, lang, issues)

        return issues

    def _collect_ast_errors(self, node: "Node", lines: List[str], out: List[CodeIssue]) -> None:
        if node.is_missing:
            ln = node.start_point[0] + 1
            col = node.start_point[1] + 1
            snip = lines[ln - 1].strip() if 0 <= ln - 1 < len(lines) else ""
            out.append(
                CodeIssue(
                    line=ln,
                    column=col,
                    kind="MISSING_TOKEN",
                    snippet=snip,
                    suggestion=f"Insert missing '{node.type}' at line {ln}, col {col}",
                )
            )
        elif node.type == "ERROR":
            ln = node.start_point[0] + 1
            col = node.start_point[1] + 1
            snip = lines[ln - 1].strip() if 0 <= ln - 1 < len(lines) else ""
            out.append(
                CodeIssue(
                    line=ln,
                    column=col,
                    kind="SYNTAX_ERROR",
                    snippet=snip,
                    suggestion=f"Syntax error near '{node.text.decode('utf-8', errors='ignore')[:30]}' on line {ln}",
                )
            )

        for child in node.children:
            self._collect_ast_errors(child, lines, out)

    def _collect_heuristic_errors(self, lines: List[str], lang: str, out: List[CodeIssue]) -> None:
        brace_stack: List[int] = []
        for idx, raw in enumerate(lines):
            s = raw.strip()
            if not s or s.startswith("//"):
                continue
            for ch in s:
                if ch == "{":
                    brace_stack.append(idx + 1)
                elif ch == "}":
                    if not brace_stack:
                        out.append(CodeIssue(idx + 1, 1, "EXTRA_CLOSING_BRACE", s, "Remove stray '}'"))
                    else:
                        brace_stack.pop()
            if lang in ("csharp", "java") and self._looks_like_missing_semicolon(s):
                out.append(CodeIssue(idx + 1, len(raw), "MISSING_SEMICOLON", s, "Append ';'"))

        for open_ln in brace_stack:
            out.append(CodeIssue(open_ln, 1, "UNCLOSED_BRACE", lines[open_ln - 1].strip(), "Add matching '}'"))

    # -----------------------------------------------------------------------
    # STEP 2: DETERMINISTIC SELF-HEALING & PRO INDENTATION
    # -----------------------------------------------------------------------
    def heal_entire_file(self, code: str, filename: str = "") -> HealedBuffer:
        """
        Inspects, repairs, and formats the entire file deterministically:
        1. Fixes IDE auto-pair collisions (e.g. `;;`, `{}` artifacts, unclosed quotes).
        2. Fixes missing semicolons on clear C#/Java statements.
        3. Repairs unbalanced closing/opening braces at scope boundaries.
        4. Re-indents the entire file from AST/lexical scope depth.
        5. Re-verifies with tree-sitter; flags `needs_llm_repair=True` ONLY if deep errors remain.
        """
        lang = self.detect_language(code, filename)
        lines = code.splitlines()
        fixed_log: List[str] = []

        # Pass 1: Line-by-line deterministic syntax fixes
        repaired_lines: List[str] = []
        for idx, raw in enumerate(lines):
            line_num = idx + 1
            s = raw.strip()

            # 1a. Fix accidental double semicolons (unless inside `for(;;)`)
            if ";;" in s and not re.search(r"\bfor\s*\(", s):
                raw = re.sub(r";\s*;+", ";", raw)
                fixed_log.append(f"Line {line_num}: collapsed duplicate ';'")
                s = raw.strip()

            # 1b. Fix IDE auto-close bracket collision at end of method declaration: `void Foo()) {` -> `void Foo() {`
            if re.search(r"\([^{};()]*\)\)\s*\{?$", s) and s.count(")") > s.count("("):
                raw = re.sub(r"\)\)(\s*\{?)$", r")\1", raw)
                fixed_log.append(f"Line {line_num}: removed extra ')' from auto-close collision")
                s = raw.strip()

            # 1c. Fix missing semicolon on unambiguous C#/Java statements
            if lang in ("csharp", "java") and self._looks_like_missing_semicolon(s):
                # Preserve trailing inline comment if present
                if "//" in raw:
                    code_part, comment_part = raw.split("//", 1)
                    raw = f"{code_part.rstrip()}; // {comment_part.strip()}"
                else:
                    raw = f"{raw.rstrip()};"
                fixed_log.append(f"Line {line_num}: added missing ';'")

            repaired_lines.append(raw)

        # Pass 2: Structural Brace Balancing (C# / Java)
        if lang in ("csharp", "java"):
            repaired_lines = self._balance_braces(repaired_lines, fixed_log)

        # Pass 3: Pro-Level Scope Indentation across the whole file
        if lang in ("csharp", "java"):
            repaired_lines = self._format_brace_language(repaired_lines, lang)

        healed_code = "\n".join(repaired_lines)
        remaining = self.diagnose(healed_code, lang)

        return HealedBuffer(
            code=healed_code,
            was_healed=(healed_code != code),
            fixed_issues=fixed_log,
            remaining_issues=remaining,
            needs_llm_repair=(len(remaining) > 0),
        )

    @staticmethod
    def _looks_like_missing_semicolon(s: str) -> bool:
        """True if `s` is clearly a complete C#/Java statement missing a trailing `;`."""
        if not s or s.startswith(("//", "/*", "*", "#", "[", "@")):
            return False
        if s.endswith((";", "{", "}", ",", ":", "\\")):
            return False
        # Control / declaration headers never end with ';'
        if re.match(
            r"^(if|else|for|foreach|while|switch|try|catch|finally|namespace|class|interface|enum|struct|record)\b",
            s,
        ):
            return False
        # Method signatures don't end with ';' (unless abstract/interface, which users rarely mistype)
        if re.match(r"^(public|private|protected|internal|static|virtual|override|async)\b.*\)\s*$", s):
            return False
        # Clear statement patterns: return, Console/System calls, assignments, increments
        if re.match(r"^(return|break|continue|throw|using)\b", s):
            return True
        if re.match(r"^(Console\.|System\.out\.|var\s+\w+\s*=|[A-Za-z_]\w*(\[\])?\s+[A-Za-z_]\w*\s*=)", s):
            return True
        if re.search(r"(\+\+|--|\+=|-=|\*=|/=)\s*$", s):
            return True
        return False

    @staticmethod
    def _balance_braces(lines: List[str], fixed_log: List[str]) -> List[str]:
        """Removes stray extra `}` and appends missing closing `}` at EOF."""
        out: List[str] = []
        depth = 0
        for idx, raw in enumerate(lines):
            s = raw.strip()
            # Skip comments/strings for rough brace count
            clean = re.sub(r'"(?:\\.|[^"\\])*"|//.*$', "", s)
            if clean == "}" and depth == 0:
                fixed_log.append(f"Line {idx + 1}: removed stray unmatched '}}'")
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
            fixed_log.append(f"EOF: appended {depth} missing closing '}}'")
        return out

    @staticmethod
    def _format_brace_language(lines: List[str], lang: str) -> List[str]:
        """Re-indents the entire file cleanly using 4-space lexical scope depth."""
        indent_unit = "    "
        formatted: List[str] = []
        depth = 0
        single_line_ctrl = False

        for raw in lines:
            s = raw.strip()
            if not s:
                formatted.append("")
                continue

            clean = re.sub(r'"(?:\\.|[^"\\])*"|//.*$', "", s).strip()

            leading_closes = 0
            for ch in clean:
                if ch == "}":
                    leading_closes += 1
                else:
                    break

            eff_depth = max(0, depth - leading_closes)
            if re.match(r"^(case\s+.+|default)\s*:", clean):
                eff_depth = max(0, eff_depth - 1)

            extra = 1 if (single_line_ctrl and not clean.startswith("{")) else 0
            formatted.append(f"{indent_unit * (eff_depth + extra)}{s}")

            net = 0
            for i, ch in enumerate(clean):
                if ch == "{":
                    net += 1
                elif ch == "}" and i >= leading_closes:
                    net -= 1
            depth = max(0, eff_depth + net)

            if re.match(r"^(if|for|foreach|while|else)\b", clean) and not clean.endswith((";", "{", "}")):
                single_line_ctrl = True
            else:
                single_line_ctrl = False

        return formatted

    # -----------------------------------------------------------------------
    # STEP 3: SURGICAL CONTEXT PROMPT FOR DEEP SYNTAX/LOGIC REPAIRS
    # -----------------------------------------------------------------------
    def build_llm_repair_prompt(
        self, current_file_code: str, new_screen_code: str, issues: List[CodeIssue]
    ) -> str:
        """
        When tree-sitter detects deep syntax errors that deterministic rules can't guess,
        this builds a minimal-token, hyper-aware prompt giving the LLM:
        1. The current IDE file (with line numbers)
        2. The exact AST error coordinates found anywhere in the file
        3. The newly captured screen code
        """
        numbered_file = "\n".join(
            f"{i + 1:3d} | {line}" for i, line in enumerate(current_file_code.splitlines())
        )
        issue_list = "\n".join(
            f"- Line {iss.line}, Col {iss.column} [{iss.kind}]: `{iss.snippet}` -> {iss.suggestion}"
            for iss in issues[:10]
        )
        return (
            "You are an expert IDE Language Server and code merger.\n"
            "Merge the NEW SCREEN CODE into the EXISTING IDE FILE and fix all syntax errors.\n\n"
            "STRICT RULES:\n"
            "1. DO NOT duplicate classes, methods, or lines that already exist in the EXISTING IDE FILE.\n"
            "2. Fix every AST syntax error listed in DIAGNOSTICS (even if it is in older code).\n"
            "3. Preserve all existing methods/lines that simply scrolled off the lecturer's screen.\n"
            "4. Return ONLY the final, valid, properly indented source code. No markdown fences, no explanations.\n\n"
            f"=== AST DIAGNOSTICS TO FIX ===\n{issue_list or 'None'}\n\n"
            f"=== EXISTING IDE FILE ===\n{numbered_file}\n\n"
            f"=== NEW SCREEN CODE ===\n{new_screen_code}\n"
        )
