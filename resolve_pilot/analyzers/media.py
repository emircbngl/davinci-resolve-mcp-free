"""ffprobe-based media inspection."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from fractions import Fraction
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
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
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
