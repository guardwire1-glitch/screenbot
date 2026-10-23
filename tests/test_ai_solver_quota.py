"""Regression tests for Gemini quota handling and the bounded-timeout fix."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

import ai_solver


def test_quota_error_detection_covers_real_messages():
    assert ai_solver._is_quota_error("429 RESOURCE_EXHAUSTED: quota exceeded")
    assert ai_solver._is_quota_error("Resource exhausted")
    assert ai_solver._is_quota_error("Rate limit reached for model")
    assert not ai_solver._is_quota_error("404 model not found")
    assert not ai_solver._is_quota_error("connection reset by peer")


class _QuotaModels:
    def __init__(self):
        self.calls = 0

    def generate_content(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")


class _FakeClient:
    def __init__(self, models):
        self.models = models


def test_quota_error_is_not_retried_and_skips_fallback_models():
    models = _QuotaModels()
    solver = ai_solver.ScreenSolver(api_key="dummy")
    solver._client = _FakeClient(models)

    with pytest.raises(ai_solver.QuotaExceededError):
        solver._call_with_retry_and_timeout(model="test-model", contents=[])

    # Exactly one network attempt: no second retry and no fallback model loop.
    assert models.calls == 1


def test_extract_lecture_code_returns_quota_marker_without_trying_fallbacks():
    models = _QuotaModels()
    solver = ai_solver.ScreenSolver(api_key="dummy")
    solver._client = _FakeClient(models)

    result = solver.extract_lecture_code(b"png-bytes", subject="csharp")

    assert result.startswith("[ScreenBot Quota Error]")
    assert models.calls == 1
