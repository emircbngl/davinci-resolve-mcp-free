"""Find and capture DaVinci Resolve's windows on macOS (Quartz + screencapture).

pyobjc's Quartz is optional (``pip install '.[ui]'``); without it every helper
reports "unavailable" instead of failing to import. Captures go to a fresh path
and count only when screencapture succeeded and wrote bytes, so a failed capture
can never hand back an earlier image.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _quartz() -> Any | None:
    try:
        import Quartz
        return Quartz
    except ImportError:
        return None


def screen_recording_available() -> bool:
    """Return whether macOS has granted screen-recording access without prompting."""
    quartz = _quartz()
    if quartz is None:
        return False
    try:
        return bool(quartz.CGPreflightScreenCaptureAccess())
    except (AttributeError, RuntimeError):
        return False


def find_resolve_windows() -> tuple[list[dict], list[dict]]:
    """Return on-screen and all eligible Resolve windows, respectively."""
    quartz = _quartz()
    if quartz is None:
        return [], []
    try:
        windows = quartz.CGWindowListCopyWindowInfo(
            quartz.kCGWindowListOptionAll, quartz.kCGNullWindowID
        )
    except (AttributeError, RuntimeError):
        return [], []

    all_windows: list[dict] = []
    for window in windows:
        if window.get(quartz.kCGWindowLayer, 0) != 0:
            continue
        if "Resolve" not in str(window.get(quartz.kCGWindowOwnerName, "")):
            continue
        bounds = window.get(quartz.kCGWindowBounds, {})
        width = float(bounds.get("Width", 0))
        height = float(bounds.get("Height", 0))
        if width <= 0 or height <= 0:
            continue
        all_windows.append({
            "id": int(window[quartz.kCGWindowNumber]),
            "name": str(window.get(quartz.kCGWindowName, "")),
            "bounds": dict(bounds),
            "onscreen": bool(window.get(quartz.kCGWindowIsOnscreen, False)),
        })
    return [window for window in all_windows if window["onscreen"]], all_windows


def png_size(path: Path) -> tuple[int, int] | None:
    """(width, height) from a PNG header, without an imaging library."""
    try:
        with path.open("rb") as f:
            header = f.read(24)
    except OSError:
        return None
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def capture_window(window_id: int, out_path: Path) -> bool:
    """Capture one window and report whether screencapture produced image bytes."""
    try:
        out_path.unlink(missing_ok=True)
        process = subprocess.run(
            ["screencapture", "-x", "-o", "-l", str(window_id), "-t", "png", str(out_path)],
            capture_output=True, text=True, timeout=15.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    try:
        return process.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0
    except OSError:
        return False
