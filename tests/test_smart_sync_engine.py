"""
test_smart_sync_engine.py
Run with: uv run pytest -v tests/test_smart_sync_engine.py
"""

from pathlib import Path
from screen_bot.legacy.smart_sync_engine import (
    ASTHealer,
    ScreenBotSmartController,
    norm,
    parse_entities,
)


def test_string_literal_whitespace_is_preserved():
    """Fixes the bug in doc_merge.py where string whitespace changes were ignored."""
    line1 = 'Console.WriteLine("Hello World");'
    line2 = 'Console.WriteLine("Hello   World");'
    assert norm(line1) != norm(line2)
    # Code whitespace outside strings should still normalize to equal
    line3 = 'Console.WriteLine(  "Hello World"  ) ; // comment'
    assert norm(line1) == norm(line3)


def test_parse_entities_at_any_nesting_depth():
    """Verifies methods & C# properties inside namespaces and classes are parsed (not just col-0)."""
    code = [
        "namespace DemoApp {",
        "    public class Calculator {",
        "        public int Count { get; set; }",
        "        public int Square(int x) => x * x;",
        "        public int Add(int a, int b) {",
        "            return a + b;",
        "        }",
        "    }",
        "}",
    ]
    ents = parse_entities(code)
    keys = {e.short_key for e in ents}
    assert "namespace:DemoApp" in keys
    assert "class:Calculator" in keys
    assert "property:Count" in keys
    assert "method:Square(int)" in keys
    assert "method:Add(int,int)" in keys


def test_existing_code_never_repeated_and_inner_method_updated(tmp_path: Path):
    """
    Reproduces the YouTube `public class Main` bug:
    - File already has `Main` with `hello()` and `add(int x, int y)`
    - Lecturer updates `add` and adds `subtract`
    - Result must have ONE `public class Main`, keep `hello()`, update `add()`, and insert `subtract()`.
    """
    target_file = tmp_path / "Main.java"
    initial_code = (
        "public class Main {\n"
        "    public static void hello() {\n"
        '        System.out.println("Hello");\n'
        "    }\n\n"
        "    public static int add(int x, int y) {\n"
        "        return x;\n"
        "    }\n"
        "}\n"
    )
    target_file.write_text(initial_code, encoding="utf-8")

    ctrl = ScreenBotSmartController(
        workspace_roots=[tmp_path],
        require_confirmations=2,
        history_dir=tmp_path / ".history",
    )
    ctrl.resolver.pin_target_file(target_file)

    # Lecturer scrolled down: `hello()` is off-screen, `add()` is fixed, and `sub()` is added
    screen_frame = (
        "    public static int add(int x, int y) {\n"
        "        int sum = x + y;\n"
        "        return sum;\n"
        "    }\n"
        "    public static int sub(int x, int y) {\n"
        "        return x - y;\n"
        "    }\n"
    )

    # Frame 1: held for 2-frame confirmation
    r1 = ctrl.process_frame_and_sync(screen_frame, allow_clipboard_fallback=False)
    assert not r1.changed

    # Frame 2: confirmed and synced directly to disk
    r2 = ctrl.process_frame_and_sync(screen_frame, allow_clipboard_fallback=False)
    assert r2.changed
    assert r2.delivery_mode == "direct_disk"

    updated_disk = target_file.read_text(encoding="utf-8")
    # Never duplicate `public class Main`
    assert updated_disk.count("public class Main") == 1
    # Scrolled-off `hello()` is still preserved
    assert "public static void hello()" in updated_disk
    # `add()` was updated in-place
    assert "int sum = x + y;" in updated_disk
    # New method `sub()` was spliced inside Main before the final closing brace
    assert "public static int sub(int x, int y)" in updated_disk
    assert updated_disk.strip().endswith("}")


def test_syntax_healer_repairs_errors_anywhere_in_file():
    """
    Verifies that broken syntax in older code (missing ';', doubled ';;', extra ')',
    crooked indentation, and missing closing '}') is repaired automatically.
    """
    healer = ASTHealer()
    broken_csharp = [
        "using System",  # Missing semicolon at line 1!
        "public class Program {",
        "public static void Main()) {",  # Extra ')' from IDE auto-close
        "int a = 5;;",  # Doubled ';;'
        "Console.WriteLine(a)",  # Missing ';'
        "if (a > 0) {",
        "Console.WriteLine(\"Positive\");",
        # Missing 3 closing braces at EOF!
    ]

    healed_lines, repairs = healer.heal_and_indent(broken_csharp, lang="csharp")
    healed_text = "\n".join(healed_lines)

    assert "using System;" in healed_text
    assert "public static void Main() {" in healed_text
    assert "int a = 5;" in healed_text
    assert ";;" not in healed_text
    assert "Console.WriteLine(a);" in healed_text
    # Check pro indentation (4 spaces per brace level)
    assert "    public static void Main() {" in healed_lines
    assert "        int a = 5;" in healed_lines
    assert "            Console.WriteLine(\"Positive\");" in healed_lines
    # Check braces were balanced
    diags = healer.diagnose(healed_text, lang="csharp")
    assert not any(d.kind == "UNBALANCED_BRACES" for d in diags)
    assert len(repairs) >= 4


def test_import_hoisting_and_undo(tmp_path: Path):
    """Verifies new `using` statements hoist to top of file and `undo_last()` restores disk state."""
    cs_file = tmp_path / "NumericMod.cs"
    original = (
        "using System;\n\n"
        "public class NumericMod {\n"
        "    public void Run() {\n"
        "        int x = 1;\n"
        "    }\n"
        "}\n"
    )
    cs_file.write_text(original, encoding="utf-8")

    ctrl = ScreenBotSmartController(
        workspace_roots=[tmp_path],
        require_confirmations=1,
        history_dir=tmp_path / ".history",
    )
    ctrl.resolver.pin_target_file(cs_file)

    frame = (
        "using System;\n"
        "using System.Linq;\n"
        "public class NumericMod {\n"
        "    public void Run() {\n"
        "        int x = 1;\n"
        "        int y = 2;\n"
        "    }\n"
        "}\n"
    )
    res = ctrl.process_frame_and_sync(frame, allow_clipboard_fallback=False)
    assert res.changed
    new_disk = cs_file.read_text(encoding="utf-8")
    assert new_disk.count("using System;") == 1
    assert "using System.Linq;" in new_disk.splitlines()[:3]
    assert "int y = 2;" in new_disk

    # Test Undo
    assert ctrl.undo_last(cs_file) is True
    assert cs_file.read_text(encoding="utf-8") == original
