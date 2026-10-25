"""
Regression tests for the live-class code-follower safety path.

Bugs these lock down (all previously claimed in the engineering record but NOT wired
into the running code):
  - ISSUE #11: the two-frame temporal stability gate existed but was never called, so
    half-rendered video frames were sent to the vision model (duplicated/garbled lines).
  - ISSUE #06/#07: the foreground HWND was never captured at snapshot time, so the
    focus guard in instant_paste() was permanently disarmed.
  - ISSUE #13: validate_code_to_inject() was never called on the live injection path.
  - Ctrl+Alt+Z undo never worked because save_snapshot() was never called.
  - Direct File Sync wrote the file AND typed into the foreground window, dumping code
    into Teams/YouTube chat when the lecture player happened to have focus.
"""

import json
import sys
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

import bot
import capture


def _patterned_frame() -> Image.Image:
    """A non-blank frame: solid colors are (correctly) rejected before the AI call."""
    img = Image.new("RGB", (64, 48), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    for x in range(0, 64, 4):
        draw.line((x, 0, x, 47), fill=(10, 10, 10))
    return img


class _DummyClipboard:
    def copy(self, _text):
        return None

    def paste(self):
        return ""


class _FakeUser32:
    def __init__(self, hwnd):
        self.hwnd = hwnd

    def GetForegroundWindow(self):
        return self.hwnd


@pytest.fixture
def screen_bot(monkeypatch):
    monkeypatch.setattr(bot, "pyperclip", _DummyClipboard())
    sb = bot.ScreenBot()
    sb.tray_icon = None
    sb.config["sound_feedback"] = False
    return sb


# ---------------------------------------------------------------------------
# Two-frame temporal stability gate (ISSUE #11)
# ---------------------------------------------------------------------------

def test_stable_frame_gate_rejects_animating_video(monkeypatch):
    frames = [
        Image.new("RGB", (64, 48), (0, 0, 0)),
        Image.new("RGB", (64, 48), (255, 255, 255)),
    ]

    monkeypatch.setattr(capture, "capture_screen_image", lambda **kwargs: frames.pop(0))
    monkeypatch.setattr(capture.time, "sleep", lambda _s: None)

    frame, is_stable = capture.capture_stable_frame(mode="lecture")

    assert frame is not None
    assert is_stable is False


def test_stable_frame_gate_accepts_identical_frames(monkeypatch):
    img = Image.new("RGB", (64, 48), (12, 34, 56))

    monkeypatch.setattr(capture, "capture_screen_image", lambda **kwargs: img.copy())
    monkeypatch.setattr(capture.time, "sleep", lambda _s: None)

    frame, is_stable = capture.capture_stable_frame(mode="lecture")

    assert frame is not None
    assert is_stable is True


def test_process_code_shadow_defers_unstable_frame(screen_bot, monkeypatch):
    """An unstable frame must never reach the vision model."""
    img = Image.new("RGB", (32, 32), (1, 2, 3))
    monkeypatch.setattr(bot, "capture_stable_frame", lambda **kwargs: (img, False))

    calls = []
    monkeypatch.setattr(
        screen_bot.solver,
        "extract_lecture_code_structured",
        lambda *a, **k: calls.append(1) or "",
    )

    screen_bot.process_code_shadow(is_auto=True)

    assert calls == []


def test_active_line_hold_is_opt_in_for_auto_scans_only(screen_bot, monkeypatch):
    """Auto scans hold a half-typed caret line; manual and force syncs do not."""
    img = _patterned_frame()
    monkeypatch.setattr(bot, "capture_stable_frame", lambda **kwargs: (img, True))
    monkeypatch.setattr(bot, "encode_image_to_bytes", lambda *a, **k: b"png-bytes")
    monkeypatch.setattr(bot, "user32", _FakeUser32(777))
    monkeypatch.setattr(screen_bot.frame_archiver, "archive_frame", lambda *a, **k: None)
    monkeypatch.setattr(screen_bot.occlusion_detector, "detect_occlusions", lambda *a, **k: [])
    monkeypatch.setattr(
        screen_bot.solver, "extract_lecture_code_structured",
        lambda *a, **k: _single_frame_envelope("int x = 1;"),
    )

    seen = []

    def spy(_lines, hold_active_line=False):
        seen.append(hold_active_line)
        return [], []

    monkeypatch.setattr(screen_bot.consensus_tracker, "feed_frame_lines", spy)
    screen_bot.last_lecture_frame = None

    screen_bot.process_code_shadow(is_auto=True)                   # interval auto-scan
    screen_bot.process_code_shadow(is_auto=False)                  # manual hotkey
    screen_bot.process_code_shadow(is_auto=True, force_full=True)  # force full sync

    assert seen == [True, False, False]


# ---------------------------------------------------------------------------
# Lecturer edits are review items, never silent skips or automatic writes
# ---------------------------------------------------------------------------

def _single_frame_envelope(code: str) -> str:
    return json.dumps({
        "tab": "Demo.cs",
        "language": "csharp",
        "overlay": False,
        "eof_visible": True,
        "code": code.strip("\n"),
    })


def test_process_code_shadow_queues_lecturer_edit(screen_bot, monkeypatch, tmp_path):
    """An in-place lecturer edit must become a review item; nothing is written
    and no caret/clipboard delivery happens."""
    from editor_adapters import EditorContext, sha256_text

    base = "class Demo\n{\n    int x = 1;\n}\n"
    edited = base.replace("int x = 1;", "int x = 2;")
    target = tmp_path / "Demo.cs"
    target.write_text(base, encoding="utf-8")
    ctx = EditorContext(
        file_path=str(target),
        text=base,
        language="csharp",
        dirty=False,
        source="disk",
        hash=sha256_text(base),
        can_apply=True,
    )

    img = _patterned_frame()
    monkeypatch.setattr(bot, "capture_stable_frame", lambda **kwargs: (img, True))
    monkeypatch.setattr(bot, "encode_image_to_bytes", lambda *a, **k: b"png-bytes")
    monkeypatch.setattr(bot, "user32", _FakeUser32(777))
    monkeypatch.setattr(screen_bot, "_editor_context_for", lambda _target: ctx)
    monkeypatch.setattr(screen_bot.frame_archiver, "archive_frame", lambda *a, **k: None)
    monkeypatch.setattr(screen_bot.occlusion_detector, "detect_occlusions", lambda *a, **k: [])
    monkeypatch.setattr(
        screen_bot.solver, "extract_lecture_code_structured",
        lambda *a, **k: _single_frame_envelope(edited),
    )
    monkeypatch.setattr(screen_bot.bridge.pending, "list", lambda: [])

    screen_bot.direct_file_sync = False
    screen_bot.target_file_path = str(target)
    screen_bot.last_lecture_frame = None
    screen_bot.consensus_tracker.reset()
    screen_bot.consensus_tracker.required_observations = 1
    doc = screen_bot.doc_registry.get("Demo.cs")
    doc.seed_baseline(base)
    screen_bot.master_tracker.master_code = base
    screen_bot.last_code_shadow = base

    delivered, queued = [], []
    monkeypatch.setattr(
        screen_bot, "inject_lecture_code", lambda *a, **k: delivered.append(1) or True
    )
    monkeypatch.setattr(
        screen_bot, "_queue_pending",
        lambda context, proposed, reason, diagnostics=None:
            queued.append((proposed, reason)) or object(),
    )

    screen_bot.process_code_shadow(is_auto=False)   # persistence gate: 1/2
    screen_bot.process_code_shadow(is_auto=False)   # confirmed -> review

    assert delivered == []
    assert target.read_text(encoding="utf-8") == base
    assert len(queued) == 1
    proposed, reason = queued[0]
    assert "lecturer" in reason
    assert "int x = 2;" in proposed
    assert "int x = 1;" not in proposed
    assert screen_bot.master_tracker.master_code == base


def test_accepted_review_resyncs_and_drops_unsafe_old_undo_lineage(
    screen_bot, monkeypatch, tmp_path
):
    from editor_adapters import sha256_text
    from editor_bridge import PendingChange

    base = "class Demo\n{\n    int x = 1;\n}\n"
    edited = base.replace("int x = 1;", "int x = 2;")
    target = tmp_path / "Demo.cs"
    target.write_text(base, encoding="utf-8")
    item = PendingChange(
        id="pc_review", created_at=time.time(), file_path=str(target),
        base_hash=sha256_text(base), proposed_text=edited,
        reason="lecturer 1 edited block(s)", source="disk",
    )
    monkeypatch.setattr(screen_bot.bridge.pending, "get", lambda _item_id: item)
    monkeypatch.setattr(
        screen_bot.editor_router, "resolve_pending",
        lambda _item_id, _accept: {
            "ok": True, "applied": True, "text": edited, "hash": sha256_text(edited)
        },
    )

    doc = screen_bot.doc_registry.get("Demo.cs")
    doc.seed_baseline(base)
    screen_bot.master_tracker.master_code = base
    screen_bot.last_code_shadow = base
    screen_bot.master_tracker.snapshots.append(base)

    outcome = screen_bot._resolve_pending_change("pc_review", True)

    assert outcome["ok"] is True
    assert doc.text == edited
    assert screen_bot.master_tracker.master_code == edited
    assert screen_bot.last_code_shadow == edited
    assert screen_bot.master_tracker.snapshots == []   # new lineage starts at the file
    assert screen_bot._pending_review_exists(str(target), edited) is False


def test_dismissed_review_is_not_requeued_every_frame(
    screen_bot, monkeypatch, tmp_path
):
    from editor_adapters import sha256_text
    from editor_bridge import PendingChange

    base = "class Demo\n{\n    int x = 1;\n}\n"
    edited = base.replace("int x = 1;", "int x = 2;")
    target = tmp_path / "Demo.cs"
    target.write_text(base, encoding="utf-8")
    item = PendingChange(
        id="pc_review", created_at=time.time(), file_path=str(target),
        base_hash=sha256_text(base), proposed_text=edited,
        reason="lecturer 1 edited block(s)", source="disk",
    )
    monkeypatch.setattr(screen_bot.bridge.pending, "get", lambda _item_id: item)
    monkeypatch.setattr(
        screen_bot.editor_router, "resolve_pending",
        lambda _item_id, _accept: {"ok": True, "applied": False},
    )

    screen_bot._resolve_pending_change("pc_review", False)

    assert target.read_text(encoding="utf-8") == base
    assert screen_bot._pending_review_exists(str(target), edited) is True


# ---------------------------------------------------------------------------
# Snapshot-time HWND binding (ISSUE #06/#07)
# ---------------------------------------------------------------------------

def test_process_code_shadow_binds_snapshot_hwnd(screen_bot, monkeypatch):
    img = _patterned_frame()
    monkeypatch.setattr(bot, "capture_stable_frame", lambda **kwargs: (img, True))
    monkeypatch.setattr(bot, "encode_image_to_bytes", lambda *a, **k: b"png-bytes")
    monkeypatch.setattr(bot, "user32", _FakeUser32(777))
    monkeypatch.setattr(screen_bot, "_editor_context_for", lambda target: None)
    monkeypatch.setattr(screen_bot.frame_archiver, "archive_frame", lambda *a, **k: None)
    monkeypatch.setattr(screen_bot.occlusion_detector, "detect_occlusions", lambda *a, **k: [])

    envelope = json.dumps({
        "tab": "Demo.cs",
        "language": "csharp",
        "overlay": False,
        "eof_visible": False,
        "code": "class Demo\n{\n    int x = 1;\n}",
    })
    monkeypatch.setattr(
        screen_bot.solver,
        "extract_lecture_code_structured",
        lambda *a, **k: envelope,
    )

    screen_bot.direct_file_sync = False
    screen_bot.target_file_path = None
    screen_bot.last_lecture_frame = None
    screen_bot.consensus_tracker.reset()
    screen_bot.consensus_tracker.required_observations = 1

    captured = {}

    def fake_inject(code_text, is_delta=True, full_master="", target_hwnd=None):
        captured.update(code=code_text, hwnd=target_hwnd, master=full_master)
        return True

    monkeypatch.setattr(screen_bot, "inject_lecture_code", fake_inject)

    screen_bot.process_code_shadow(is_auto=True)

    assert captured["hwnd"] == 777
    assert "int x = 1;" in captured["code"]
    assert "class Demo" in captured["master"]


def test_stale_vision_result_is_discarded(screen_bot, monkeypatch):
    """A late Gemini answer describes a frame the lecture has moved past."""
    img = _patterned_frame()
    monkeypatch.setattr(bot, "capture_stable_frame", lambda **kwargs: (img, True))
    monkeypatch.setattr(bot, "encode_image_to_bytes", lambda *a, **k: b"png-bytes")
    monkeypatch.setattr(bot, "user32", _FakeUser32(777))
    monkeypatch.setattr(screen_bot, "_editor_context_for", lambda target: None)
    monkeypatch.setattr(screen_bot.frame_archiver, "archive_frame", lambda *a, **k: None)
    monkeypatch.setattr(screen_bot.occlusion_detector, "detect_occlusions", lambda *a, **k: [])

    envelope = json.dumps({
        "tab": "Demo.cs", "language": "csharp", "overlay": False,
        "eof_visible": False, "code": "int x = 1;",
    })

    def slow_extraction(*a, **k):
        time.sleep(0.05)
        return envelope

    monkeypatch.setattr(screen_bot.solver, "extract_lecture_code_structured", slow_extraction)
    screen_bot.config["max_frame_age_seconds"] = 0.01
    screen_bot.consensus_tracker.required_observations = 1

    delivered = []
    monkeypatch.setattr(
        screen_bot, "inject_lecture_code", lambda *a, **k: delivered.append(True) or True
    )

    screen_bot.process_code_shadow(is_auto=True)

    assert delivered == []
    assert screen_bot.last_lecture_frame is None


def test_caret_guard_blocks_focus_change(screen_bot, monkeypatch):
    monkeypatch.setattr(bot, "user32", _FakeUser32(999))
    monkeypatch.setattr(bot, "is_browser_or_meeting_window", lambda _hwnd: False)

    assert screen_bot._caret_target_safe(111, "paste") is False


def test_caret_guard_blocks_browser_and_meeting_windows(screen_bot, monkeypatch):
    monkeypatch.setattr(bot, "user32", _FakeUser32(111))
    monkeypatch.setattr(bot, "is_browser_or_meeting_window", lambda _hwnd: True)

    assert screen_bot._caret_target_safe(111, "paste") is False


def test_caret_guard_allows_matching_editor(screen_bot, monkeypatch):
    monkeypatch.setattr(bot, "user32", _FakeUser32(111))
    monkeypatch.setattr(bot, "is_browser_or_meeting_window", lambda _hwnd: False)

    assert screen_bot._caret_target_safe(111, "paste") is True


# ---------------------------------------------------------------------------
# Direct File Sync semantics
# ---------------------------------------------------------------------------

def test_direct_file_sync_writes_disk_snapshots_and_skips_caret(screen_bot, monkeypatch, tmp_path):
    target = tmp_path / "Demo.cs"
    old_master = "class Demo\n{\n}\n"
    new_master = "class Demo\n{\n    int x = 1;\n}\n"
    target.write_text(old_master, encoding="utf-8")

    screen_bot.direct_file_sync = True
    screen_bot.target_file_path = str(target)
    screen_bot.master_tracker.master_code = old_master
    screen_bot.last_code_shadow = old_master

    typed = []
    monkeypatch.setattr(screen_bot, "instant_paste", lambda text, target_hwnd=None: typed.append(text))
    monkeypatch.setattr(screen_bot, "human_type", lambda text, is_turbo=False, target_hwnd=None: typed.append(text))
    monkeypatch.setattr(screen_bot, "get_or_detect_target_file", lambda *a, **k: target)
    monkeypatch.setattr(
        screen_bot,
        "_editor_context_for",
        lambda t: screen_bot.editor_router.disk.get_context(str(t)),
    )

    screen_bot.inject_lecture_code(
        "    int x = 1;", is_delta=True, full_master=new_master, target_hwnd=111
    )

    assert target.read_text(encoding="utf-8") == new_master
    assert typed == []  # zero-keystroke mode: never type into the foreground window
    assert screen_bot.master_tracker.snapshots == [old_master]
    assert screen_bot.master_tracker.master_code == new_master


def test_direct_sync_never_auto_discovers_a_write_target(
    screen_bot, monkeypatch, tmp_path
):
    auto = tmp_path / "Demo.cs"
    auto.write_text("int x = 1;\n", encoding="utf-8")
    screen_bot.direct_file_sync = True
    screen_bot.target_file_path = None
    monkeypatch.setattr(screen_bot, "get_or_detect_target_file", lambda *a, **k: auto)
    monkeypatch.setattr(
        screen_bot,
        "_apply_master_direct",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote an unbound file")),
    )
    messages = []
    monkeypatch.setattr(screen_bot, "_notify_guard", lambda msg: messages.append(msg))

    assert screen_bot.inject_lecture_code(
        "int x = 2;", is_delta=True, full_master="int x = 2;\n"
    ) is False
    assert auto.read_text(encoding="utf-8") == "int x = 1;\n"
    assert messages and "bind" in messages[0].lower()


def test_direct_sync_holds_when_user_removed_a_delivered_line(
    screen_bot, monkeypatch, tmp_path
):
    target = tmp_path / "Demo.cs"
    delivered = "class Demo\n{\n    int x = 1;\n}\n"
    target.write_text("class Demo\n{\n}\n", encoding="utf-8")  # user deleted int x

    screen_bot.direct_file_sync = True
    screen_bot.target_file_path = str(target)
    screen_bot.last_code_shadow = delivered
    monkeypatch.setattr(
        screen_bot,
        "_editor_context_for",
        lambda t: screen_bot.editor_router.disk.get_context(str(t)),
    )
    reasons = []
    monkeypatch.setattr(
        screen_bot,
        "_queue_pending",
        lambda ctx, proposed, reason, diagnostics=None: reasons.append(reason),
    )

    new_master = "class Demo\n{\n    int y = 2;\n}\n"
    assert screen_bot.inject_lecture_code("", is_delta=True, full_master=new_master) is False

    assert target.read_text(encoding="utf-8") == "class Demo\n{\n}\n"
    assert len(reasons) == 1 and "removed" in reasons[0]
    assert screen_bot.last_code_shadow == delivered


def test_direct_sync_queues_instead_of_writing_a_dirty_disk_buffer(
    screen_bot, monkeypatch, tmp_path
):
    from editor_adapters import EditorContext

    target = tmp_path / "Demo.cs"
    target.write_text("class Demo\n{\n}\n", encoding="utf-8")
    ctx = EditorContext(
        file_path=str(target),
        text=target.read_text(encoding="utf-8"),
        language="csharp",
        dirty=True,
        source="disk",
        hash="irrelevant",
        can_apply=False,
    )
    monkeypatch.setattr(screen_bot, "_editor_context_for", lambda _t: ctx)
    monkeypatch.setattr(
        screen_bot.editor_router,
        "apply",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote a dirty buffer")),
    )
    reasons = []
    monkeypatch.setattr(
        screen_bot,
        "_queue_pending",
        lambda ctx, proposed, reason, diagnostics=None: reasons.append(reason),
    )

    applied = screen_bot._apply_master_direct(
        target, "class Demo\n{\n    int x = 1;\n}\n"
    )

    assert applied is False
    assert reasons and "unsaved" in reasons[0]
    assert target.read_text(encoding="utf-8") == "class Demo\n{\n}\n"


def test_direct_sync_queues_an_identical_held_change_only_once(
    screen_bot, monkeypatch, tmp_path
):
    from editor_adapters import EditorContext
    from editor_bridge import PendingStore

    target = tmp_path / "Demo.cs"
    target.write_text("class Demo\n{\n}\n", encoding="utf-8")
    screen_bot.bridge.pending = PendingStore(tmp_path / "pending.json")
    ctx = EditorContext(
        file_path=str(target),
        text=target.read_text(encoding="utf-8"),
        language="csharp",
        dirty=None,
        source="disk",
        hash="irrelevant",
        can_apply=False,
        hold_reason="saved state unknown",
    )
    monkeypatch.setattr(screen_bot, "_editor_context_for", lambda _t: ctx)
    monkeypatch.setattr(screen_bot, "_notify_guard", lambda _msg: None)

    proposal = "class Demo\n{\n    int x = 1;\n}\n"
    assert screen_bot._apply_master_direct(target, proposal) is False
    assert screen_bot._apply_master_direct(target, proposal) is False

    items = screen_bot.bridge.pending.list()
    assert len(items) == 1
    assert "saved state unknown" in items[0].reason


def test_direct_sync_holds_a_syntax_regression(screen_bot, monkeypatch, tmp_path):
    from editor_adapters import EditorContext
    from editor_adapters import sha256_text

    target = tmp_path / "Demo.cs"
    base = "class Demo\n{\n}\n"
    target.write_text(base, encoding="utf-8")
    ctx = EditorContext(
        file_path=str(target),
        text=base,
        language="csharp",
        dirty=False,
        source="disk",
        hash=sha256_text(base),
        can_apply=True,
    )
    monkeypatch.setattr(screen_bot, "_editor_context_for", lambda _t: ctx)
    monkeypatch.setattr(
        screen_bot.editor_router,
        "apply",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("applied invalid syntax")),
    )
    reasons = []
    monkeypatch.setattr(
        screen_bot,
        "_queue_pending",
        lambda ctx, proposed, reason, diagnostics=None: reasons.append(reason),
    )

    applied = screen_bot._apply_master_direct(
        target, "class Demo\n{\n    void Broken( {\n}\n"
    )

    assert applied is False
    assert reasons and "syntax" in reasons[0].lower()
    assert target.read_text(encoding="utf-8") == base


