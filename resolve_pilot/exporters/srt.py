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


def _wrap_words(words: list[str], max_chars: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines


def wrap_cues(cues: list[SrtCue], max_chars_per_line: int = 42, max_lines: int = 2) -> list[SrtCue]:
    """Wrap cue text to at most `max_lines` lines of `max_chars_per_line` characters.

    Text that does not fit is split into consecutive cues; each gets a share of
    the original time span proportional to its length, so reading speed stays
    even. Words are never broken. `max_chars_per_line <= 0` disables wrapping.
    """
    if max_chars_per_line <= 0:
        return list(cues)
    max_lines = max(1, max_lines)
    out: list[SrtCue] = []
    for cue in cues:
        lines = _wrap_words(cue.text.split(), max_chars_per_line)
        if not lines:
            continue
        chunks = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        if len(chunks) == 1:
            out.append(SrtCue(cue.start, cue.end, "\n".join(lines)))
            continue
        weights = [sum(len(line) for line in chunk) for chunk in chunks]
        span = max(0.0, cue.end - cue.start)
        t = cue.start
        for k, (chunk, w) in enumerate(zip(chunks, weights)):
            end = cue.end if k == len(chunks) - 1 else t + span * w / sum(weights)
            out.append(SrtCue(t, end, "\n".join(chunk)))
            t = end
    return out
