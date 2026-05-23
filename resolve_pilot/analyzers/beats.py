"""Beat / onset detection for music-video cutting.

Uses ffmpeg's `aubiotrack`-free path: we extract a mono PCM stream and run a
lightweight energy-onset detector that doesn't need extra dependencies beyond
numpy. For higher precision, plug in librosa or aubio if installed.
"""
from __future__ import annotations

import shutil
import subprocess
import wave
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
        raise RuntimeError("ffmpeg not found")
    return p


def _decode_mono(path: str | Path, sr: int = 22050) -> tuple[list[float], int]:
    """Decode a file to a mono float32 list at the given sample rate."""
    import struct
    cmd = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-ac", "1", "-ar", str(sr), "-f", "wav", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    # The output is a WAV stream; read with wave from BytesIO.
    import io
    with wave.open(io.BytesIO(proc.stdout)) as w:
        n = w.getnframes()
        sw = w.getsampwidth()
        raw = w.readframes(n)
        if sw == 2:
            samples = list(struct.unpack(f"<{n}h", raw))
            scale = 1.0 / 32768.0
            return [s * scale for s in samples], sr
        else:
            raise RuntimeError(f"Unexpected sample width: {sw}")


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
    if not samples:
        return []

    # Frame the signal, compute RMS per frame, then locate peaks.
    win = int(sr * 0.025)            # 25 ms windows
    hop = int(sr * 0.010)            # 10 ms hop
    rms: list[float] = []
    times: list[float] = []
    import math
    for start in range(0, max(0, len(samples) - win), hop):
        frame = samples[start:start + win]
        m = sum(x * x for x in frame) / win
        rms.append(math.sqrt(m) if m > 0 else 1e-12)
        times.append(start / sr)

    if not rms:
        return []

    # Onset envelope: half-wave rectified diff
    env = [0.0]
    for i in range(1, len(rms)):
        d = max(0.0, rms[i] - rms[i - 1])
        env.append(d)

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
    return beats
