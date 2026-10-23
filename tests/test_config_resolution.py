"""
Regression tests for configuration & credential resolution.

Bugs these lock down:
  - A shipped config.json template with "gemini_api_key": "" silently erased
    the real key stored in %APPDATA%\\ScreenBot\\config.json, so the deployed
    ScreenBot.exe died with "GEMINI_API_KEY is not set."
  - A shipped .env with an empty "GEMINI_API_KEY=" injected an empty string
    into os.environ, masking a real system environment variable.
  - Blank values in any lower-priority source must never erase a populated
    value from a higher-priority source.

Documented credential precedence (highest first):
  1. System environment variable (GEMINI_API_KEY / GOOGLE_API_KEY)
  2. .env file beside the script or executable
  3. %APPDATA%\\ScreenBot\\config.json  (per-user authoritative store)
  4. Local config.json                   (shipped template)
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "screen_bot"))

import bot


REAL_KEY = "test_gemini_api_key_placeholder_for_unit_tests"
OTHER_KEY = "AIzaSyTESTKEY0000000000000000000000000"


@pytest.fixture(autouse=True)
def clean_env():
    """Restores os.environ so dotenv mutations cannot leak between tests."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """Builds a simulated deployed layout and points bot at it.

    Returns a helper: build(env=..., local_config=..., user_config=...) -> dict
    """

    def build(env=None, local_config=None, user_config=None, system_env=None):
        bt = tmp_path / "bt"
        bt.mkdir(exist_ok=True)

        if env is not None:
            (bt / ".env").write_text(env, encoding="utf-8")
        if local_config is not None:
            (bt / "config.json").write_text(
                json.dumps(local_config, indent=2), encoding="utf-8"
            )

        appdata = tmp_path / "appdata" / "ScreenBot"
        appdata.mkdir(parents=True, exist_ok=True)
        if user_config is not None:
            (appdata / "config.json").write_text(
                json.dumps(user_config, indent=2), encoding="utf-8"
            )

        monkeypatch.setattr(bot, "SCRIPT_DIR", bt)
        monkeypatch.setattr(bot, "ENV_PATH", bt / ".env")
        monkeypatch.setattr(bot, "CONFIG_PATH", bt / "config.json")
        monkeypatch.setattr(bot, "USER_CONFIG_DIR", appdata)
        monkeypatch.setattr(bot, "USER_CONFIG_PATH", appdata / "config.json")

        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_API_KEY", None)
        if system_env:
            os.environ["GEMINI_API_KEY"] = system_env

        return bot.load_config()

    return build


def test_empty_local_config_does_not_erase_per_user_key(layout):
    """The deployed bt/ layout: empty .env + empty config.json must still
    resolve the real key from the %APPDATA% per-user store."""
    cfg = layout(
        env="GEMINI_API_KEY=\n",
        local_config={"gemini_api_key": "", "model": "gemini-3.5-flash-lite"},
        user_config={"gemini_api_key": REAL_KEY},
    )
    assert cfg["gemini_api_key"] == REAL_KEY


def test_empty_env_file_does_not_shadow_system_env_var(layout):
    """An empty .env must not mask a real system environment variable."""
    cfg = layout(
        env="GEMINI_API_KEY=\n",
        local_config={"gemini_api_key": ""},
        user_config={"gemini_api_key": REAL_KEY},
        system_env=OTHER_KEY,
    )
    assert cfg["gemini_api_key"] == OTHER_KEY


def test_env_file_key_wins_over_per_user_store(layout):
    """Precedence tier 2 (.env) beats tier 3 (%APPDATA%)."""
    cfg = layout(
        env=f"GEMINI_API_KEY={OTHER_KEY}\n",
        local_config={"gemini_api_key": ""},
        user_config={"gemini_api_key": REAL_KEY},
    )
    assert cfg["gemini_api_key"] == OTHER_KEY


def test_system_env_var_wins_over_env_file(layout):
    """Precedence tier 1 (system env var) beats tier 2 (.env)."""
    cfg = layout(
        env=f"GEMINI_API_KEY={OTHER_KEY}\n",
        local_config={"gemini_api_key": ""},
        user_config={"gemini_api_key": REAL_KEY},
        system_env=REAL_KEY,
    )
    assert cfg["gemini_api_key"] == REAL_KEY


def test_per_user_store_wins_over_local_template(layout):
    """Precedence tier 3 (%APPDATA%) beats tier 4 (local config.json)."""
    cfg = layout(
        env=None,
        local_config={"gemini_api_key": OTHER_KEY},
        user_config={"gemini_api_key": REAL_KEY},
    )
    assert cfg["gemini_api_key"] == REAL_KEY


def test_blank_overlay_never_erases_populated_value(layout):
    """Blank strings in any overlay must not wipe a populated value."""
    cfg = layout(
        env=None,
        local_config={"gemini_api_key": "", "model": ""},
        user_config={"gemini_api_key": REAL_KEY, "model": "gemini-3.5-flash-lite"},
    )
    assert cfg["gemini_api_key"] == REAL_KEY
    assert cfg["model"] == "gemini-3.5-flash-lite"


def test_local_template_still_supplies_missing_settings(layout):
    """Settings absent from the per-user store still come from the template."""
    cfg = layout(
        env=None,
        local_config={"gemini_api_key": "", "subject": "csharp", "typing_wpm": 38},
        user_config={"gemini_api_key": REAL_KEY},
    )
    assert cfg["subject"] == "csharp"
    assert cfg["typing_wpm"] == 38


def test_boolean_false_still_overrides(layout):
    """False is a meaningful value, not a blank — it must still override."""
    cfg = layout(
        env=None,
        local_config={"auto_scan_enabled": False},
        user_config={"auto_scan_enabled": True},
    )
    assert cfg["auto_scan_enabled"] is False


def test_missing_key_everywhere_resolves_empty(layout):
    """With no credential anywhere, the key resolves to empty (bot reports
    'GEMINI_API_KEY is not set') rather than raising."""
    cfg = layout(
        env="GEMINI_API_KEY=\n",
        local_config={"gemini_api_key": ""},
        user_config={"gemini_api_key": ""},
    )
    assert cfg["gemini_api_key"] == ""
