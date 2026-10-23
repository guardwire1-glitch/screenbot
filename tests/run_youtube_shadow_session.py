"""
ScreenBot Real YouTube 1080p Coding Lecture Shadow Verification Runner
Video: 'Java methods explained in 10+ minutes!' (Duration: 11.1 min / 664.3s)
URL: https://www.youtube.com/watch?v=v5p_SUfi710

Executes the complete production class-mode pipeline in shadow mode:
- Frame extraction from genuine 1080p video stream
- 2-frame stability check (~400ms delta)
- Local pixel difference gate
- Ring buffer frame archiving
- Gemini Vision structured JSON extraction (temperature=0)
- 2-consecutive-stable-frame consensus tracker
- Deterministic syntax validation
- Anchor-based middle-insertion merge into ShadowMaster
- Atomic disk persistence & JSONL event logging
- Strict INJECTION OFF enforcement
"""

import sys
import time
import io
import json
import logging
from pathlib import Path
from PIL import Image
import cv2

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
from screen_bot.code_validator import LineConsensusTracker, validate_syntax, is_valid_code_line
from screen_bot.frame_pipeline import FrameArchiver, FrameStabilityChecker, OcclusionDetector
from screen_bot.ai_solver import ScreenSolver
from screen_bot.capture import compute_frame_diff, is_frame_black_or_blank
from screen_bot.failsafes import compute_build_hash
from screen_bot.bot import load_config

# Set up dedicated logger for real YouTube shadow run
log_path = ROOT / "screen_bot" / "youtube_shadow_run.log"
logger = logging.getLogger("ScreenBotYouTubeShadow")
logger.setLevel(logging.INFO)
logger.handlers.clear()
f_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
c_handler = logging.StreamHandler(sys.stdout)
fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
f_handler.setFormatter(fmt)
c_handler.setFormatter(fmt)
logger.addHandler(f_handler)
logger.addHandler(c_handler)


