"""Per-language tree-sitter availability.

A missing or broken grammar for one language must never disable parsing for
the languages whose grammar is installed.
"""

from types import SimpleNamespace

from screen_bot import syntax_gate


class _FakeNode:
    is_missing = False
    type = "module"
    children = ()
    start_point = (0, 0)


class _FakeTree:
    root_node = _FakeNode()


class _FakeParser:
    def __init__(self, language):
        self.language = language

    def parse(self, _raw):
        return _FakeTree()


class _FakeCore:
    Language = staticmethod(lambda pointer: pointer)
    Parser = _FakeParser


def _install_fake_grammars(monkeypatch, grammars):
    monkeypatch.setattr(syntax_gate, "_TS_CORE", _FakeCore())
    monkeypatch.setattr(syntax_gate, "_GRAMMARS", grammars)
    monkeypatch.setattr(syntax_gate, "_PARSERS", {})


def test_missing_grammar_does_not_disable_the_others(monkeypatch):
    core = _FakeCore()
    csharp = SimpleNamespace(language=lambda: "csharp")
    python = SimpleNamespace(language=lambda: "python")
    installed = {
        "tree_sitter": core,
        "tree_sitter_c_sharp": csharp,
        "tree_sitter_python": python,
    }

    def fake_import(name):
        if name in installed:
            return installed[name]
        raise ImportError(name)

    monkeypatch.setattr(syntax_gate.importlib, "import_module", fake_import)

    loaded_core, grammars = syntax_gate._load_tree_sitter()

    assert loaded_core is core
    assert set(grammars) == {"csharp", "python"}


def test_installed_grammar_uses_tree_sitter_and_missing_one_falls_back(monkeypatch):
    _install_fake_grammars(
        monkeypatch, {"csharp": SimpleNamespace(language=lambda: "csharp")}
    )

    csharp = syntax_gate.check("public class Demo {}", "csharp")
    java = syntax_gate.check("public class Demo {}", "java")

    assert csharp.parser == "tree-sitter"
    assert csharp.clean
    assert java.parser == "delimiter-scanner"


def test_broken_grammar_only_disables_that_language(monkeypatch):
    def broken_pointer():
        raise RuntimeError("grammar ABI mismatch")

    _install_fake_grammars(
        monkeypatch,
        {
            "csharp": SimpleNamespace(language=broken_pointer),
            "java": SimpleNamespace(language=lambda: "java"),
        },
    )

    assert syntax_gate.check("public class Demo {}", "csharp").parser == "delimiter-scanner"
    assert syntax_gate.check("public class Demo {}", "java").parser == "tree-sitter"


def test_missing_tree_sitter_core_falls_back_for_every_language(monkeypatch):
    monkeypatch.setattr(syntax_gate, "_TS_CORE", None)
    monkeypatch.setattr(syntax_gate, "_GRAMMARS", {})
    monkeypatch.setattr(syntax_gate, "_PARSERS", {})

    assert syntax_gate.check("public class Demo {}", "csharp").parser == "delimiter-scanner"
    assert syntax_gate.check("def demo():\n    pass\n", "python").parser == "delimiter-scanner"
