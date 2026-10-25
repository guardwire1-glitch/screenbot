"""
Regression tests for the stuck-modifier / shutdown-cleanup path.

Symptoms this locks down:
  - After a suppressed global hotkey (Ctrl+Shift+F), Ctrl/Shift could remain
    logically down, so every later click became a Ctrl+click (opens a link/new
    window) or a Ctrl+drag (duplicates an item) until the PC was rebooted.
  - Force-killing ScreenBot (stop_bot.bat uses taskkill /F) skipped all Python
    cleanup, leaving the same stuck state.
  - Ctrl+Alt+Z and Ctrl+Shift+Q were needlessly registered with suppress=True.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

import bot


class _Clipboard:
    def __init__(self, fail_copy=False):
        self.fail_copy = fail_copy

    def copy(self, _text):
        if self.fail_copy:
            raise RuntimeError("clipboard busy")

    def paste(self):
        return "original"


class _FakeUser32:
    def __init__(self):
        self.events = []
        self.hwnd = 1

    def keybd_event(self, vk, scan, flags, extra):
        self.events.append((vk, flags))

    def GetForegroundWindow(self):
        return self.hwnd

    def GetAsyncKeyState(self, _vk):
        return 0


@pytest.fixture
def screen_bot(monkeypatch):
    monkeypatch.setattr(bot, "pyperclip", _Clipboard())
    sb = bot.ScreenBot()
    sb.tray_icon = None
    sb.config["sound_feedback"] = False
    return sb


def test_release_all_modifiers_sends_keyups_including_win():
    fake = _FakeUser32()
    original = bot.user32
    bot.user32 = fake
    try:
        bot.release_all_modifiers()
    finally:
        bot.user32 = original

    keyups = {vk for vk, flags in fake.events if flags & 0x0002}
    # Generic + left/right Ctrl, Shift, Alt and both Windows keys.
    assert {0x10, 0x11, 0x12, 0xA0, 0xA2, 0xA4, 0x5B, 0x5C}.issubset(keyups)


def test_release_all_modifiers_never_raises_without_keybd_event():
    class _Broken:
        pass

    original = bot.user32
    bot.user32 = _Broken()
    try:
        bot.release_all_modifiers(reason="test")
    finally:
        bot.user32 = original


def test_wait_for_modifiers_release_forces_keyups(screen_bot, monkeypatch):
    fake = _FakeUser32()
    calls = []
    monkeypatch.setattr(bot, "user32", fake)
    monkeypatch.setattr(bot.keyboard, "is_pressed", lambda _key: False)
    monkeypatch.setattr(bot.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        bot, "release_all_modifiers", lambda reason="": calls.append(reason)
    )

    screen_bot.wait_for_modifiers_release()

    assert calls == [""]


def test_guarded_hotkey_always_releases_after_handler(screen_bot, monkeypatch):
    events = []

    class _ImmediateTimer:
        def __init__(self, _interval, function):
            self.function = function
            self.daemon = False

        def start(self):
            events.append("timer")
            self.function()

    monkeypatch.setattr(bot.threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(
        bot, "release_all_modifiers", lambda reason="": events.append("release")
    )

    wrapped = screen_bot._guarded_hotkey(lambda: events.append("handler") or "ok")

    assert wrapped() == "ok"
    assert events == ["handler", "timer", "release"]


def test_guarded_hotkey_releases_even_when_handler_raises(screen_bot, monkeypatch):
    events = []

    class _ImmediateTimer:
        def __init__(self, _interval, function):
            self.function = function
            self.daemon = False

        def start(self):
            self.function()

    monkeypatch.setattr(bot.threading, "Timer", _ImmediateTimer)
    monkeypatch.setattr(
        bot, "release_all_modifiers", lambda reason="": events.append("release")
    )

    def _boom():
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError):
        screen_bot._guarded_hotkey(_boom)()

    assert events == ["release"]


def test_stop_releases_modifiers_before_and_after_unhook(screen_bot, monkeypatch):
    calls = []
    monkeypatch.setattr(
        bot, "release_all_modifiers", lambda reason="": calls.append(reason)
    )
    monkeypatch.setattr(bot.keyboard, "unhook_all", lambda: calls.append("unhook"))

    with pytest.raises(SystemExit):
        screen_bot.stop()

    assert calls == ["shutdown", "unhook", ""]

    # A second stop() must be a harmless no-op (tray + exit hotkey can race).
    screen_bot.stop()
    assert calls == ["shutdown", "unhook", ""]


def test_instant_paste_releases_ctrl_and_v_when_interrupted(screen_bot, monkeypatch):
    fake = _FakeUser32()
    monkeypatch.setattr(bot, "user32", fake)
    monkeypatch.setattr(bot, "pyperclip", _Clipboard(fail_copy=True))
    monkeypatch.setattr(screen_bot, "wait_for_modifiers_release", lambda: None)
    monkeypatch.setattr(screen_bot, "_caret_target_safe", lambda *a, **k: True)

    fallback = []
    monkeypatch.setattr(
        screen_bot, "human_type", lambda *a, **k: fallback.append(True)
    )

    screen_bot.instant_paste("int x = 1;")

    assert fallback == [True]
    keyups = {vk for vk, flags in fake.events if flags & 0x0002}
    assert {0x11, 0x56}.issubset(keyups)  # Ctrl + V


def test_manual_sync_is_queued_when_auto_scan_is_busy(screen_bot, monkeypatch):
    screen_bot.is_busy = True
    screen_bot._manual_sync_queued = False
    started = []

    class _FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            started.append(True)

    monkeypatch.setattr(bot.threading, "Thread", _FakeThread)

    screen_bot.trigger_ctrl_shift_f()

    assert screen_bot._manual_sync_queued is True
    assert started == [True]


def test_queued_manual_sync_runs_once_idle(screen_bot, monkeypatch):
    screen_bot.is_busy = False
    screen_bot._manual_sync_queued = True
    calls = []
    monkeypatch.setattr(screen_bot, "trigger_ctrl_shift_f", lambda: calls.append(True))

    screen_bot._run_queued_manual_sync()

    assert calls == [True]
    assert screen_bot._manual_sync_queued is False


def test_running_badge_is_a_separate_silent_process(screen_bot, monkeypatch):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return None

    monkeypatch.setattr(bot.subprocess, "Popen", fake_popen)
    screen_bot.auto_scan_enabled = False
    screen_bot.current_subject = "auto"

    screen_bot.show_running_badge()

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert "--badge" in cmd
    assert cmd[-2:] == ["0", "auto"]
    assert kwargs.get("creationflags") == 0x08000000  # CREATE_NO_WINDOW


def test_process_screen_ctrl0_passes_quality_without_typeerror(screen_bot, monkeypatch):
    """Regression for: capture_screen_bytes() got an unexpected keyword 'quality'."""
    calls = {}

    def fake_capture(**kwargs):
        calls.update(kwargs)
        return b"fake-png"

    monkeypatch.setattr(bot, "capture_screen_bytes", fake_capture)
    monkeypatch.setattr(screen_bot.solver, "solve", lambda *a, **k: "answer")
    monkeypatch.setattr(
        bot.pyperclip, "copy", lambda text: calls.__setitem__("copied", text)
    )

    screen_bot.config["capture_mode"] = "active_window"
    screen_bot.config["max_image_dimension"] = 1920
    screen_bot.config["image_quality"] = 85
    screen_bot.current_subject = "auto"

    screen_bot.process_screen("concise")

    assert calls["mode"] == "active_window"
    assert calls["max_dim"] == 1920
    assert calls["quality"] == 85
    assert calls["copied"] == "answer"


def test_start_registers_ctrl_shift_f_as_the_only_suppressed_hotkey(
    screen_bot, monkeypatch
):
    recorded = []
    screen_bot.config["auto_scan_enabled"] = False
    monkeypatch.setattr(
        bot.keyboard,
        "add_hotkey",
        lambda keys, callback, **kwargs: recorded.append(
            (keys, bool(kwargs.get("suppress", False)))
        ),
    )
    monkeypatch.setattr(bot, "release_all_modifiers", lambda reason="": None)
    monkeypatch.setattr(screen_bot, "_modifier_watchdog", lambda: None)
    monkeypatch.setattr(screen_bot, "play_sound", lambda *a, **k: None)

    class _FakeIcon:
        def __init__(self, *a, **k):
            self.visible = False

        def run(self, setup=None):
            if setup:
                setup(self)

        def stop(self):
            pass

        def notify(self, *a, **k):
            pass

    monkeypatch.setattr(bot.pystray, "Icon", _FakeIcon)
    monkeypatch.setattr(bot, "get_or_create_icon_image", lambda: None)

    screen_bot.start()

    suppressed = [keys for keys, is_suppressed in recorded if is_suppressed]
    assert suppressed == ["ctrl+shift+f"]
    assert ("ctrl+alt+z", False) in recorded
    assert ("ctrl+shift+q", False) in recorded
