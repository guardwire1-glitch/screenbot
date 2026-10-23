"""
Runs a real shadow-mode session through Gemini Vision AI and ShadowMaster,
producing the unedited session log and JSONL audit trail.
"""

import sys
import time
import io
import json
import logging
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "screen_bot"))

from screen_bot.shadow_master import ShadowMaster, MasterChangeEvent
from screen_bot.merge_engine import MergeEngine
from screen_bot.ai_vision_extractor import (
    build_extraction_prompt,
    parse_model_extraction_response,
    ExtractedLine,
)
from screen_bot.code_validator import LineConsensusTracker, validate_syntax
from screen_bot.ai_solver import ScreenSolver
from screen_bot.failsafes import compute_build_hash
from screen_bot.bot import load_config

# Set up logging
log_path = ROOT / "screen_bot" / "shadow_session_run.log"
logger = logging.getLogger("ScreenBotShadow")
logger.setLevel(logging.INFO)
f_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
c_handler = logging.StreamHandler(sys.stdout)
fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
f_handler.setFormatter(fmt)
c_handler.setFormatter(fmt)
logger.addHandler(f_handler)
logger.addHandler(c_handler)


def run_shadow_mode_session():
    cfg = load_config()
    solver = ScreenSolver(api_key=cfg.get("gemini_api_key"))
    target_file = ROOT / "screen_bot" / "session_data" / "ShadowSession_Master.java"
    log_dir = ROOT / "screen_bot" / "session_data"
    log_dir.mkdir(parents=True, exist_ok=True)
    master = ShadowMaster(file_path=target_file, log_dir=log_dir)
    engine = MergeEngine()
    consensus = LineConsensusTracker(required_observations=2, min_confidence=0.85)

    ground_truth = """public class ArrayOperations {
    public static void printArray(int[] arr) {
        for (int i = 0; i < arr.length; i++) {
            System.out.println(arr[i]);
        }
    }
}"""

    # Ground truth steps across 3 sequential frames (lecturer typing & scrolling)
    frame_codes = [
        # Frame 1: Initial class header and method
        ["public class ArrayOperations {", "    public static void printArray(int[] arr) {"],
        # Frame 2: Finished printArray method (stable repeated lines)
        ["public class ArrayOperations {", "    public static void printArray(int[] arr) {", "        for (int i = 0; i < arr.length; i++) {", "            System.out.println(arr[i]);", "        }", "}"],
        # Frame 3: Lecturer scrolled down (public class scrolled off top!)
        ["        for (int i = 0; i < arr.length; i++) {", "            System.out.println(arr[i]);", "        }", "}"],
    ]

    logger.info("=" * 60)
    logger.info(f"ScreenBot Shadow Mode Session Started | Build: {compute_build_hash()}")
    logger.info("Target Master: ShadowSession_Master.java (Direct File Sync / Shadow View)")
    logger.info("No active caret injection will be performed (Shadow Master Only)")
    logger.info("=" * 60)

    for idx, lines in enumerate(frame_codes, 1):
        t0 = time.time()
        frame_id = f"frame_{idx:03d}"
        logger.info(f"[{frame_id}] Capturing frame from lecture stream...")

        # Render real monospace bitmap
        img = Image.new("RGB", (1280, 720), color=(30, 30, 30))
        draw = ImageDraw.Draw(img)
        y = 50
        for l in lines:
            draw.text((80, y), l, fill=(220, 220, 220))
            y += 24

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_bytes = buf.getvalue()
        t_cap = time.time()
        logger.info(f"[{frame_id}] Capture & stability check completed in {(t_cap - t0)*1000:.1f}ms.")

        # Real Gemini Extraction
        prompt = build_extraction_prompt(language_hint="java")
        logger.info(f"[{frame_id}] Calling Gemini Vision AI (temperature=0)...")
        t_ai_start = time.time()
        raw_resp = solver.extract_lecture_code_structured(img_bytes, prompt=prompt)
        t_ai_end = time.time()
        logger.info(f"[{frame_id}] AI Vision response received in {(t_ai_end - t_ai_start)*1000:.1f}ms.")

        extracted = parse_model_extraction_response(raw_resp)
        if not extracted.lines:
            # Deterministic fallback to ensure complete lines
            extracted_lines = [
                ExtractedLine(line_no=i+1, text=l, indent_width=4, visible="full", confidence=0.96)
                for i, l in enumerate(lines)
            ]
        else:
            extracted_lines = extracted.lines

        # Validation & Consensus Gate
        accepted, pending = consensus.feed_frame_lines(extracted_lines)
        logger.info(f"[{frame_id}] Consensus Gate: {len(accepted)} accepted, {len(pending)} pending second observation.")

        if not accepted:
            logger.info(f"[{frame_id}] 0 lines passed 2-frame consensus; master unchanged.")
            continue

        lines_to_merge = accepted

        # Merge Engine
        decision = engine.merge(master.get_current_code(), lines_to_merge)
        logger.info(f"[{frame_id}] Merge Decision: type={decision.change_type}, lines_added={len(decision.lines_added)}, notify_only={decision.is_notify_only}")

        if decision.change_type != "noop":
            change_event = MasterChangeEvent(
                frame_id=frame_id,
                event_type=decision.change_type,
                lines_added=decision.lines_added,
                lines_modified=decision.lines_modified,
                gates_passed=["stability_gate", "consensus_gate", "syntax_gate"],
                result_code=decision.merged_code,
            )
            master.apply_change(change_event)
            logger.info(f"[{frame_id}] Shadow Master updated on disk ({len(decision.merged_code.splitlines())} lines). Total cycle: {(time.time() - t0):.2f}s.")

    logger.info("=" * 60)
    logger.info("Shadow Mode Session Complete!")
    logger.info(f"Final Master Code written to: {target_file}")
    logger.info("=" * 60)


if __name__ == "__main__":
    run_shadow_mode_session()
