"""Scene cut detection using ffmpeg's `scdet` / `select=gt(scene,...)` filter."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class SceneCut:
    pts_seconds: float
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


def _ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found on PATH. Install with: brew install ffmpeg")
    return path


# ffmpeg's scdet prints metadata as `lavfi.scd.time: X` (newer builds, ':' separator)
# or `lavfi.scd.time=X` (older builds, '=' separator). The two keys can also appear
# in either order on the same line. Scan per line and pair them up.
_TIME_RE = re.compile(r"lavfi\.scd\.time[=:\s]+([0-9.]+)")
_SCORE_RE = re.compile(r"lavfi\.scd\.score[=:\s]+([0-9.]+)")


def detect_scenes(path: str | Path, threshold: float = 10.0) -> list[SceneCut]:
    """Run ffmpeg scdet filter and return list of scene cut timestamps.

    threshold: 0-100, lower is more sensitive. 10 is a reasonable default for cuts;
    raise to 30+ if you want only hard cuts and ignore lighting changes.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)

    cmd = [
        _ffmpeg_bin(),
        "-hide_banner",
        "-i", str(p),
        "-an", "-sn", "-dn",  # only the picture matters; skip decoding the rest
        "-vf", f"scdet=threshold={threshold}",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Without this an unreadable file looked like "no scene cuts".
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"ffmpeg scene detection failed for {p.name}: {tail}")

    cuts: list[SceneCut] = []
    for line in proc.stderr.splitlines():
        t_m = _TIME_RE.search(line)
        if not t_m:
            continue
        try:
            pts = float(t_m.group(1))
        except ValueError:
            continue
        s_m = _SCORE_RE.search(line)
        score = 0.0
        if s_m:
            try:
                score = float(s_m.group(1))
            except ValueError:
                pass
        cuts.append(SceneCut(pts_seconds=pts, score=score))
    return cuts