def test_direct_sync_rolls_back_when_diagnostics_worsen(
    screen_bot, monkeypatch, tmp_path
):
    from editor_adapters import ApplyResult, EditorContext
    from editor_adapters import sha256_text

    target = tmp_path / "Demo.cs"
    base = "class Demo\n{\n}\n"
    target.write_text(base, encoding="utf-8")
    ctx = EditorContext(
        file_path=str(target),
        text=base,
        language="csharp",
        dirty=False,
        source="disk",
        hash=sha256_text(base),
        can_apply=True,
        diagnostics=[],
    )
    applied_text = "class Demo\n{\n    int x = 1;\n}\n"
    calls = {"restore": []}

    monkeypatch.setattr(screen_bot, "_editor_context_for", lambda _t: ctx)
    monkeypatch.setattr(
        screen_bot.editor_router,
        "apply",
        lambda *a, **k: ApplyResult(
            ok=True,
            applied=True,
            text=applied_text,
            hash=sha256_text(applied_text),
            diagnostics=[{"file": str(target), "severity": "error", "message": "boom"}],
            source="disk",
        ),
    )

    def fake_restore(context, original_text, applied_hash=""):
        calls["restore"].append((original_text, applied_hash))
        return ApplyResult(ok=True, applied=True, text=original_text, hash=sha256_text(original_text))

    monkeypatch.setattr(screen_bot.editor_router, "restore", fake_restore)
    reasons = []
    monkeypatch.setattr(
        screen_bot,
        "_queue_pending",
        lambda ctx, proposed, reason, diagnostics=None: reasons.append(reason),
    )

    applied = screen_bot._apply_master_direct(target, applied_text)

    assert applied is False
    assert calls["restore"] == [(base, sha256_text(applied_text))]
    assert reasons and "diagnostics worsened" in reasons[0]


