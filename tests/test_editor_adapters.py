"""Contract tests for the editor adapters.

These lock down the properties that keep a bound file safe:
  - an explicit path is never swapped for the resolver's active file,
  - disk writes are hash-checked and preserve the file's newline style,
  - restore() uses the hash the successful apply reported.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

import editor_adapters
from editor_adapters import (
    DiskAdapter,
    EditorRouter,
    detect_ide,
    title_is_dirty,
)


def _router(tmp_path, resolver_target=None):
    bridge = SimpleNamespace(live_hosts=lambda: [])
    return EditorRouter(bridge, resolver=lambda: resolver_target)


def test_disk_apply_preserves_crlf_and_normalizes_mixed_newlines(tmp_path):
    path = tmp_path / "Demo.cs"
    path.write_bytes(b"class Demo\r\n{\r\n}\r\n")
    adapter = DiskAdapter(lambda: path)
    ctx = adapter.get_context(str(path))

    proposed = "class Demo\n{\n    int x = 1;\n}\n"
    result = adapter.apply(ctx, proposed)

    assert result.ok
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert b"int x = 1;" in raw


def test_disk_apply_rejects_a_stale_hash(tmp_path):
    path = tmp_path / "Demo.cs"
    path.write_text("class Demo\n{\n}\n", encoding="utf-8")
    adapter = DiskAdapter(lambda: path)
    ctx = adapter.get_context(str(path))

    path.write_text("class Demo\n{\n    // user edit\n}\n", encoding="utf-8")
    result = adapter.apply(ctx, "class Demo\n{\n    int x = 1;\n}\n")

    assert not result.ok
    assert result.conflict
    assert "user edit" in path.read_text(encoding="utf-8")


def test_router_reads_the_exact_requested_path_not_the_active_file(tmp_path, monkeypatch):
    wanted = tmp_path / "Wanted.cs"
    wanted.write_text("class Wanted { }\n", encoding="utf-8")
    other = tmp_path / "Other.cs"
    other.write_text("class Other { }\n", encoding="utf-8")

    router = _router(tmp_path, resolver_target=other)
    monkeypatch.setattr(
        editor_adapters, "_foreground_window_info", lambda: (1, "Chrome", "chrome.exe")
    )

    ctx = router.get_context(file_path=str(wanted))

    assert ctx is not None
    assert Path(ctx.file_path).resolve() == wanted.resolve()
    assert "Wanted" in ctx.text


def test_restore_uses_the_applied_hash(tmp_path):
    path = tmp_path / "Demo.cs"
    original = "class Demo\n{\n}\n"
    path.write_text(original, encoding="utf-8")
    router = _router(tmp_path)
    ctx = router.disk.get_context(str(path))

    applied = router.apply(ctx, "class Demo\n{\n    int x = 1;\n}\n")
    assert applied.ok

    restored = router.restore(ctx, original, applied.hash)

    assert restored.ok
    assert path.read_text(encoding="utf-8") == original


def test_ide_detection_and_dirty_titles():
    assert detect_ide("idea64.exe") == "jetbrains"
    assert detect_ide("eclipse.exe") == "eclipse"
    assert detect_ide("netbeans64.exe") == "netbeans"
    assert detect_ide("ssms.exe") == "ssms"
    assert detect_ide("devenv.exe") == "visualstudio"
    assert detect_ide("code.exe") == "vscode"
    assert detect_ide("antigravity.exe") == "antigravity"

    assert title_is_dirty("* Demo.cs - IntelliJ IDEA")
    assert title_is_dirty("● Demo.cs - Visual Studio Code")
    assert not title_is_dirty("Demo.cs - IntelliJ IDEA")
