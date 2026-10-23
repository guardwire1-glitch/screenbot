"""
Tests for Frame Pipeline:
- Frame Ring Buffer Archiver (saves full-res PNGs in session_frames/, ring buffer pruning).
- Two-frame stability check (~400ms delta, pixel difference gate).
- Text height estimation & low-quality frame flagging.
- Overlay / Occlusion detection (toasts, tooltips, IntelliSense, caret, selection).
"""

import time
import pytest
from pathlib import Path
from PIL import Image, ImageDraw

from screen_bot.frame_pipeline import (
    FrameArchiver,
    FrameStabilityChecker,
    OcclusionDetector,
    estimate_text_height,
)


def test_frame_archiver_saves_and_prunes(tmp_path):
    archiver = FrameArchiver(session_dir=tmp_path / "session_frames", max_frames=5, max_bytes=100 * 1024 * 1024)
    
    img = Image.new("RGB", (100, 100), color=(255, 255, 255))
    saved_paths = []
    for i in range(7):
        p = archiver.archive_frame(img, frame_id=f"frame_{i:03d}")
        saved_paths.append(p)
        time.sleep(0.01)
        
    current_files = list(archiver.session_dir.glob("*.png"))
    assert len(current_files) <= 5  # Pruned to ring buffer capacity
    assert archiver.session_dir.joinpath("frame_006.png").exists()


def test_frame_stability_checker_accepts_stable_frames():
    checker = FrameStabilityChecker(stability_threshold=0.01)
    
    frame1 = Image.new("RGB", (200, 200), color=(255, 255, 255))
    frame2 = Image.new("RGB", (200, 200), color=(255, 255, 255))
    
    is_stable, diff_ratio = checker.check_stability(frame1, frame2)
    assert is_stable is True
    assert diff_ratio == 0.0


def test_frame_stability_checker_rejects_animating_or_buffering_frames():
    checker = FrameStabilityChecker(stability_threshold=0.02)
    
    frame1 = Image.new("RGB", (200, 200), color=(255, 255, 255))
    frame2 = Image.new("RGB", (200, 200), color=(255, 255, 255))
    
    # Draw significant change (e.g. video buffer spinner or large scrolling motion)
    draw = ImageDraw.Draw(frame2)
    draw.rectangle([50, 50, 150, 150], fill=(0, 0, 0))
    
    is_stable, diff_ratio = checker.check_stability(frame1, frame2)
    assert is_stable is False
    assert diff_ratio > 0.05


def test_text_height_estimator_flags_low_quality():
    # Blank image has zero estimated text height -> low quality
    blank_img = Image.new("RGB", (300, 300), color=(30, 30, 30))
    height, is_low_quality = estimate_text_height(blank_img, min_acceptable_height=8)
    assert is_low_quality is True


def test_occlusion_detector_identifies_tooltip_or_toast():
    detector = OcclusionDetector()
    
    img = Image.new("RGB", (800, 600), color=(30, 30, 30))
    draw = ImageDraw.Draw(img)
    # Draw bright floating toast/tooltip box in bottom-right corner
    draw.rectangle([600, 450, 780, 550], fill=(240, 240, 240), outline=(200, 200, 200))
    
    occlusions = detector.detect_occlusions(img)
    assert len(occlusions) > 0
    assert any(occ["type"] in ("toast", "tooltip", "tooltip_or_toast", "floating_overlay") for occ in occlusions)
    assert any(occ["type"] == "toast" for occ in occlusions)
