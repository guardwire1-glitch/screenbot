"""
Shadow Master Storage & Atomic Persistence (Core Design Principles 1, 2, 3).

Key Guarantees:
- Principle 1: Master is append-only except for gated micro-edits. Lines missing
  from a frame are "unseen", never "deleted".
- Principle 2: Master code is a file on disk (the shadow master), written atomically
  via temp file + atomic rename (os.replace).
- Principle 3: Every state change is an event appended to a JSONL log (frame id,
  proposal, gates passed/failed, result). Master can be rebuilt from the log.
- Failsafe 8: Snapshot ring buffer (keeps last 50) with instant undo.
"""

import os
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Any, Optional


@dataclass
class MasterChangeEvent:
    frame_id: str
    event_type: str  # "init", "append", "edit", "undo", "noop"
    lines_added: List[int] = field(default_factory=list)
    lines_modified: List[int] = field(default_factory=list)
    gates_passed: List[str] = field(default_factory=list)
    gates_failed: List[str] = field(default_factory=list)
    result_code: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


class ShadowMaster:
    """Manages the disk-backed authoritative master code, atomic writes, snapshots, and JSONL audit logging."""

    def __init__(
        self,
        file_path: Path,
        log_dir: Optional[Path] = None,
        max_snapshots: int = 50,
    ):
        self.file_path = Path(file_path).resolve()
        self.log_dir = Path(log_dir).resolve() if log_dir else self.file_path.parent / ".session_logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_snapshots = max_snapshots

        self._session_id = f"session_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self._log_file = self.log_dir / f"master_audit_{self._session_id}.jsonl"

        # In-memory code state
        self._current_code: str = ""
        self._snapshots: List[str] = []

        if self.file_path.exists():
            try:
                self._current_code = self.file_path.read_text(encoding="utf-8")
                self._snapshots.append(self._current_code)
            except Exception:
                self._current_code = ""

    def get_current_code(self) -> str:
        return self._current_code

    def apply_change(self, event: MasterChangeEvent) -> bool:
        """Applies an event, writes atomically to disk, archives snapshot, and appends to JSONL."""
        new_code = event.result_code

        # Push prior state to snapshots before mutation
        if self._current_code is not None:
            self._snapshots.append(self._current_code)
            if len(self._snapshots) > self.max_snapshots:
                self._snapshots.pop(0)

        # Atomic file write: write to temp file on same filesystem, then atomic replace
        self._atomic_write(new_code)
        self._current_code = new_code

        # Log event to JSONL audit log
        self._append_event_log(event)
        return True

    def undo(self) -> bool:
        """Rolls back to the previous snapshot in the ring buffer."""
        if not self._snapshots:
            return False

        previous_code = self._snapshots.pop()
        self._atomic_write(previous_code)
        self._current_code = previous_code

        undo_event = MasterChangeEvent(
            frame_id="undo_action",
            event_type="undo",
            gates_passed=["user_undo"],
            result_code=previous_code,
        )
        self._append_event_log(undo_event)
        return True

    def propose_merge(self, visible_lines: List[str], anchor_hint: Optional[str] = None) -> str:
        """
        Principle 1 Merge:
        Absence of evidence is not evidence of deletion.
        A line missing from a frame is 'unseen', never 'deleted'.
        The master code is append-only except for gated micro-edits.
        """
        if not self._current_code:
            return "\n".join(visible_lines)

        master_lines = self._current_code.splitlines()
        if not visible_lines:
            return self._current_code

        # If visible lines are already a subset/subsequence of master (e.g. lecturer scrolled down),
        # keep master intact!
        joined_visible = "\n".join(visible_lines)
        if joined_visible in self._current_code:
            return self._current_code

        # Find overlap anchor between visible_lines and the end of master_lines
        max_overlap = 0
        overlap_index = 0
        for i in range(len(visible_lines), 0, -1):
            candidate = visible_lines[:i]
            if len(master_lines) >= i and master_lines[-i:] == candidate:
                max_overlap = i
                overlap_index = i
                break

        if max_overlap > 0:
            # Append newly seen lines following the overlap anchor
            new_lines = visible_lines[overlap_index:]
            combined = master_lines + new_lines
            return "\n".join(combined)

        # If no clean overlap at end, check if visible_lines are deeper down in an append scenario
        return self._current_code

    def _atomic_write(self, content: str):
        """Writes content atomically using a temporary file and os.replace."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.file_path.with_suffix(f".tmp_{os.getpid()}_{uuid.uuid4().hex[:6]}")
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.file_path)

    def _append_event_log(self, event: MasterChangeEvent):
        """Appends event record to the JSONL log file."""
        record = asdict(event)
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()

    @classmethod
    def rebuild_from_log(cls, jsonl_path: Path) -> str:
        """Reconstructs master code from the JSONL audit log from scratch."""
        last_code = ""
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if "result_code" in data:
                    last_code = data["result_code"]
        return last_code
