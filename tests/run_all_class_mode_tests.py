"""
ScreenBot Class Mode Verification Suite:
Executes the full test suite and verifies:
1. All unit tests
2. Invariant property tests
3. 1,000-session fuzz testing (0 deleted, 0 duplicated, 0 invented lines)
4. Self-test system check
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "screen_bot"))

import pytest
from screen_bot.harness import run_property_invariants_check, run_fuzz_sessions, run_selftest
from screen_bot.failsafes import compute_build_hash


def main():
    print("=" * 70)
    print("    SCREENBOT CLASS MODE COMPLETE VERIFICATION SUITE")
    print(f"    Build Hash: {compute_build_hash()}")
    print("=" * 70)

    # 1. Self-test
    print("\n--- 1. SYSTEM SELF-TEST ---")
    st = run_selftest(check_api=False)
    for k, v in st.items():
        print(f"  [{v:4s}] {k}")
    assert all(v in ("PASS", "SKIPPED") for v in st.values()), "Self-test failed!"

    # 2. Mathematical Invariants
    print("\n--- 2. MATHEMATICAL PROPERTY INVARIANTS ---")
    invariants = run_property_invariants_check()
    for k, v in invariants.items():
        status = "PASS" if v else "FAIL"
        print(f"  [{status:4s}] {k}")
    assert all(invariants.values()), "Property invariant check failed!"

    # 3. 1,000 Fuzz Sessions
    print("\n--- 3. 1,000-SESSION FUZZ SIMULATION ---")
    fuzz = run_fuzz_sessions(session_count=1000)
    print(f"  Sessions Simulated : {fuzz['sessions_run']}")
    print(f"  Deleted Lines      : {fuzz['deleted_lines']}")
    print(f"  Duplicated Lines   : {fuzz['duplicated_lines']}")
    print(f"  Invented Lines     : {fuzz['invented_lines']}")
    print(f"  Overall Verdict    : {'PASS' if fuzz['passed'] else 'FAIL'}")
    assert fuzz["passed"], "Fuzz testing failed (nonzero deleted, duplicated, or invented lines)!"

    # 4. Pytest Test Suite
    print("\n--- 4. PYTEST TEST RUNNER (42 TESTS) ---")
    test_files = [
        "tests/test_shadow_master.py",
        "tests/test_frame_pipeline.py",
        "tests/test_ai_vision_extractor.py",
        "tests/test_code_validator.py",
        "tests/test_merge_engine.py",
        "tests/test_injector_safety.py",
        "tests/test_failsafes.py",
        "tests/test_session_reconciler.py",
        "tests/test_capture_robustness.py",
        "tests/test_failure_scenarios_asserted.py",
        "tests/test_harness_and_properties.py",
        "tests/test_bot_pipeline_integration.py",
    ]
    ret = pytest.main(["-q", "--tb=short"] + test_files)
    assert ret == 0, "Pytest test suite failed!"

    print("\n" + "=" * 70)
    print("    ALL 42 TESTS & 1,000 FUZZ SESSIONS VERIFIED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    main()
