"""
Tests for Failsafes and Circuit Breaker (Pipeline Stage 8):
- Circuit breaker trips on >3 auto-edits in 60s, same line edited twice, or 3 failed injections.
- Safe Mode: capture and master keep running; injection is paused; status is yellow.
- Watchdog flags stalled scan loops (>30s).
- Deterministic build hash.
"""

import time
import pytest
from screen_bot.failsafes import CircuitBreaker, ScanWatchdog, compute_build_hash


def test_circuit_breaker_trips_on_rate_limit():
    cb = CircuitBreaker(max_edits_per_minute=3)
    
    now = time.time()
    assert cb.record_edit(line_no=10, timestamp=now) is True
    assert cb.record_edit(line_no=11, timestamp=now + 5) is True
    assert cb.record_edit(line_no=12, timestamp=now + 10) is True
    
    # 4th edit within 60s trips the circuit breaker!
    assert cb.record_edit(line_no=13, timestamp=now + 15) is False
    assert cb.is_tripped is True
    assert cb.status_color == "yellow"


def test_circuit_breaker_trips_on_same_line_edited_twice():
    cb = CircuitBreaker()
    now = time.time()
    assert cb.record_edit(line_no=42, timestamp=now) is True
    # Same line edited again within 60s trips the breaker!
    assert cb.record_edit(line_no=42, timestamp=now + 2) is False
    assert cb.is_tripped is True


def test_circuit_breaker_trips_on_three_failed_injections():
    cb = CircuitBreaker()
    cb.record_injection_failure()
    assert cb.is_tripped is False
    cb.record_injection_failure()
    assert cb.is_tripped is False
    cb.record_injection_failure()
    # 3 failures trips it
    assert cb.is_tripped is True
    assert cb.status_color == "yellow"


def test_scan_watchdog_detects_stall():
    watchdog = ScanWatchdog(timeout_seconds=30)
    watchdog.tick(timestamp=100.0)
    assert watchdog.is_stalled(current_time=120.0) is False
    # Stalled after 30s
    assert watchdog.is_stalled(current_time=135.0) is True


def test_build_hash_returns_hex():
    h = compute_build_hash()
    assert isinstance(h, str)
    assert len(h) >= 8
