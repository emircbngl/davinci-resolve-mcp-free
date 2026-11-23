"""ffprobe-based media inspection."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from fractions import Fraction
from functools import lru_cache
from pathlib import Path


@dataclass
class MediaInfo:
    path: str
    duration_seconds: float
    width: int | None
    height: int | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None
    bit_rate: int | None
    container: str
    # Degrees the picture is tagged to be turned for display (phones: 90 / -90 / 180).
    # width and height are the stored ones; players show a 90-degree clip the other way up.
    rotation: int = 0
    # When the camera says it was recorded (ISO 8601), if it says.
    recorded: str | None = None
    # "hlg" (iPhone HDR) or "pq" (HDR10): the picture is not Rec.709 and looks pale shown as such.
    hdr: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# The rates cameras and timelines use.
_STANDARD_RATES = frozenset(Fraction(n, d) for n, d in (
    (24000, 1001), (24, 1), (25, 1), (30000, 1001), (30, 1), (48000, 1001), (48, 1), (50, 1),
    (60000, 1001), (60, 1), (100, 1), (120000, 1001), (120, 1), (240, 1)))


def _frame_rate(average: str | None, nominal: str | None) -> float | None:
    """A video stream's frame rate from ffprobe's avg_frame_rate and r_frame_rate.

    A phone records a variable rate: frames arrive a little early or late and some are
    dropped, so the average comes out as 23.9989 or 29.755 while the file is a 24 or 30 fps
    one. The nominal rate says which, and it is what Resolve conforms the clip to. A
    timeline made at the average rate imported into Resolve 20.3 as a 24 fps one with its
    audio on two alternating tracks and a one-frame hole.
    """
    def parse(text: str | None) -> Fraction | None:
        try:
            rate = Fraction(text) if text else None
        except (ValueError, ZeroDivisionError):
            return None
        return rate if rate and rate > 0 else None

    avg, nom = parse(average), parse(nominal)
    if avg is None:
        return None
    if nom in _STANDARD_RATES and avg not in _STANDARD_RATES and abs(avg - nom) <= nom / 20:
        return float(nom)
    return float(avg)


def _ffprobe_bin() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RuntimeError("ffprobe not found on PATH. Install with: brew install ffmpeg")
    return path


def probe_media(path: str | Path) -> MediaInfo:
    """Return container/codec/timing info for a media file using ffprobe."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)

    cmd = [
        _ffprobe_bin(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(p),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60.0).stdout
    data = json.loads(out)

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    # Album art in MP3/M4A files shows up as a one-frame "video" stream; it is not footage.
    v = next((s for s in streams if s.get("codec_type") == "video"
              and (s.get("disposition") or {}).get("attached_pic") != 1), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    rotation = 0
    if v:
        tagged = [d.get("rotation") for d in v.get("side_data_list") or []] + [(v.get("tags") or {}).get("rotate")]
        try:
            rotation = next(int(float(r)) for r in tagged if r not in (None, ""))
        except (StopIteration, ValueError):
            rotation = 0

    fps = _frame_rate(v.get("avg_frame_rate"), v.get("r_frame_rate")) if v else None

    return MediaInfo(
        path=str(p),
        duration_seconds=float(fmt.get("duration", 0.0)),
        width=int(v["width"]) if v and v.get("width") else None,
        height=int(v["height"]) if v and v.get("height") else None,
        fps=fps,
        video_codec=v.get("codec_name") if v else None,
        audio_codec=a.get("codec_name") if a else None,
        sample_rate=int(a["sample_rate"]) if a and a.get("sample_rate") else None,
        channels=int(a["channels"]) if a and a.get("channels") else None,
        bit_rate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        container=fmt.get("format_name", ""),
        rotation=rotation,
        recorded=((fmt.get("tags") or {}).get("com.apple.quicktime.creationdate")
                  or (fmt.get("tags") or {}).get("creation_time") or None),
        hdr={"arib-std-b67": "hlg", "smpte2084": "pq"}.get(v.get("color_transfer")) if v else None,
    )


@lru_cache(maxsize=256)
def _probe_cached(path: str, mtime: float, size: int) -> MediaInfo:
    return probe_media(path)


def probe_media_cached(path: str | Path) -> MediaInfo | None:
    """probe_media with a per-(path, mtime, size) cache; None if unreadable.

    Used where a missing file or a missing ffprobe should degrade gracefully
    (plan validation, FCPXML asset attributes) instead of failing the call.
    """
    p = Path(path).expanduser()
    try:
        st = p.stat()
        return _probe_cached(str(p.resolve()), st.st_mtime, st.st_size)
    except Exception:
        return None
