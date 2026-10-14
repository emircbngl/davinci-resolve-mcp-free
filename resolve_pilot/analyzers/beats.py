"""Beat / onset detection for music-video cutting.

Uses ffmpeg's `aubiotrack`-free path: we extract a mono PCM stream and run a
lightweight energy-onset detector that doesn't need extra dependencies beyond
numpy. For higher precision, plug in librosa or aubio if installed.
"""
from __future__ import annotations

import math
import operator
import shutil
import subprocess
import sys
from array import array
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class Beat:
    pts_seconds: float
    strength: float  # 0..1, normalized

    def to_dict(self) -> dict:
        return asdict(self)


def _ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg not found on PATH. Install with: brew install ffmpeg")
    return p


def _decode_mono(path: str | Path, sr: int = 22050) -> tuple[array, int]:
    """Decode a file to mono signed 16-bit samples at the given sample rate.

    Raw PCM rather than WAV: piped WAV output has no valid size header (ffmpeg
    cannot seek back to fill it in), which broke the `wave` module reader.
    """
    cmd = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error",
        "-i", str(path), "-vn",
        "-ac", "1", "-ar", str(sr), "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    samples = array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples, sr


def detect_beats(
    path: str | Path,
    min_seconds_between: float = 0.20,
    energy_threshold_db: float = -25.0,
) -> list[Beat]:
    """Energy-based onset detector. Good enough for typical music videos.

    For BPM-locked, sample-accurate detection, install librosa and call
    `librosa.beat.beat_track` — we expose this lightweight path as the default
    so the package stays small.
    """
    samples, sr = _decode_mono(path)
    hop = int(sr * 0.010)            # 10 ms hop
    if len(samples) < 3 * hop:
        return []

    # Energy per 10 ms block, then RMS over the 30 ms window ending at each block,
    # stamped with that newest block: that is where the energy rise (the onset)
    # is, not 20 ms earlier at the window start. Only full windows are used, so
    # sound that is already playing at t=0 does not count as a rise from silence
    # (that would also dwarf every other onset's strength); a hit right at t=0 is
    # handled separately below.
    block = [sum(map(operator.mul, seg, seg))
             for seg in (samples[i:i + hop] for i in range(0, len(samples) - hop + 1, hop))]
    scale = 1.0 / (3 * hop * 32768.0 * 32768.0)
    rms = [math.sqrt(sum(block[j - 2:j + 1]) * scale) or 1e-12 for j in range(2, len(block))]
    times = [j * hop / sr for j in range(2, len(block))]
    if not rms:
        return []

    # Onset envelope: half-wave rectified diff
    env = [0.0] + [max(0.0, rms[i] - rms[i - 1]) for i in range(1, len(rms))]

    peak_max = max(env) or 1e-12
    threshold_lin = 10 ** (energy_threshold_db / 20.0) * peak_max
    min_gap_samples = max(1, int(min_seconds_between / 0.010))

    beats: list[Beat] = []
    last_idx = -min_gap_samples
    for i, v in enumerate(env):
        if v < threshold_lin:
            continue
        # Local maximum: stronger than 2 neighbors each side
        lo, hi = max(0, i - 2), min(len(env), i + 3)
        if v < max(env[lo:hi]):
            continue
        if i - last_idx < min_gap_samples:
            continue
        last_idx = i
        beats.append(Beat(pts_seconds=times[i], strength=v / peak_max))

    # A hit at t=0 (a song cut to start on its downbeat) has no rise inside the file.
    # Count it when the opening is clearly louder than 80-200 ms later, i.e. it
    # decays like a hit rather than continuing like music already playing.
    if len(rms) > 20 and (not beats or beats[0].pts_seconds >= min_seconds_between):
        later = sum(rms[8:19]) / 11
        rise = rms[0] - later
        if rms[0] > 2 * later and rise >= threshold_lin:
            beats.insert(0, Beat(pts_seconds=0.0, strength=min(1.0, rise / peak_max)))
    return beats


def estimate_tempo(beats: list[Beat], min_strength: float = 0.3) -> float | None:
    """Rough BPM from the median gap between strong onsets, folded into 70-180."""
    times = [b.pts_seconds for b in beats if b.strength >= min_strength]
    gaps = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    if len(gaps) < 3:
        return None
    bpm = 60.0 / gaps[len(gaps) // 2]
    while bpm < 70:
        bpm *= 2
    while bpm > 180:
        bpm /= 2
    return round(bpm, 1)
