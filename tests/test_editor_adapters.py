"""Contract tests for the editor adapters.

These lock down the properties that keep a bound file safe:
  - an explicit path is never swapped for the resolver's active file,
  - disk writes are hash-checked and preserve the file's newline style,
  - an unverifiable saved state queues instead of writing,
  - Visual Studio edits are minimal, undoable as one action, and reconnect,
  - restore() uses the hash the successful apply reported.
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

import editor_adapters
from editor_adapters import (
    DiskAdapter,
    EditorRouter,
    VisualStudioAdapter,
    assess_disk_dirty,
    detect_ide,
    sha256_text,
    title_is_dirty,
)


def _router(tmp_path, resolver_target=None):
    bridge = SimpleNamespace(live_hosts=lambda: [])
    return EditorRouter(bridge, resolver=lambda: resolver_target)


def _visual_studio(dte, **kwargs):
    return VisualStudioAdapter(
        connect=lambda: dte,
        initialize_com=lambda: True,
        clock=lambda: 0.0,
        **kwargs,
    )


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
    monkeypatch.setattr(editor_adapters, "_list_window_info", lambda: [])
    monkeypatch.setattr(router.visual_studio, "available", lambda: False)

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
    assert title_is_dirty("Demo.cs* - Microsoft Visual Studio")
    assert not title_is_dirty("Demo.cs - IntelliJ IDEA")


def test_ide_detection_trusts_the_process_and_anchors_title_fallbacks():
    # A browser tab is never an IDE, whatever its title says.
    assert detect_ide("chrome.exe", "Eclipse tutorial - Google Chrome") == "unknown"
    assert detect_ide("chrome.exe", "Demo.java - Eclipse IDE") == "unknown"
    assert detect_ide("msedge.exe", "JetBrains keynote - Microsoft Edge") == "unknown"

    # Bare phrases are not document-window titles.
    assert detect_ide("", "Eclipse tutorial") == "unknown"
    assert detect_ide("", "JetBrains keynote") == "unknown"

    # Title fallback only when the image name is unknown, and only for the
    # trailing IDE segment of a document window.
    assert detect_ide("", "Demo.java - Eclipse IDE") == "eclipse"
    assert detect_ide("", "Demo.cs - Microsoft Visual Studio") == "visualstudio"
    assert detect_ide("", "Demo.java – IntelliJ IDEA 2024.1") == "jetbrains"
    assert detect_ide("java.exe", "Demo.java - IntelliJ IDEA 2024.1") == "jetbrains"


def test_disk_dirty_assessment_is_tri_state():
    # No editor running means no buffer can hold unsaved changes.
    assert assess_disk_dirty("C:/work/Demo.cs", []) == (False, "")

    # VS Code titles are trusted both for dirty and clean.
    assert assess_disk_dirty(
        "C:/work/Demo.cs", [("● Demo.cs - Visual Studio Code", "code.exe")]
    )[0] is True
    assert assess_disk_dirty(
        "C:/work/Demo.cs", [("Demo.cs - Visual Studio Code", "code.exe")]
    )[0] is False

    # JetBrains/Eclipse do not expose saved state in the window title.
    dirty, reason = assess_disk_dirty(
        "C:/work/Demo.cs", [("Demo.cs - IntelliJ IDEA", "idea64.exe")]
    )
    assert dirty is None
    assert "does not report" in reason

    # A different file in the editor means the target may be a dirty
    # background tab, so it is not safe to write.
    dirty, reason = assess_disk_dirty(
        "C:/work/Demo.cs", [("Other.cs - IntelliJ IDEA", "idea64.exe")]
    )
    assert dirty is None
    assert "background tab" in reason

    # A browser with a similar title proves nothing and does not count as an
    # editor window.
    assert assess_disk_dirty(
        "C:/work/Demo.cs", [("Demo.cs - Eclipse IDE - Google Chrome", "chrome.exe")]
    )[0] is False


def test_router_holds_a_disk_write_when_saved_state_is_unknown(
    tmp_path, monkeypatch
):
    path = tmp_path / "Demo.cs"
    original = "class Demo\n{\n}\n"
    path.write_text(original, encoding="utf-8")
    router = _router(tmp_path)
    monkeypatch.setattr(
        editor_adapters,
        "_foreground_window_info",
        lambda: (1, "Demo.cs - IntelliJ IDEA", "idea64.exe"),
    )
    monkeypatch.setattr(
        editor_adapters,
        "_list_window_info",
        lambda: [("Demo.cs - IntelliJ IDEA", "idea64.exe")],
    )
    monkeypatch.setattr(router.visual_studio, "available", lambda: False)

    ctx = router.get_context(file_path=str(path))
    assert ctx is not None
    assert ctx.dirty is None
    assert ctx.can_apply is False
    assert "does not report" in ctx.hold_reason

    proposed = "class Demo\n{\n    int x = 1;\n}\n"
    blocked = router.apply(ctx, proposed)
    assert not blocked.ok
    assert path.read_text(encoding="utf-8") == original

    # Accepting a review item is explicit, so it may bypass the unknown hold.
    accepted = router.apply(ctx, proposed, explicit=True)
    assert accepted.ok
    assert path.read_text(encoding="utf-8") == proposed


def test_router_writes_when_a_trusted_editor_proves_the_buffer_is_saved(
    tmp_path, monkeypatch
):
    path = tmp_path / "Demo.cs"
    path.write_text("class Demo\n{\n}\n", encoding="utf-8")
    router = _router(tmp_path)
    monkeypatch.setattr(
        editor_adapters, "_foreground_window_info", lambda: (1, "Lecture", "chrome.exe")
    )
    monkeypatch.setattr(
        editor_adapters,
        "_list_window_info",
        lambda: [("Demo.cs - Visual Studio Code", "code.exe")],
    )
    monkeypatch.setattr(router.visual_studio, "available", lambda: False)

    ctx = router.get_context(file_path=str(path))
    assert ctx is not None
    assert ctx.dirty is False
    assert ctx.can_apply is True
    assert ctx.hold_reason == ""

    result = router.apply(ctx, "class Demo\n{\n    int x = 1;\n}\n")
    assert result.ok
    assert "int x = 1;" in path.read_text(encoding="utf-8")


def test_disk_apply_refuses_a_context_that_cannot_be_verified(tmp_path):
    path = tmp_path / "Demo.cs"
    original = "class Demo\n{\n}\n"
    path.write_text(original, encoding="utf-8")
    adapter = DiskAdapter(lambda: path)
    ctx = adapter.get_context(str(path))
    ctx.dirty = None
    ctx.can_apply = False
    ctx.hold_reason = "saved state unknown"

    result = adapter.apply(ctx, "class Demo\n{\n    int x = 1;\n}\n")

    assert not result.ok
    assert "unknown" in result.error
    assert path.read_text(encoding="utf-8") == original


class _FakeEditPoint:
    def __init__(self, text_document, offset=0):
        self.text_document = text_document
        self.offset = offset

    def GetText(self, end):
        return self.text_document.text[self.offset:end.offset]

    def MoveToAbsoluteOffset(self, offset):
        offset = int(offset) - 1
        if not 0 <= offset <= len(self.text_document.text):
            raise ValueError(f"offset out of range: {offset}")
        self.offset = offset

    def MoveToLineAndOffset(self, line, column):
        text = self.text_document.text
        line_starts = [0]
        for index, character in enumerate(text):
            if character == "\n":
                line_starts.append(index + 1)
        if not 1 <= line <= len(line_starts):
            raise ValueError(f"line out of range: {line}")
        start = line_starts[line - 1]
        if line < len(line_starts):
            end = line_starts[line] - 1
            if end > start and text[end - 1] == "\r":
                end -= 1
        else:
            end = len(text)
        if not 1 <= column <= end - start + 1:
            raise ValueError(f"column out of range: {column}")
        self.offset = start + column - 1

    def Delete(self, end):
        self.text_document.deleted.append(self.text_document.text[self.offset:end.offset])
        self.text_document.text = (
            self.text_document.text[:self.offset] + self.text_document.text[end.offset:]
        )

    def Insert(self, text):
        self.text_document.inserted.append(text)
        self.text_document.text = (
            self.text_document.text[:self.offset] + text + self.text_document.text[self.offset:]
        )
        self.offset += len(text)


class _FakePoint:
    def __init__(self, text_document, offset):
        self.text_document = text_document
        self.offset = offset

    def CreateEditPoint(self):
        return _FakeEditPoint(self.text_document, self.offset)


class _FakeTextDocument:
    def __init__(self, text):
        self.text = text
        self.inserted = []
        self.deleted = []

    @property
    def StartPoint(self):
        return _FakePoint(self, 0)

    @property
    def EndPoint(self):
        return _FakePoint(self, len(self.text))


class _FakeDocument:
    def __init__(self, path, text, save_error=None):
        self.FullName = str(path)
        self.Saved = True
        self.text_document = _FakeTextDocument(text)
        self.save_error = save_error
        self.save_calls = 0

    def Object(self, kind):
        assert kind == "TextDocument"
        return self.text_document

    def Save(self):
        self.save_calls += 1
        if self.save_error is not None:
            raise self.save_error
        self.Saved = True


class _FakeUndoContext:
    def __init__(self, is_open=False):
        self.IsOpen = is_open
        self.opened = 0
        self.closed = 0
        self.aborted = 0

    def Open(self, name, strict=False):
        if self.IsOpen:
            raise RuntimeError("undo context already open")
        self.IsOpen = True
        self.opened += 1

    def SetAborted(self):
        self.aborted += 1

    def Close(self):
        self.IsOpen = False
        self.closed += 1


class _FakeDte:
    def __init__(self, document, undo=None):
        self.Documents = [document]
        self.ActiveDocument = document
        self.UndoContext = undo or _FakeUndoContext()

    @property
    def ToolWindows(self):
        raise RuntimeError("no error list in tests")


def test_dte_line_column_maps_crlf_boundaries():
    text = "alpha\r\nbeta\r\ngamma\r\n"
    assert editor_adapters._dte_line_column(text, 0) == (1, 1)
    assert editor_adapters._dte_line_column(text, 2) == (1, 3)
    assert editor_adapters._dte_line_column(text, 5) == (1, 6)
    assert editor_adapters._dte_line_column(text, 7) == (2, 1)
    assert editor_adapters._dte_line_column(text, len(text)) == (4, 1)

    no_trailing = text.rstrip("\r\n")
    assert editor_adapters._dte_line_column(
        no_trailing, len(no_trailing)
    ) == (3, 6)


def test_visual_studio_apply_appends_at_the_end_of_a_newline_terminated_file(tmp_path):
    path = tmp_path / "Demo.cs"
    original = "class Demo\r\n{\r\n}\r\n"
    path.write_text(original, encoding="utf-8", newline="")
    document = _FakeDocument(path, original)
    dte = _FakeDte(document)
    adapter = _visual_studio(dte)
    ctx = adapter.get_context(file_path=str(path))

    proposed = original + "// ScreenBot\r\n"
    result = adapter.apply(ctx, proposed)

    assert result.ok and result.applied
    assert document.text_document.text == proposed
    assert document.text_document.inserted == ["// ScreenBot\r\n"]
    assert document.text_document.deleted == []


def test_visual_studio_apply_appends_without_a_trailing_newline(tmp_path):
    path = tmp_path / "Demo.cs"
    original = "class Demo\r\n{\r\n}"
    path.write_text(original, encoding="utf-8", newline="")
    document = _FakeDocument(path, original)
    dte = _FakeDte(document)
    adapter = _visual_studio(dte)
    ctx = adapter.get_context(file_path=str(path))

    proposed = original + "\r\n// ScreenBot\r\n"
    result = adapter.apply(ctx, proposed)

    assert result.ok and result.applied
    assert document.text_document.text == proposed
    assert document.text_document.inserted == ["}\r\n// ScreenBot\r\n"]


def test_visual_studio_apply_is_minimal_and_undoable_as_one_action(tmp_path):
    path = tmp_path / "Demo.cs"
    original = "class Demo\n{\n}\n"
    path.write_text(original, encoding="utf-8")
    document = _FakeDocument(path, original)
    dte = _FakeDte(document)
    adapter = _visual_studio(dte)

    ctx = adapter.get_context(file_path=str(path))
    assert ctx is not None
    assert ctx.dirty is False

    proposed = "class Demo\n{\n    int x = 1;\n}\n"
    result = adapter.apply(ctx, proposed)

    assert result.ok and result.applied
    assert document.text_document.text == proposed
    assert document.text_document.inserted == ["    int x = 1;\n"]
    assert document.text_document.deleted == []
    assert document.save_calls == 1
    assert dte.UndoContext.opened == 1
    assert dte.UndoContext.closed == 1
    assert dte.UndoContext.aborted == 0
    assert result.hash == sha256_text(proposed)


def test_visual_studio_apply_aborts_its_undo_context_when_saving_fails(tmp_path):
    path = tmp_path / "Demo.cs"
    original = "class Demo\n{\n}\n"
    path.write_text(original, encoding="utf-8")
    document = _FakeDocument(path, original, save_error=OSError("read-only file"))
    dte = _FakeDte(document)
    adapter = _visual_studio(dte)
    ctx = adapter.get_context(file_path=str(path))

    result = adapter.apply(
        ctx, "class Demo\n{\n    int x = 1;\n}\n"
    )

    assert not result.ok
    assert "read-only" in result.error
    assert dte.UndoContext.aborted == 1
    assert dte.UndoContext.closed == 1


def test_visual_studio_apply_never_joins_another_undo_context(tmp_path):
    path = tmp_path / "Demo.cs"
    original = "class Demo\n{\n}\n"
    path.write_text(original, encoding="utf-8")
    document = _FakeDocument(path, original)
    undo = _FakeUndoContext(is_open=True)
    dte = _FakeDte(document, undo=undo)
    adapter = _visual_studio(dte)
    ctx = adapter.get_context(file_path=str(path))

    result = adapter.apply(ctx, "class Demo\n{\n    int x = 1;\n}\n")

    assert not result.ok
    assert "undo operation open" in result.error
    assert undo.opened == 0
    assert undo.closed == 0
    assert document.text_document.text == original


class _BrokenDte:
    @property
    def Documents(self):
        raise RuntimeError("Visual Studio restarted")


def test_visual_studio_reconnects_after_a_com_failure(tmp_path):
    path = tmp_path / "Demo.cs"
    path.write_text("class Demo\n{\n}\n", encoding="utf-8")
    document = _FakeDocument(path, "class Demo\n{\n}\n")
    responses = [_BrokenDte(), _FakeDte(document)]
    calls = []

    def connect():
        calls.append(1)
        return responses.pop(0)

    adapter = VisualStudioAdapter(
        connect=connect,
        initialize_com=lambda: True,
        clock=lambda: 0.0,
    )

    assert adapter.get_context(file_path=str(path)) is None
    ctx = adapter.get_context(file_path=str(path))

    assert ctx is not None
    assert len(calls) == 2


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_visual_studio_retries_connection_with_backoff():
    clock = _Clock()
    calls = []

    def connect():
        calls.append(clock.now)
        if len(calls) < 3:
            raise RuntimeError("Visual Studio is not running")
        return object()

    adapter = VisualStudioAdapter(
        connect=connect,
        initialize_com=lambda: True,
        clock=clock,
        retry_initial=2.0,
        retry_max=5.0,
    )

    assert adapter.available() is False
    assert calls == [0.0]

    clock.now = 1.9
    assert adapter.available() is False
    assert calls == [0.0]

    clock.now = 2.0
    assert adapter.available() is False
    assert calls == [0.0, 2.0]

    clock.now = 5.9
    assert adapter.available() is False
    assert calls == [0.0, 2.0]

    clock.now = 6.0
    assert adapter.available() is True
    assert calls == [0.0, 2.0, 6.0]

    clock.now = 100.0
    assert adapter.available() is True
    assert calls == [0.0, 2.0, 6.0]


def test_visual_studio_com_and_connection_are_per_thread():
    main_thread = threading.get_ident()
    initialized = []
    connected = []

    def initialize():
        initialized.append(threading.get_ident())
        return True

    def connect():
        connected.append(threading.get_ident())
        return object()

    adapter = VisualStudioAdapter(connect=connect, initialize_com=initialize)

    assert adapter.available() is True
    assert adapter.available() is True
    assert initialized == [main_thread]
    assert connected == [main_thread]

    result = []
    worker = threading.Thread(target=lambda: result.append(adapter.available()))
    worker.start()
    worker.join()

    assert result == [True]
    assert len(initialized) == 2
    assert len(connected) == 2
    assert initialized[1] == connected[1] != main_thread


def test_visual_studio_18_progid_is_tried_before_older_versions(monkeypatch):
    import win32com.client

    attempted = []
    sentinel = object()

    def fake_get_active(progid):
        attempted.append(progid)
        if progid == "VisualStudio.DTE.17.0":
            return sentinel
        raise RuntimeError("not running")

    monkeypatch.setattr(win32com.client, "GetActiveObject", fake_get_active)

    assert editor_adapters._connect_active_dte() is sentinel
    assert attempted == ["VisualStudio.DTE.18.0", "VisualStudio.DTE.17.0"]
