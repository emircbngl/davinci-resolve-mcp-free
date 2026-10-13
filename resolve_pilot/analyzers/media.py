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

    def to_dict(self) -> dict:
        return asdict(self)


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

    fps: float | None = None
    if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
        fps = float(Fraction(v["avg_frame_rate"]))

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