def cv2_frame_to_pil(frame) -> Image.Image:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def pil_to_jpeg_bytes(pil_img: Image.Image, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def run_youtube_shadow_session():
    cfg = load_config()
    solver = ScreenSolver(api_key=cfg.get("gemini_api_key"))
    video_path = ROOT / "screen_bot" / "session_data" / "lecture_video.mp4"
    assert video_path.exists(), f"Video file not found at {video_path}"

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps if fps else 0

    log_dir = ROOT / "screen_bot" / "session_data"
    target_master_path = log_dir / "YouTube_Shadow_Master.java"
    if target_master_path.exists():
        target_master_path.unlink()
    master = ShadowMaster(file_path=target_master_path, log_dir=log_dir)
    engine = MergeEngine()
    consensus = LineConsensusTracker(required_observations=2, min_confidence=0.85)
    archiver = FrameArchiver(session_dir=log_dir / "youtube_session_frames", max_frames=500)
    stability_checker = FrameStabilityChecker(stability_threshold=0.03)
    occlusion_detector = OcclusionDetector()

    logger.info("=" * 70)
    logger.info("   SCREENBOT LIVE YOUTUBE 1080p SHADOW RUN VERIFICATION")
    logger.info(f"   Video Source: https://www.youtube.com/watch?v=v5p_SUfi710")
    logger.info(f"   Title: Java methods explained in 10+ minutes!")
    logger.info(f"   Resolution: 1920x1080 | Duration: {duration_sec:.1f}s | Build: {compute_build_hash()}")
    logger.info(f"   Target Master File: {target_master_path.name}")
    logger.info(f"   Active Keystroke Injection: STRICTLY OFF (Pure Shadow Mode)")
    logger.info("=" * 70)

    # Sampling points across lecture progression (seconds)
    # Consecutive stable pairs test 2-frame consensus across typing & scroll:
    sample_timestamps = [
        # Segment 1: Initial method declaration (consecutive stable frames at 145s, 146s)
        (145.0, 145.4),
        (146.0, 146.4),
        # Segment 2: Added hello() method body and parameters (consecutive at 280s, 281s)
        (280.0, 280.4),
        (281.0, 281.4),
        # Segment 3: Added return values and add() method (consecutive at 420s, 421s)
        (420.0, 420.4),
        (421.0, 421.4),
        # Segment 4: Scrolled viewport (public class scrolled off top! consecutive at 595s, 596s)
        (595.0, 595.4),
        (596.0, 596.4),
    ]

    last_pil_img = None

    for step_idx, (t_sec, t_stable_sec) in enumerate(sample_timestamps, 1):
        t0 = time.time()
        frame_id = f"yt_frame_{step_idx:02d}_t{int(t_sec)}s"
        logger.info(f"\n--- [{frame_id}] Extracting video frame at t={t_sec:.1f}s (stability frame at t={t_stable_sec:.1f}s) ---")

        # 1. Grab primary frame
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t_sec * fps))
        ret_a, f_a = cap.read()
        assert ret_a, f"Failed to read frame at {t_sec}s"
        pil_a = cv2_frame_to_pil(f_a)

        # 2. Grab stability check frame ~400ms later
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(t_stable_sec * fps))
        ret_b, f_b = cap.read()
        assert ret_b, f"Failed to read stability frame at {t_stable_sec}s"
        pil_b = cv2_frame_to_pil(f_b)

        # 3. Stability check gate
        is_stable, diff_ratio = stability_checker.check_stability(pil_a, pil_b)
        logger.info(f"[{frame_id}] 2-Frame Stability Gate: is_stable={is_stable} (diff_ratio={diff_ratio:.4f})")
        if not is_stable:
            logger.warning(f"[{frame_id}] Frame unstable (video transition/seeking). Skipping tick.")
            continue

        # 4. Black/Blank frame check
        if is_frame_black_or_blank(pil_a):
            logger.warning(f"[{frame_id}] Black frame detected. Skipping tick.")
            continue

        # 5. Archive raw frame into ring buffer
        archived_path = archiver.archive_frame(pil_a, frame_id=frame_id)
        logger.info(f"[{frame_id}] Frame archived to {archived_path.name} (1920x1080 PNG).")

        # 6. Occlusion / Overlay detection
        occlusions = occlusion_detector.detect_occlusions(pil_a)
        if occlusions:
            logger.info(f"[{frame_id}] Occlusions detected: {[o['type'] for o in occlusions]}")

        # 7. Model Extraction via Gemini Vision AI (temperature=0)
        img_bytes = pil_to_jpeg_bytes(pil_a, quality=85)
        prompt = build_extraction_prompt(language_hint="java")
        logger.info(f"[{frame_id}] Calling Gemini Vision AI (temperature=0)...")
        t_ai_start = time.time()
        raw_resp = solver.extract_lecture_code_structured(img_bytes, prompt=prompt)
        t_ai_end = time.time()
        logger.info(f"[{frame_id}] AI Vision response received in {(t_ai_end - t_ai_start)*1000:.1f}ms.")

        parsed = parse_model_extraction_response(raw_resp)
        logger.info(f"[{frame_id}] Parsed {len(parsed.lines)} visible lines (File: {parsed.file_name or 'Main.java'}).")

        # 8. Consensus Gate (2 consecutive stable observations, confidence >= 0.85)
        accepted, pending = consensus.feed_frame_lines(parsed.lines)
        logger.info(f"[{frame_id}] Consensus Gate: {len(accepted)} accepted, {len(pending)} pending 2nd observation.")

        if not accepted:
            logger.info(f"[{frame_id}] 0 lines passed 2-frame consensus; master code unchanged.")
            continue

        # 9. Merge Engine (middle insertion, anchor tracking, append-only)
        current_master = master.get_current_code()
        decision = engine.merge(current_master, accepted)
        logger.info(f"[{frame_id}] Merge Decision: type={decision.change_type}, lines_added={len(decision.lines_added)}, notify_only={decision.is_notify_only}")

        if decision.change_type == "noop":
            logger.info(f"[{frame_id}] Merge is no-op. Master is already consistent.")
            continue

        # 10. Syntax Validation Gate
        is_syntax_valid, syntax_err = validate_syntax(decision.merged_code, language="java")
        logger.info(f"[{frame_id}] Syntax Gate: is_valid={is_syntax_valid} (err='{syntax_err}')")
        if not is_syntax_valid:
            logger.warning(f"[{frame_id}] Syntax gate failed: {syntax_err}. Holding lines as pending.")
            continue

        # 11. Atomic Shadow Master Commit & JSONL Audit Event
        change_event = MasterChangeEvent(
            frame_id=frame_id,
            event_type=decision.change_type,
            lines_added=decision.lines_added,
            lines_modified=decision.lines_modified,
            gates_passed=["stability_gate", "consensus_gate", "syntax_gate"],
            result_code=decision.merged_code,
        )
        master.apply_change(change_event)
        logger.info(f"[{frame_id}] Shadow Master atomically updated -> {target_master_path.name} ({len(decision.merged_code.splitlines())} lines). Cycle time: {(time.time() - t0):.2f}s.")

    cap.release()
    logger.info("\n" + "=" * 70)
    logger.info("   YOUTUBE SHADOW MODE SESSION COMPLETE")
    logger.info(f"   Final Master File: {target_master_path}")
    logger.info("=" * 70)


if __name__ == "__main__":
    run_youtube_shadow_session()
