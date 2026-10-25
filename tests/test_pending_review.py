"""Non-GUI tests for the pending-change review window and bot wiring."""

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

import bot
from pending_review import (
    MAX_LINE_CHARS,
    build_rows,
    preview_for_item,
    preview_text,
    read_file_text,
    result_message,
    saved_state_warning,
    shorten_path,
)


def _item(**overrides):
    values = {
        "id": "pc_1",
        "created_at": 100.0,
        "file_path": "C:/work/Demo.cs",
        "base_hash": "base",
        "proposed_text": "class Demo\n{\n}\n",
        "reason": "syntax gate",
        "source": "disk",
        "diagnostics": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_rows_sorts_newest_first_and_uses_the_file_name():
    rows = build_rows(
        [
            _item(id="old", created_at=10.0, file_path="C:/work/Old.cs"),
            _item(id="new", created_at=20.0, file_path="C:/work/New.cs"),
        ]
    )

    assert [row.id for row in rows] == ["new", "old"]
    assert rows[0].file_name == "New.cs"
    assert rows[0].reason == "syntax gate"
    assert rows[0].source == "disk"


def test_read_file_text_handles_bom_and_missing_files(tmp_path):
    target = tmp_path / "Demo.cs"
    target.write_bytes(b"\xef\xbb\xbfclass Demo {}\n")

    assert read_file_text(str(target)) == "class Demo {}\n"
    assert read_file_text(str(tmp_path / "missing.cs")) is None


def test_preview_text_shows_added_and_removed_lines():
    item = _item(proposed_text="class Demo\n{\n    int y = 2;\n}\n")
    current = "class Demo\n{\n    int x = 1;\n}\n"

    preview = preview_text(item, current)

    assert "--- current file" in preview
    assert "+++ proposed" in preview
    assert "-    int x = 1;" in preview
    assert "+    int y = 2;" in preview


def test_preview_text_handles_unreadable_files_and_unchanged_text():
    item = _item(proposed_text="class Demo\n{\n}\n")

    missing = preview_text(item, None)
    assert "not readable" in missing
    assert "class Demo" in missing

    assert preview_text(item, item.proposed_text) == (
        "No textual differences from the current file."
    )


def test_preview_text_truncates_very_long_output():
    item = _item(proposed_text="\n".join(f"line {i}" for i in range(12)))
    preview = preview_text(item, None, max_lines=3)

    assert "preview truncated" in preview
    assert len(preview.splitlines()) == 4

    long_item = _item(proposed_text="x" * (MAX_LINE_CHARS + 50))
    long_preview = preview_text(long_item, None)
    assert "..." in long_preview
    assert max(len(line) for line in long_preview.splitlines()) <= (
        MAX_LINE_CHARS + 2 + 3
    )


def test_preview_for_item_diffs_against_the_file_on_disk(tmp_path):
    target = tmp_path / "Demo.cs"
    target.write_text("class Demo\n{\n    int x = 1;\n}\n", encoding="utf-8")
    item = _item(
        file_path=str(target),
        proposed_text="class Demo\n{\n    int x = 1;\n    int y = 2;\n}\n",
    )

    preview = preview_for_item(item)

    assert "+    int y = 2;" in preview


def test_result_message_covers_apply_dismiss_conflict_and_unknown_error():
    assert result_message({"ok": True, "applied": True}, True) == (
        True,
        "Change applied.",
    )
    assert result_message({"ok": True, "accepted": False}, False) == (
        True,
        "Change dismissed.",
    )

    ok, message = result_message(
        {"ok": False, "conflict": True, "error": "file changed"}, True
    )
    assert ok is False
    assert "Review the file again" in message

    ok, message = result_message(None, True)
    assert ok is False
    assert "unknown error" in message


def test_saved_state_warning_only_trusts_adapters_that_prove_it():
    for source in ("vscode", "antigravity", "visualstudio"):
        assert saved_state_warning(source) == ""
    for source in ("eclipse", "jetbrains", "disk", ""):
        assert "does not report unsaved state" in saved_state_warning(source)


def test_shorten_path_keeps_short_paths_and_elides_long_ones():
    assert shorten_path("C:/work/Demo.cs") == "C:/work/Demo.cs"

    long_path = "C:/work/" + ("nested/" * 30) + "Demo.cs"
    shortened = shorten_path(long_path, max_chars=60)

    assert len(shortened) == 60
    assert "..." in shortened
    assert shortened.startswith("C:/work/")
    assert shortened.endswith("Demo.cs")


class _BlockingWindow:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self):
        self.entered.set()
        self.release.wait(5)


class _ReviewHost:
    _run_pending_review_window = bot.ScreenBot._run_pending_review_window

    def __init__(self):
        self._review_lock = threading.RLock()
        self._review_window_open = False
        self.notices = []
        self.window = _BlockingWindow()
        self.builds = 0

    def _notify_guard(self, message):
        self.notices.append(message)

    def _build_pending_review_window(self):
        self.builds += 1
        return self.window


def _wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_pending_review_window_is_single_instance_and_reusable():
    host = _ReviewHost()

    bot.ScreenBot.open_pending_review(host)
    assert host.window.entered.wait(3)
    assert host._review_window_open is True

    bot.ScreenBot.open_pending_review(host)
    assert host.notices == ["The pending changes window is already open."]
    assert host.builds == 1

    host.window.release.set()
    assert _wait_until(lambda: host._review_window_open is False)

    host.window = _BlockingWindow()
    bot.ScreenBot.open_pending_review(host)
    assert host.window.entered.wait(3)
    assert host.builds == 2
    host.window.release.set()
    assert _wait_until(lambda: host._review_window_open is False)


def test_pending_review_failure_clears_the_single_instance_flag():
    host = _ReviewHost()

    def fail():
        raise RuntimeError("tkinter is not available")

    host._build_pending_review_window = fail

    bot.ScreenBot.open_pending_review(host)

    assert _wait_until(lambda: host._review_window_open is False)
    assert any("Could not open" in notice for notice in host.notices)
