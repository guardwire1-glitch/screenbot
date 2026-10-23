"""
High-performance screen capture module with Windows DPI awareness,
active window targeting, mss acceleration, and sleeping/locked display backoff.
"""

import io
import time
import ctypes
from ctypes import wintypes
from typing import Optional, Tuple
from PIL import Image, ImageGrab, ImageStat

# Enable DPI awareness for crisp, 1:1 pixel captures across all Windows scaling factors (125%, 150%, 200%)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class CaptureBackoffManager:
    """Manages backoff delays (5s, 10s, 30s) during locked screen, sleeping display, or minimized window."""

    BACKOFF_STEPS = [5.0, 10.0, 30.0]

    def __init__(self):
        self.failures = 0
        self.last_failure_time = 0.0
        self.current_backoff = 0.0

    def record_failure(self, reason: str = "") -> float:
        self.failures += 1
        idx = min(self.failures - 1, len(self.BACKOFF_STEPS) - 1)
        self.current_backoff = self.BACKOFF_STEPS[idx]
        self.last_failure_time = time.time()
        return self.current_backoff

    def record_success(self):
        self.failures = 0
        self.current_backoff = 0.0

    def reset(self):
        self.record_success()

    def get_backoff_delay(self) -> float:
        return self.current_backoff

    def should_skip_tick(self) -> bool:
        if self.current_backoff <= 0:
            return False
        return (time.time() - self.last_failure_time) < self.current_backoff


def is_frame_black_or_blank(image: Optional[Image.Image]) -> bool:
    """Detects black frames, blank unrendered screens, or display sleep artifacts."""
    if image is None:
        return True
    try:
        stat = ImageStat.Stat(image.convert("L"))
        # Pure black or flat solid color has standard deviation < 2.0
        if stat.stddev[0] < 2.0:
            return True
        # Extremely dark image (mean < 3.0)
        if stat.mean[0] < 3.0:
            return True
        return False
    except Exception:
        return True


def get_active_window_rect(hwnd: Optional[int] = None) -> Optional[Tuple[int, int, int, int]]:
    """Returns the (left, top, right, bottom) bounding box of target or active window, or None."""
    try:
        target = hwnd or user32.GetForegroundWindow()
        if not target:
            return None
        rect = wintypes.RECT()
        if user32.GetWindowRect(target, ctypes.byref(rect)):
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width > 50 and height > 50:
                return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return None


def get_cursor_region_rect(box_width: int = 800, box_height: int = 600) -> Optional[Tuple[int, int, int, int]]:
    """Returns a bounding box centered around the current mouse cursor."""
    try:
        pt = POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            half_w = box_width // 2
            half_h = box_height // 2
            return (pt.x - half_w, pt.y - half_h, pt.x + half_w, pt.y + half_h)
    except Exception:
        pass
    return None


def get_split_screen_opposite_rect() -> Optional[Tuple[int, int, int, int]]:
    """If the active foreground window is snapped to one half (split-screen),
    returns the bounding box of the opposite half where YouTube/Teams/Zoom is playing."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

        fg_left, fg_top, fg_right, fg_bottom = rect.left, rect.top, rect.right, rect.bottom

        # Virtual desktop dimensions
        SM_CXVIRTUALSCREEN = 78
        SM_CYVIRTUALSCREEN = 79
        SM_XVIRTUALSCREEN = 76
        SM_YVIRTUALSCREEN = 77

        scr_w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        scr_h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        scr_x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        scr_y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)

        # Foreground window is on right half -> lecture is on left half
        if fg_left >= scr_x + int(scr_w * 0.35):
            return (scr_x, scr_y, fg_left, scr_y + scr_h)
        # Foreground window is on left half -> lecture is on right half
        elif fg_right <= scr_x + int(scr_w * 0.65):
            return (fg_right, scr_y, scr_x + scr_w, scr_y + scr_h)
    except Exception:
        pass
    return None


def capture_screen_image(
    mode: str = "active_window",
    target_hwnd: Optional[int] = None,
    target_region: Optional[Tuple[int, int, int, int]] = None,
    max_dim: int = 1920,
) -> Optional[Image.Image]:
    """Captures the screen using mss first with ImageGrab fallback."""
    bbox = target_region
    if not bbox:
        if target_hwnd:
            bbox = get_active_window_rect(target_hwnd)
        elif mode == "lecture":
            bbox = get_split_screen_opposite_rect()
        elif mode == "cursor_region":
            bbox = get_cursor_region_rect()
        elif mode == "active_window":
            bbox = get_active_window_rect()

    img = None

    # Strategy 1: mss (high-speed GDI capture)
    try:
        import mss
        with mss.mss() as sct:
            if bbox:
                l, t, r, b = bbox
                mon = {"left": l, "top": t, "width": max(1, r - l), "height": max(1, b - t)}
            else:
                mon = sct.monitors[0]
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception:
        img = None

    # Strategy 2: ImageGrab fallback
    if img is None:
        try:
            if bbox:
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
            else:
                img = ImageGrab.grab(all_screens=True)
        except Exception:
            try:
                img = ImageGrab.grab(all_screens=False)
            except Exception:
                img = None

    if img is None or is_frame_black_or_blank(img):
        return None

    # Downscale if exceeding max_dim
    if max_dim and (img.width > max_dim or img.height > max_dim):
        ratio = min(max_dim / img.width, max_dim / img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    return img


def capture_screen_image_and_bytes(mode: str = "active_window", max_dim: int = 1920, quality: int = 85):
    """Captures the screen and returns both PIL Image and compressed JPEG bytes in one call."""
    img = capture_screen_image(mode=mode, max_dim=max_dim)
    if img is None:
        return None, b""
    export_img = img
    if export_img.mode in ("RGBA", "P"):
        export_img = export_img.convert("RGB")
    buf = io.BytesIO()
    export_img.save(buf, format="JPEG", quality=quality, optimize=True)
    return img, buf.getvalue()


def capture_screen_bytes(mode: str = "active_window", max_dim: int = 1920, quality: int = 85) -> bytes:
    """Captures the screen and compresses to JPEG bytes directly in memory."""
    _, b = capture_screen_image_and_bytes(mode=mode, max_dim=max_dim, quality=quality)
    return b


def compute_frame_diff(img1: Optional[Image.Image], img2: Optional[Image.Image]) -> float:
    """Computes mean pixel difference between two frames in ~5ms."""
    if img1 is None or img2 is None:
        return 999.0
    try:
        from PIL import ImageChops, ImageStat
        s1 = img1.resize((160, 120), Image.Resampling.BILINEAR).convert("L")
        s2 = img2.resize((160, 120), Image.Resampling.BILINEAR).convert("L")
        diff = ImageChops.difference(s1, s2)
        return float(ImageStat.Stat(diff).mean[0])
    except Exception:
        return 999.0