def test_caret_mode_forwards_target_hwnd(screen_bot, monkeypatch):
    screen_bot.direct_file_sync = False
    screen_bot.config["lecture_injection_mode"] = "paste"
    screen_bot.last_injection_ended_with_newline = True  # skip End/Enter key taps

    seen = {}
    monkeypatch.setattr(
        screen_bot,
        "instant_paste",
        lambda text, target_hwnd=None: seen.update(hwnd=target_hwnd, text=text),
    )

    screen_bot.inject_lecture_code(
        "int x = 1;", is_delta=True, full_master="int x = 1;", target_hwnd=4242
    )

    assert seen["hwnd"] == 4242
    assert seen["text"].startswith("int x = 1;")


def test_bind_refuses_when_foreground_is_not_an_editor(screen_bot, monkeypatch):
    monkeypatch.setattr(
        screen_bot.editor_router,
        "active_window",
        lambda: (123, "YouTube - Google Chrome", "chrome.exe", "unknown"),
    )
    screen_bot.target_file_path = None
    screen_bot.direct_file_sync = False
    messages = []
    monkeypatch.setattr(screen_bot, "_notify_guard", lambda msg: messages.append(msg))
    monkeypatch.setattr(screen_bot, "play_sound", lambda *a, **k: None)

    assert screen_bot._active_editor_file() is None
    screen_bot._bind_and_sync_worker()

    assert screen_bot.target_file_path is None
    assert screen_bot.direct_file_sync is False
    assert messages and "Nothing was bound or typed" in messages[0]


