"""Real pauses in a recording's audio, for placing cuts next to words.

Whisper's word timestamps are approximate; where the sound actually stops and starts
is measured here with ffmpeg's silencedetect. "Silence" is relative to the file: the
threshold sits a little above the recording's own noise floor, because room tone in a
vlog is far louder than the digital silence of a screen recording.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path

MIN_PAUSE_SECONDS = 0.15   # shorter gaps are stop consonants inside words, not pauses
_BLIP_SECONDS = 0.06       # sound this short between two pauses is a click, not speech
_ABOVE_FLOOR_DB = 6.0      # threshold = noise floor + this
_FALLBACK_DB = -35.0       # when the noise floor cannot be measured
_LOWEST_DB, _BELOW_SPEECH_DB = -60.0, 10.0

_STAT = re.compile(r"(Noise floor dB|RMS level dB):\s*(-?inf|-?[0-9.]+)")
_EDGE = re.compile(r"silence_(start|end):\s*(-?[0-9.]+)")


def _ffmpeg(path: Path, audio_filter: str, timeout: float) -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is not installed")
    result = subprocess.run(
        [exe, "-hide_banner", "-nostdin", "-vn", "-i", str(path), "-af", audio_filter,
         "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
    if result.returncode != 0:
        tail = " | ".join((result.stderr or "").strip().splitlines()[-2:])
        raise RuntimeError(f"ffmpeg could not read the audio of {path.name}: {tail or 'no output'}")
    return result.stderr


def pause_threshold_db(stats_log: str) -> float:
    """Silence threshold from an astats log: just above the noise floor, well below speech."""
    stats: dict[str, float] = {}
    for name, value in _STAT.findall(stats_log):
        stats[name] = -math.inf if value.endswith("inf") else float(value)
    floor = stats.get("Noise floor dB")
    if floor is None:
        return _FALLBACK_DB
    threshold = max(_LOWEST_DB, floor + _ABOVE_FLOOR_DB)
    level = stats.get("RMS level dB")
    if level is not None and math.isfinite(level):
        threshold = min(threshold, level - _BELOW_SPEECH_DB)
    return round(max(_LOWEST_DB, threshold), 1)


def parse_pauses(log: str, duration: float | None = None) -> list[tuple[float, float]]:
    pauses: list[tuple[float, float]] = []
    start: float | None = None
    for edge, value in _EDGE.findall(log):
        if edge == "start":
            start = max(0.0, float(value))
        elif start is not None:
            pauses.append((round(start, 3), round(float(value), 3)))
            start = None
    if start is not None and duration is not None and duration > start:
        pauses.append((round(start, 3), round(duration, 3)))  # silent to the end of the file
    # A click or a burst of room tone splits one pause in two (measured with pink noise:
    # 3.0-3.188 and 3.188-3.4). Sound that short between two pauses is not a word.
    merged: list[tuple[float, float]] = []
    for a, b in pauses:
        if merged and a - merged[-1][1] <= _BLIP_SECONDS:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def detect_pauses(path: str | Path, duration: float | None = None,
                  timeout: float = 900.0) -> tuple[list[tuple[float, float]], float]:
    """(pauses as (start, end) seconds, the threshold in dB that was used)."""
    p = Path(path).expanduser().resolve()
    threshold = pause_threshold_db(_ffmpeg(p, "astats=measure_perchannel=none", timeout))
    log = _ffmpeg(p, f"silencedetect=noise={threshold}dB:d={MIN_PAUSE_SECONDS}", timeout)
    return parse_pauses(log, duration), threshold
