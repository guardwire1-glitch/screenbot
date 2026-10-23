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

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

import bot
import capture


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
        "extract_lecture_code",
        lambda *a, **k: calls.append(1) or "",
    )

    screen_bot.process_code_shadow(is_auto=True)

    assert calls == []


# ---------------------------------------------------------------------------
# Snapshot-time HWND binding (ISSUE #06/#07)
# ---------------------------------------------------------------------------

def test_process_code_shadow_binds_snapshot_hwnd(screen_bot, monkeypatch):
    img = Image.new("RGB", (32, 32), (1, 2, 3))
    monkeypatch.setattr(bot, "capture_stable_frame", lambda **kwargs: (img, True))
    monkeypatch.setattr(bot, "encode_image_to_bytes", lambda *a, **k: b"png-bytes")
    monkeypatch.setattr(bot, "user32", _FakeUser32(777))
    monkeypatch.setattr(
        screen_bot.solver,
        "extract_lecture_code",
        lambda *a, **k: "int x = 1;",
    )

    screen_bot.direct_file_sync = False
    screen_bot.target_file_path = None
    screen_bot.last_lecture_frame = None

    captured = {}

    def fake_inject(code_text, is_delta=True, full_master="", target_hwnd=None):
        captured.update(code=code_text, hwnd=target_hwnd, master=full_master)

    monkeypatch.setattr(screen_bot, "inject_lecture_code", fake_inject)

    screen_bot.process_code_shadow(is_auto=True)

    assert captured["hwnd"] == 777
    assert "int x = 1;" in captured["code"]


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

    screen_bot.inject_lecture_code(
        "    int x = 1;", is_delta=True, full_master=new_master, target_hwnd=111
    )

    assert target.read_text(encoding="utf-8") == new_master
    assert typed == []  # zero-keystroke mode: never type into the foreground window
    assert screen_bot.master_tracker.snapshots == [old_master]
    assert screen_bot.master_tracker.master_code == new_master


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


# ---------------------------------------------------------------------------
# Undo (Ctrl+Alt+Z)
# ---------------------------------------------------------------------------

def test_undo_restores_previous_snapshot(screen_bot, monkeypatch, tmp_path):
    target = tmp_path / "Demo.cs"
    old_master = "class Demo\n{\n}\n"
    new_master = "class Demo\n{\n    int x = 1;\n}\n"
    target.write_text(new_master, encoding="utf-8")

    screen_bot.target_file_path = str(target)
    screen_bot.last_code_shadow = new_master
    screen_bot.master_tracker.master_code = old_master
    screen_bot.master_tracker.save_snapshot()
    screen_bot.master_tracker.master_code = new_master

    monkeypatch.setattr(screen_bot, "get_or_detect_target_file", lambda *a, **k: target)

    screen_bot.trigger_undo()

    assert screen_bot.master_tracker.master_code == old_master
    assert screen_bot.last_code_shadow == old_master
    assert target.read_text(encoding="utf-8") == old_master


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