def test_bind_refuses_to_guess_a_non_bridge_ide_path(screen_bot, monkeypatch):
    monkeypatch.setattr(
        screen_bot.editor_router,
        "active_window",
        lambda: (123, "Demo.cs - IntelliJ IDEA", "idea64.exe", "jetbrains"),
    )
    monkeypatch.setattr(
        screen_bot,
        "get_or_detect_target_file",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("guessed a path")),
    )
    screen_bot.target_file_path = None
    messages = []
    monkeypatch.setattr(screen_bot, "_notify_guard", lambda msg: messages.append(msg))
    monkeypatch.setattr(screen_bot, "play_sound", lambda *a, **k: None)

    assert screen_bot._active_editor_file() is None
    screen_bot._bind_and_sync_worker()

    assert screen_bot.target_file_path is None
    assert messages and "target_file_path" in messages[0]


def test_bind_uses_the_extension_active_buffer(screen_bot, monkeypatch, tmp_path):
    from editor_adapters import EditorContext
    from editor_adapters import sha256_text

    target = tmp_path / "Demo.cs"
    text = "class Demo\n{\n}\n"
    target.write_text(text, encoding="utf-8")
    ctx = EditorContext(
        file_path=str(target),
        text=text,
        language="csharp",
        dirty=False,
        source="vscode",
        hash=sha256_text(text),
    )
    monkeypatch.setattr(
        screen_bot.editor_router,
        "active_window",
        lambda: (123, "Demo.cs - Visual Studio Code", "code.exe", "vscode"),
    )
    monkeypatch.setattr(
        screen_bot.editor_router.extension,
        "get_context",
        lambda ide, file_path=None: ctx,
    )

    assert screen_bot._active_editor_file() == target


