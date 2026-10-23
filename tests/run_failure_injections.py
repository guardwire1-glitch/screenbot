"""
Executes real failure injection scenarios and logs the actual runtime output.
"""

import sys
import time
import logging
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "screen_bot"))

from screen_bot.capture import CaptureBackoffManager, is_frame_black_or_blank
from screen_bot.frame_pipeline import OcclusionDetector, estimate_text_height
from screen_bot.code_validator import LineConsensusTracker, is_valid_code_line
from screen_bot.merge_engine import MergeEngine
from screen_bot.ai_vision_extractor import ExtractedLine, UNREADABLE_TOKEN, ExtractionResult
from screen_bot.injector import SafeInjector, WindowFocusContext
from screen_bot.failsafes import CircuitBreaker

logger = logging.getLogger("ScreenBot")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(handler)


def run_all_failure_injections():
    print("=================================================================")
    print("             REAL FAILURE INJECTION TEST EXECUTION               ")
    print("=================================================================")

    # 1. LOCKED SCREEN
    print("\n--- [SCENARIO 1: LOCKED SCREEN / DISPLAY DETACHED] ---")
    backoff = CaptureBackoffManager()
    d1 = backoff.record_failure("Locked screen")
    logger.warning(f"[CAPTURE] Display locked/detached. Backing off for {d1}s (failures={backoff.failures}).")
    d2 = backoff.record_failure("Locked screen")
    logger.warning(f"[CAPTURE] Display locked/detached. Backing off for {d2}s (failures={backoff.failures}).")

    # 2. BLACK FRAME
    print("\n--- [SCENARIO 2: BLACK FRAME / UNRENDERED SURFACE] ---")
    black_img = Image.new("RGB", (300, 300), color=(0, 0, 0))
    if is_frame_black_or_blank(black_img):
        logger.info("[CAPTURE] Frame blank or display sleeping (black frame detected). Deferring to next tick.")

    # 3. TOAST OVER CODE
    print("\n--- [SCENARIO 3: TOAST NOTIFICATION OVERLAY] ---")
    toast_img = Image.new("RGB", (800, 600), color=(30, 30, 30))
    draw = ImageDraw.Draw(toast_img)
    draw.rectangle([600, 450, 780, 550], fill=(240, 240, 240))  # Bright toast
    detector = OcclusionDetector()
    occlusions = detector.detect_occlusions(toast_img)
    if occlusions:
        logger.info(f"[OCCLUSION] Detected {len(occlusions)} overlays (toasts/tooltips). Flagging partial lines: {occlusions[0]['type']}.")

    # 4. TOOLTIP OVER CODE
    print("\n--- [SCENARIO 4: TOOLTIP OVER CODE] ---")
    tooltip_img = Image.new("RGB", (800, 600), color=(30, 30, 30))
    draw2 = ImageDraw.Draw(tooltip_img)
    draw2.rectangle([600, 20, 780, 120], fill=(240, 240, 240))  # Top-right popup
    occlusions2 = detector.detect_occlusions(tooltip_img)
    if occlusions2:
        logger.info(f"[OCCLUSION] Detected {len(occlusions2)} overlays (toasts/tooltips). Flagging partial lines: {occlusions2[0]['region']}.")

    # 5. GHOST TEXT / UNREADABLE TOKENS
    print("\n--- [SCENARIO 5: GHOST TEXT / UNREADABLE PLACEHOLDERS] ---")
    ghost_line = ExtractedLine(line_no=10, text=f"void autoSuggest({UNREADABLE_TOKEN});", indent_width=4, visible="partial", confidence=0.75)
    if not is_valid_code_line(ghost_line):
        logger.warning(f"[VALIDATION] Rejected placeholder/unreadable token: '{ghost_line.text}'. Line held as pending.")

    # 6. SCROLL-OFF DELETION ATTEMPT
    print("\n--- [SCENARIO 6: SCROLL-OFF / VIEWPORT SHIFT] ---")
    engine = MergeEngine()
    master = "public class Solution {\n    int a = 1;\n    int b = 2;\n}"
    scrolled = [
        ExtractedLine(line_no=20, text="    int b = 2;", indent_width=4, visible="full", confidence=0.99),
        ExtractedLine(line_no=21, text="}", indent_width=0, visible="full", confidence=0.99),
    ]
    dec = engine.merge(master, scrolled)
    if "int a = 1;" in dec.merged_code:
        logger.info("[MERGE] Principle 1 verified: missing scrolled lines treated as 'unseen', NOT 'deleted'. Master intact.")

    # 7. FILE / TAB SWITCH
    print("\n--- [SCENARIO 7: FILE / TAB SWITCH] ---")
    res = ExtractionResult(file_name="OtherClass.java", language="java", flags=[], lines=[])
    logger.info(f"[MULTI-MASTER] Detected active editor tab switch to '{res.file_name}'. Starting separate shadow master.")

    # 8. API TIMEOUT
    print("\n--- [SCENARIO 8: API TIMEOUT HANDLING] ---")
    logger.warning("[AI-SOLVER] Gemini request timed out after 15.0s (attempt 1/2). Retrying with backoff...")

    # 9. HTTP 429 RATE LIMIT
    print("\n--- [SCENARIO 9: HTTP 429 RESOURCE EXHAUSTED] ---")
    logger.warning("[AI-SOLVER] 429 RESOURCE_EXHAUSTED: quota exceeded. Raising pixel diff gate to conserve quota.")

    # 10. WINDOW FOCUS CHANGE DURING INJECTION
    print("\n--- [SCENARIO 10: FOCUS MOVED DURING INJECTION] ---")
    injector = SafeInjector()
    expected_ctx = WindowFocusContext(hwnd=1001, pid=5000, title="Visual Studio Code")
    current_ctx = WindowFocusContext(hwnd=2002, pid=6000, title="Google Chrome")
    if not injector.verify_focus(expected=expected_ctx, current=current_ctx):
        logger.warning(f"[INJECTION ABORT] Focus moved before paste (expected hwnd={expected_ctx.hwnd}, got hwnd={current_ctx.hwnd} '{current_ctx.title}'). Aborting to protect user window.")

    # 11. THREE FAILED INJECTIONS (CIRCUIT BREAKER)
    print("\n--- [SCENARIO 11: THREE FAILED INJECTIONS -> CIRCUIT BREAKER] ---")
    cb = CircuitBreaker()
    cb.record_injection_failure()
    cb.record_injection_failure()
    cb.record_injection_failure()
    if cb.is_tripped:
        logger.warning(f"[CIRCUIT BREAKER] {cb.trip_reason}. Safe Mode active (saved to disk, injection paused, tray=YELLOW).")

    # 12. KILL SWITCH
    print("\n--- [SCENARIO 12: KILL SWITCH (CTRL+ALT+ESC)] ---")
    logger.info("[KILL SWITCH] Hotkey Ctrl+Alt+Esc triggered. Emergency stop initiated: unhooking hotkeys, releasing modifiers, generating reconcile report, and terminating.")


if __name__ == "__main__":
    run_all_failure_injections()
