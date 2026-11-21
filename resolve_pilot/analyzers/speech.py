"""How loud the voice band is over time, for placing cuts between words.

Whisper's word timestamps are approximate, so cut points are chosen from the sound
itself. A fixed "silence" threshold does not work for that: measured on handheld vlog
footage, the gaps between words are only 8-12 dB below the words (wind, traffic and
music fill them), and ffmpeg's silencedetect found no pause at all. What does hold up
is the *shape*: band-limited to the voice (250-3400 Hz), a real gap between words is
a valley that is clearly wider (median 240 ms) than the dips inside continuous speech
(90 ms). So this module only measures the level curve; `editorial.wordcut` looks for
the valley between two given words, relative to how loud those words are.
"""
from __future__ import annotations

import array
import base64
import math
import operator
import shutil
import subprocess
import sys
from pathlib import Path

HOP_SECONDS = 0.01
_RATE = 8000                      # enough for the voice band
_HOP = int(_RATE * HOP_SECONDS)
_VOICE_BAND = "highpass=f=250,lowpass=f=3400"
_FLOOR_DB = -100.0                # digital silence; also the lowest level stored


def voice_levels(path: str | Path, timeout: float = 1800.0) -> list[float]:
    """Voice-band RMS level in dBFS for every 10 ms of the file's audio."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is not installed")
    p = Path(path).expanduser().resolve()
    result = subprocess.run(
        [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-vn", "-i", str(p),
         "-af", _VOICE_BAND, "-ac", "1", "-ar", str(_RATE), "-f", "s16le", "-"],
        capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0:
        tail = " | ".join(result.stderr.decode("utf-8", "replace").strip().splitlines()[-2:])
        raise RuntimeError(f"ffmpeg could not read the audio of {p.name}: {tail or 'no output'}")
    samples = array.array("h")
    samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if len(samples) < _HOP:
        raise RuntimeError(f"{p.name} has no audio to measure")
    scale = 1.0 / (_HOP * 32768.0 * 32768.0)
    levels = []
    for i in range(0, len(samples) - _HOP + 1, _HOP):
        block = samples[i:i + _HOP]
        energy = sum(map(operator.mul, block, block)) * scale
        levels.append(max(_FLOOR_DB, 10.0 * math.log10(energy)) if energy > 0 else _FLOOR_DB)
    return levels


def pack_levels(levels: list[float]) -> str:
    """Levels as base64, one byte per 10 ms (whole dB below 0 dBFS): ~0.5 MB per hour."""
    return base64.b64encode(bytes(min(255, max(0, round(-v))) for v in levels)).decode("ascii")


def unpack_levels(packed: str) -> list[float]:
    return [-float(b) for b in base64.b64decode(packed)]