# ---------------------------------------------------------------------------
# Undo (Ctrl+Alt+Z)
# ---------------------------------------------------------------------------

def test_undo_restores_previous_snapshot(screen_bot, monkeypatch, tmp_path):
    target = tmp_path / "Demo.cs"
    old_master = "class Demo\n{\n}\n"
    new_master = "class Demo\n{\n    int x = 1;\n}\n"
    target.write_text(new_master, encoding="utf-8")

    screen_bot.direct_file_sync = True
    screen_bot.target_file_path = str(target)
    screen_bot.last_code_shadow = new_master
    screen_bot.master_tracker.master_code = old_master
    screen_bot.master_tracker.save_snapshot()
    screen_bot.master_tracker.master_code = new_master

    monkeypatch.setattr(screen_bot, "get_or_detect_target_file", lambda *a, **k: target)
    monkeypatch.setattr(
        screen_bot,
        "_editor_context_for",
        lambda t: screen_bot.editor_router.disk.get_context(str(t)),
    )

    screen_bot.trigger_undo()

    assert screen_bot.master_tracker.master_code == old_master
    assert screen_bot.last_code_shadow == old_master
    assert target.read_text(encoding="utf-8") == old_master


def test_undo_refuses_to_overwrite_newer_user_edits(screen_bot, monkeypatch, tmp_path):
    target = tmp_path / "Demo.cs"
    old_master = "class Demo\n{\n}\n"
    delivered = "class Demo\n{\n    int x = 1;\n}\n"
    user_edited = delivered + "// my own note\n"
    target.write_text(user_edited, encoding="utf-8")

    screen_bot.direct_file_sync = True
    screen_bot.target_file_path = str(target)
    screen_bot.last_code_shadow = delivered
    screen_bot.master_tracker.master_code = old_master
    screen_bot.master_tracker.save_snapshot()
    screen_bot.master_tracker.master_code = delivered

    monkeypatch.setattr(
        screen_bot,
        "_editor_context_for",
        lambda t: screen_bot.editor_router.disk.get_context(str(t)),
    )

    assert screen_bot.trigger_undo(quiet=True) is False

    assert target.read_text(encoding="utf-8") == user_edited
    assert screen_bot.master_tracker.snapshots == [old_master]
    assert screen_bot.last_code_shadow == delivered


# ---------------------------------------------------------------------------
# 2-read hallucination gate
# ---------------------------------------------------------------------------

def test_two_read_gate_requires_second_observation(screen_bot):
    line = "    Console.WriteLine(1);"

    assert screen_bot.master_tracker.record_candidate_read(line) == 1
    assert screen_bot.master_tracker.record_candidate_read(line) == 2

    screen_bot._decay_candidate_reads([])
    assert screen_bot.master_tracker.read_counts == {}


def test_compute_code_delta_parts_rebuild_master():
    master = "class Demo\n{\n}\n"
    new_code = "class Demo\n{\n    int x = 1;\n}\n"

    delta, updated, prefix, tail, suffix = bot.compute_code_delta(master, new_code, with_parts=True)

    assert "int x = 1;" in delta
    assert prefix + tail + suffix == updated.splitlines()
    assert "int x = 1;" in updated
