"""Regression tests for the Ctrl+0 capture crash and multi-language support.

The deployed build crashed with:
    [ScreenBot Error]: capture_screen_bytes() got an unexpected keyword argument 'quality'
because bot.py passed quality= while capture_screen_bytes() did not accept it.
These tests lock the helper signatures together.
"""

import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

import ai_solver
import capture


def _fake_capture(**_kwargs):
    return Image.new("RGB", (80, 40), "white")


def test_capture_screen_bytes_accepts_bot_keywords(monkeypatch):
    monkeypatch.setattr(capture, "capture_screen_image", _fake_capture)

    png = capture.capture_screen_bytes(
        mode="active_window",
        max_dim=1920,
        export_format="PNG",
        quality=85,
    )
    assert png.startswith(b"\x89PNG")

    jpeg = capture.capture_screen_bytes(
        mode="lecture",
        bbox=(0, 0, 80, 40),
        target_hwnd=1,
        crop_rect=(0, 0, 80, 40),
        max_dim=100,
        export_format="JPEG",
        quality=50,
    )
    assert jpeg.startswith(b"\xff\xd8")


def test_solve_prompt_supports_auto_language_detection(monkeypatch):
    solver = ai_solver.ScreenSolver(api_key="dummy")
    captured = {}

    def fake_call(model, contents, config=None, timeout=15.0):
        captured["model"] = model
        captured["prompt"] = contents[-1]
        return "print('hello')"

    monkeypatch.setattr(solver, "_call_with_retry_and_timeout", fake_call)

    result = solver.solve(b"png-bytes", mode="concise", subject="auto")

    prompt = captured["prompt"]
    assert "Auto-detect the programming language" in prompt
    assert "Python" in prompt
    assert "C++" in prompt
    assert "JavaScript" in prompt
    assert result == "print('hello')"


def test_extract_lecture_code_prompt_supports_all_major_languages(monkeypatch):
    solver = ai_solver.ScreenSolver(api_key="dummy")
    captured = {}

    def fake_call(model, contents, config=None, timeout=15.0):
        captured["prompt"] = contents[-1]
        return "def add(a, b):\n    return a + b"

    monkeypatch.setattr(solver, "_call_with_retry_and_timeout", fake_call)

    result = solver.extract_lecture_code(b"png-bytes", subject="auto")

    prompt = captured["prompt"]
    assert "Auto-detect the language" in prompt
    for language in ("Java", "C++", "C#", "Python", "JavaScript", "PHP", "Go", "Rust", "Kotlin", "Swift", "SQL"):
        assert language in prompt
    assert "NEVER translate" in prompt
    assert result.startswith("def add")


def test_extract_lecture_code_keeps_explicit_subject_as_a_hint(monkeypatch):
    solver = ai_solver.ScreenSolver(api_key="dummy")
    captured = {}

    def fake_call(model, contents, config=None, timeout=15.0):
        captured["prompt"] = contents[-1]
        return "public class Demo {}"

    monkeypatch.setattr(solver, "_call_with_retry_and_timeout", fake_call)

    solver.extract_lecture_code(b"png-bytes", subject="csharp")

    prompt = captured["prompt"]
    assert "Subject hint: CSHARP" in prompt
    assert "If the visible code is a different language" in prompt
