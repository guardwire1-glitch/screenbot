"""
Failsafes, Circuit Breaker, and Watchdog (Pipeline Stage 8):
- Circuit Breaker: pauses injection (safe mode) and turns tray yellow if:
  * >3 auto-edits occur within 60s
  * the same line is edited twice within 60s
  * 3 consecutive injection failures occur
- Scan Watchdog: restarts scan loop if no tick in 30s.
- Deterministic Build Hash.
- Kill Switch: Ctrl+Alt+Esc (not bare ESC).
"""

import time
import hashlib
from typing import List, Dict, Tuple, Optional
from pathlib import Path


class CircuitBreaker:
    """Monitors live edits and injections to prevent runaway corruptions."""

    def __init__(self, max_edits_per_minute: int = 3):
        self.max_edits_per_minute = max_edits_per_minute
        self.is_tripped = False
        self.status_color = "green"  # "green", "yellow", "red"
        self._edit_timestamps: List[float] = []
        self._line_edits: Dict[int, float] = {}
        self._consecutive_failures: int = 0
        self.trip_reason: str = ""

    def record_edit(self, line_no: Optional[int], timestamp: Optional[float] = None) -> bool:
        """Records an edit. Returns True if within safety bounds, False if tripped."""
        now = timestamp if timestamp is not None else time.time()
        # Clean timestamps older than 60s
        self._edit_timestamps = [t for t in self._edit_timestamps if now - t <= 60.0]

        # Check condition 1: Rate limit (>3 edits in 60s)
        if len(self._edit_timestamps) >= self.max_edits_per_minute:
            self._trip(f"Circuit breaker tripped: rate limit ({len(self._edit_timestamps)} edits in 60s)")
            return False

        # Check condition 2: Same line edited twice within 60s
        if line_no is not None:
            last_time = self._line_edits.get(line_no)
            if last_time and (now - last_time <= 60.0):
                self._trip(f"Circuit breaker tripped: line {line_no} edited twice within 60s")
                return False
            self._line_edits[line_no] = now

        self._edit_timestamps.append(now)
        return True

    def record_injection_failure(self):
        """Records a failed injection. 3 in a row trips the breaker."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            self._trip("Circuit breaker tripped: 3 consecutive injection failures")

    def record_injection_success(self):
        self._consecutive_failures = 0

    def _trip(self, reason: str):
        self.is_tripped = True
        self.status_color = "yellow"
        self.trip_reason = reason

    def reset(self):
        self.is_tripped = False
        self.status_color = "green"
        self._consecutive_failures = 0
        self.trip_reason = ""


class ScanWatchdog:
    """Monitors the capture/scan loop and reports stalls > 30s."""

    def __init__(self, timeout_seconds: float = 30.0):
        self.timeout_seconds = timeout_seconds
        self.last_tick_time = time.time()

    def tick(self, timestamp: Optional[float] = None):
        self.last_tick_time = timestamp if timestamp is not None else time.time()

    def is_stalled(self, current_time: Optional[float] = None) -> bool:
        now = current_time if current_time is not None else time.time()
        return (now - self.last_tick_time) > self.timeout_seconds


def compute_build_hash() -> str:
    """Computes a deterministic build hash for traceability in startup logs."""
    hasher = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for py_file in sorted(root.glob("*.py")):
        try:
            hasher.update(py_file.read_bytes())
        except Exception:
            pass
    return hasher.hexdigest()[:10]
