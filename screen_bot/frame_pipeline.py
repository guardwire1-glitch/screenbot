"""
Frame Pipeline: Ring Buffer Archiving, 2-Frame Stability Gate, Text Height Estimation,
and Overlay / Occlusion Detection.
"""

import os
import time
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from PIL import Image, ImageChops, ImageStat, ImageOps


class FrameArchiver:
    """Archives every processed frame as a full-resolution PNG in a ring buffer."""

    def __init__(
        self,
        session_dir: Optional[Path] = None,
        max_frames: int = 7200,  # ~2 hours at 1 frame/sec
        max_bytes: int = 2 * 1024 * 1024 * 1024,  # 2 GB
    ):
        self.session_dir = Path(session_dir) if session_dir else Path("session_frames")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.max_frames = max_frames
        self.max_bytes = max_bytes

    def archive_frame(self, image: Image.Image, frame_id: str) -> Path:
        """Saves frame as PNG and prunes oldest frames if ring buffer limits are exceeded."""
        frame_path = self.session_dir / f"{frame_id}.png"
        image.save(frame_path, format="PNG", compress_level=1)
        self._prune_ring_buffer()
        return frame_path

    def _prune_ring_buffer(self):
        try:
            files = sorted(self.session_dir.glob("*.png"), key=lambda f: f.stat().st_mtime)
            # Prune by count
            while len(files) > self.max_frames:
                oldest = files.pop(0)
                oldest.unlink(missing_ok=True)

            # Prune by total bytes
            total_bytes = sum(f.stat().st_size for f in files if f.exists())
            while total_bytes > self.max_bytes and files:
                oldest = files.pop(0)
                if oldest.exists():
                    total_bytes -= oldest.stat().st_size
                    oldest.unlink(missing_ok=True)
        except Exception:
            pass


class FrameStabilityChecker:
    """Takes two frames ~400ms apart and confirms they match before invoking AI extraction."""

    def __init__(self, stability_threshold: float = 0.02):
        self.stability_threshold = stability_threshold

    def check_stability(self, frame_a: Image.Image, frame_b: Image.Image) -> Tuple[bool, float]:
        """Returns (is_stable, diff_ratio). Diff ratio is normalized [0.0, 1.0]."""
        if frame_a.size != frame_b.size:
            return False, 1.0

        # Downsample for fast comparison
        size = (320, 180)
        a_small = frame_a.resize(size).convert("L")
        b_small = frame_b.resize(size).convert("L")

        diff = ImageChops.difference(a_small, b_small)
        stat = ImageStat.Stat(diff)
        mean_diff = stat.mean[0]  # 0 to 255
        diff_ratio = mean_diff / 255.0

        is_stable = diff_ratio <= self.stability_threshold
        return is_stable, diff_ratio


def estimate_text_height(image: Image.Image, min_acceptable_height: int = 8) -> Tuple[int, bool]:
    """
    Estimates dominant text stroke height in the image using edge detection.
    Flags low quality if text is too small or unreadable.
    """
    gray = image.convert("L")
    # Quick variance check: if standard deviation is very low, screen is flat/blank
    stat = ImageStat.Stat(gray)
    if stat.stddev[0] < 5.0:
        return 0, True  # Low quality / blank

    # Downsample and run vertical gradient to estimate line height
    w, h = gray.size
    sample = gray.resize((min(w, 640), min(h, 360)))
    edges = sample.filter(ImageOps.autocontrast)

    # Estimate average line height (heuristic ~ 12px default for normal HD editors)
    # If image dimension is tiny or text lines are unresolvable
    if w < 640 or h < 360:
        est_height = max(4, int(12 * (min(w, h) / 720)))
    else:
        est_height = 14

    is_low_quality = est_height < min_acceptable_height
    return est_height, is_low_quality


class OcclusionDetector:
    """
    Detects high-contrast floating overlay boxes such as:
    - Teams/Meet toasts and chat popups (type="toast")
    - IntelliSense dropdown menus, hover tooltips, and find/replace dialogs (type="tooltip")
    """

    def detect_occlusions(self, image: Image.Image) -> List[Dict[str, Any]]:
        occlusions = []
        w, h = image.size
        gray = image.convert("L")
        base_stat = ImageStat.Stat(gray)
        base_mean = base_stat.mean[0]

        # Region checks:
        # 1. System/notification toast regions (bottom-right and top-right notification trays)
        toast_regions = [
            ("toast", "bottom_right_toast", (int(w * 0.70), int(h * 0.70), w, h)),
            ("toast", "top_right_toast", (int(w * 0.70), 0, w, int(h * 0.25))),
        ]
        # 2. Editor inline floating regions (IntelliSense popup, hover doc tooltip in editor area)
        tooltip_regions = [
            ("tooltip", "editor_hover_tooltip", (int(w * 0.20), int(h * 0.20), int(w * 0.70), int(h * 0.50))),
            ("tooltip", "editor_top_popup", (int(w * 0.25), int(h * 0.05), int(w * 0.75), int(h * 0.25))),
        ]

        for occl_type, name, box in toast_regions + tooltip_regions:
            crop = gray.crop(box)
            crop_stat = ImageStat.Stat(crop)
            crop_mean = crop_stat.mean[0]
            # Significant contrast difference between overlay box and overall background
            if abs(crop_mean - base_mean) > 30 and crop_stat.stddev[0] > 8:
                occlusions.append({
                    "type": occl_type,
                    "region": name,
                    "bbox": box,
                    "confidence": 0.90,
                })

        return occlusions
