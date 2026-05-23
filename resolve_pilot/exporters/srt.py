"""SRT subtitle writer. Resolve imports SRT directly onto a subtitle track."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class SrtCue:
    start: float
    end: float
    text: str


def _fmt_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues: list[SrtCue]) -> str:
    """Build SRT text from a list of cues."""
    parts: list[str] = []
    for i, cue in enumerate(cues, start=1):
        parts.append(str(i))
        parts.append(f"{_fmt_timestamp(cue.start)} --> {_fmt_timestamp(cue.end)}")
        parts.append(cue.text.strip())
        parts.append("")
    return "\n".join(parts)


def write_srt(cues: list[SrtCue], out_path: str | Path) -> Path:
    p = Path(out_path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_srt(cues), encoding="utf-8")
    return p
