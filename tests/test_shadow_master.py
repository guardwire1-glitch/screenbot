"""
Tests for ShadowMaster (Core Design Principles 1, 2, 3):
- Principle 1: Master is append-only except for gated micro-edits; never shrinks on unseen lines.
- Principle 2: Master code is a file on disk written atomically (.tmp + rename).
- Principle 3: Every state change is appended to a JSONL audit log; master can be rebuilt from the log.
- Failsafe 8: Snapshot ring buffer (up to 50) and undo capability.
"""

import os
import json
import tempfile
import pytest
from pathlib import Path

from screen_bot.shadow_master import ShadowMaster, MasterChangeEvent


def test_atomic_write_creates_file(tmp_path):
    target_file = tmp_path / "Program.cs"
    master = ShadowMaster(file_path=target_file, log_dir=tmp_path)
    
    code = "using System;\nclass Program {\n    static void Main() {}\n}"
    event = MasterChangeEvent(
        frame_id="frame_001",
        event_type="init",
        lines_added=[1, 2, 3, 4],
        lines_modified=[],
        gates_passed=["stable_frame", "syntax_check"],
        gates_failed=[],
        result_code=code,
    )
    master.apply_change(event)
    
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == code


def test_master_never_shrinks_on_missing_lines(tmp_path):
    target_file = tmp_path / "Solution.java"
    master = ShadowMaster(file_path=target_file, log_dir=tmp_path)
    
    initial_code = "public class Solution {\n    int a = 1;\n    int b = 2;\n}"
    master.apply_change(MasterChangeEvent(
        frame_id="frame_001",
        event_type="init",
        lines_added=[1, 2, 3, 4],
        lines_modified=[],
        gates_passed=["stable_frame"],
        gates_failed=[],
        result_code=initial_code,
    ))
    
    # Lecturer scrolled down, frame only sees: "    int b = 2;\n}"
    # Master must NOT shrink or delete "public class Solution {\n    int a = 1;"
    unseen_frame_lines = ["    int b = 2;", "}"]
    merged = master.propose_merge(visible_lines=unseen_frame_lines, anchor_hint=None)
    
    assert "public class Solution {" in merged
    assert "int a = 1;" in merged
    assert len(merged.splitlines()) >= len(initial_code.splitlines())


def test_jsonl_event_log_records_every_change(tmp_path):
    target_file = tmp_path / "App.java"
    log_dir = tmp_path / "logs"
    master = ShadowMaster(file_path=target_file, log_dir=log_dir)
    
    event1 = MasterChangeEvent(
        frame_id="frame_001",
        event_type="init",
        lines_added=[1],
        lines_modified=[],
        gates_passed=["stable"],
        gates_failed=[],
        result_code="class App {}",
    )
    master.apply_change(event1)
    
    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 1
    
    with open(jsonl_files[0], "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
        
    assert len(records) == 1
    assert records[0]["frame_id"] == "frame_001"
    assert records[0]["result_code"] == "class App {}"


def test_rebuild_master_from_jsonl(tmp_path):
    target_file = tmp_path / "Rebuild.cs"
    log_dir = tmp_path / "logs"
    master = ShadowMaster(file_path=target_file, log_dir=log_dir)
    
    master.apply_change(MasterChangeEvent(
        frame_id="f1",
        event_type="init",
        lines_added=[1],
        lines_modified=[],
        gates_passed=["gate1"],
        gates_failed=[],
        result_code="// Step 1\n",
    ))
    master.apply_change(MasterChangeEvent(
        frame_id="f2",
        event_type="append",
        lines_added=[2],
        lines_modified=[],
        gates_passed=["gate1", "gate2"],
        gates_failed=[],
        result_code="// Step 1\n// Step 2\n",
    ))
    
    jsonl_path = list(log_dir.glob("*.jsonl"))[0]
    rebuilt_code = ShadowMaster.rebuild_from_log(jsonl_path)
    assert rebuilt_code == "// Step 1\n// Step 2\n"
    assert rebuilt_code == target_file.read_text(encoding="utf-8")


def test_snapshot_ring_buffer_and_undo(tmp_path):
    target_file = tmp_path / "UndoTest.java"
    master = ShadowMaster(file_path=target_file, log_dir=tmp_path, max_snapshots=50)
    
    master.apply_change(MasterChangeEvent(
        frame_id="f1",
        event_type="init",
        lines_added=[1],
        lines_modified=[],
        gates_passed=[],
        gates_failed=[],
        result_code="version 1",
    ))
    master.apply_change(MasterChangeEvent(
        frame_id="f2",
        event_type="edit",
        lines_added=[2],
        lines_modified=[],
        gates_passed=[],
        gates_failed=[],
        result_code="version 2",
    ))
    
    assert master.get_current_code() == "version 2"
    undone = master.undo()
    assert undone is True
    assert master.get_current_code() == "version 1"
    assert target_file.read_text(encoding="utf-8") == "version 1"
