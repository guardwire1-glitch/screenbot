"""
Integration tests for Bot Pipeline Integration:
- Verifies that bot initializes ShadowMaster, CircuitBreaker, ConsensusTracker, and MergeEngine.
- Verifies undo hotkey integration with ShadowMaster.
- Verifies that --selftest returns cleanly.
"""

import pytest
from pathlib import Path
from screen_bot.bot import ScreenBot
from screen_bot.harness import run_selftest


def test_bot_initializes_core_architecture():
    bot = ScreenBot()
    assert hasattr(bot, "shadow_master")
    assert hasattr(bot, "circuit_breaker")
    assert hasattr(bot, "consensus_tracker")
    assert hasattr(bot, "merge_engine")
    assert hasattr(bot, "safe_injector")
    assert hasattr(bot, "session_reconciler")
    assert hasattr(bot, "build_hash")


def test_bot_undo_rolls_back_master(tmp_path):
    bot = ScreenBot()
    bot.shadow_master.file_path = tmp_path / "UndoTarget.java"
    
    from screen_bot.shadow_master import MasterChangeEvent
    bot.shadow_master.apply_change(MasterChangeEvent(
        frame_id="f1",
        event_type="init",
        result_code="version 1",
    ))
    bot.shadow_master.apply_change(MasterChangeEvent(
        frame_id="f2",
        event_type="append",
        result_code="version 2",
    ))
    
    assert bot.shadow_master.get_current_code() == "version 2"
    bot.undo_last_edit()
    assert bot.shadow_master.get_current_code() == "version 1"


def test_selftest_cli_integration():
    report = run_selftest(check_api=False)
    assert report["clipboard_access"] in ("PASS", "FAIL")
    assert report["write_access"] == "PASS"
    assert report["capture_system"] == "PASS"
