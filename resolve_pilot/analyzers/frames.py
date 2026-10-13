"""Still frames and contact sheets from video with ffmpeg, so an agent can *look* at footage.

Binding details (each one fixed a real bug):
- ``-ss`` goes before ``-i``: input seeking is fast and frame-accurate on modern ffmpeg,
  output seeking decodes everything up to the timestamp.
- ``-frames:v 1`` on every command, also the contact sheet: without it the ``tile``
  filter keeps emitting (and image2 overwrites) or never writes a partial grid.
- Output files end in ``.jpg``: ffmpeg picks the encoder from the extension, and the
  MIME type sent to the model is derived from it too.
- A timestamp ffmpeg cannot serve (past the end, negative, not a number) is reported in
  ``dropped`` with a reason; it never fails the whole request.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

FRAME_TIMEOUT_SECONDS = 30.0


@dataclass
class Frame:
    index: int          # position of the timestamp in the request
    pts_seconds: float
    path: str

    def to_dict(self) -> dict:
        return asdict(self)


def _ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found on PATH. Install with: brew install ffmpeg")
    return path


def _frame_cmd(src: str | Path, t: float, out: str | Path, width: int) -> list[str]:
    return [
        _ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
        "-ss", f"{t:.3f}", "-i", str(src),
        "-an", "-sn", "-dn",
        "-vf", f"scale={int(width)}:-2", "-frames:v", "1", "-q:v", "3", "-y", str(out),
    ]


def _sheet_cmd(pattern: str | Path, out: str | Path, cols: int, rows: int) -> list[str]:
    return [
        _ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
        "-i", str(pattern), "-vf", f"tile={cols}x{rows}:padding=4:margin=4",
        "-frames:v", "1", "-q:v", "3", "-y", str(out),
    ]


def sample_points(cut_times: list[float], offset: float, duration: float) -> list[float]:
    """One timestamp inside each scene: ``offset`` after its start, at most its midpoint.

    ``cut_times`` are scene starts in ascending order (include 0.0 for the first scene).
    """
    points: list[float] = []
    for i, start in enumerate(cut_times):
        end = cut_times[i + 1] if i + 1 < len(cut_times) else float(duration)
        points.append(min(start + float(offset), start + max(0.0, end - start) / 2.0))
    return points


def pick_evenly(n: int, k: int) -> list[int]:
    """``k`` indices spread evenly over ``range(n)`` (all of them when ``k >= n``)."""
    if k >= n:
        return list(range(n))
    if k <= 1:
        return [0][:max(0, k)]
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def grid_shape(n: int, max_ratio: float = 3.0) -> tuple[int, int]:
    """(cols, rows) for ``n`` cells: an exact fit when it is not too elongated."""
    if n <= 0:
        raise ValueError("a contact sheet needs at least one frame")
    for rows in range(math.isqrt(n), 0, -1):
        if n % rows == 0 and (n // rows) / rows <= max_ratio:
            return n // rows, rows
    cols = math.ceil(math.sqrt(n))
    return cols, math.ceil(n / cols)


def extract_frames(
    path: str | Path,
    at_seconds: list[float],
    width: int = 480,
    max_frames: int = 12,
    out_dir: str | Path | None = None,
) -> tuple[list[Frame], list[dict[str, Any]], Path]:
    """Grab one JPEG per timestamp into ``out_dir`` (a new temp folder by default).

    Returns ``(frames, dropped, out_dir)``. Successful frames are numbered
    ``frame_000.jpg``... in order, ready for :func:`contact_sheet`; each keeps the
    ``index`` of its timestamp in the request so callers pair results by position,
    never by float equality. ``dropped`` holds ``{index, pts_seconds, reason}``.
    """
    import tempfile

    ffmpeg = _ffmpeg_bin()
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    folder = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="resolve-pilot-frames-"))
    folder.mkdir(parents=True, exist_ok=True)

    from resolve_pilot.analyzers.media import probe_media_cached
    info = probe_media_cached(source)
    duration = info.duration_seconds if info and info.duration_seconds else None

    frames: list[Frame] = []
    dropped: list[dict[str, Any]] = []
    cap = max(0, int(max_frames))
    for index, raw in enumerate(at_seconds):
        try:
            t = float(raw)
        except (TypeError, ValueError):
            dropped.append({"index": index, "pts_seconds": raw, "reason": "not a number"})
            continue
        if index >= cap:
            dropped.append({"index": index, "pts_seconds": t, "reason": f"over max_frames ({cap})"})
            continue
        if not math.isfinite(t) or t < 0:
            # str() keeps NaN / inf out of the JSON reply, where they are not valid.
            dropped.append({"index": index, "pts_seconds": t if math.isfinite(t) else str(t),
                            "reason": "not a valid time"})
            continue
        candidate = folder / f"candidate_{index:03d}.jpg"
        cmd = _frame_cmd(source, t, candidate, width)
        cmd[0] = ffmpeg
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=FRAME_TIMEOUT_SECONDS, check=False)
            err = (proc.stderr or "").strip().splitlines()[-1:]
        except subprocess.TimeoutExpired:
            err = [f"ffmpeg timed out after {FRAME_TIMEOUT_SECONDS:g}s"]
        if candidate.exists() and candidate.stat().st_size:
            out = folder / f"frame_{len(frames):03d}.jpg"
            candidate.replace(out)
            frames.append(Frame(index=index, pts_seconds=t, path=str(out)))
            continue
        candidate.unlink(missing_ok=True)
        if duration is not None and t >= duration:
            reason = f"past the end of the media ({duration:.3f}s)"
        else:
            reason = "ffmpeg produced no frame" + (f": {err[0]}" if err else "")
        dropped.append({"index": index, "pts_seconds": t, "reason": reason})
    return frames, dropped, folder


def contact_sheet(frames: list[Frame], out_path: str | Path) -> tuple[int, int]:
    """Tile frames from :func:`extract_frames` into one JPEG; returns (cols, rows).

    Cells read left to right, top to bottom, in the order of ``frames``.
    """
    if not frames:
        raise ValueError("a contact sheet needs at least one frame")
    folder = Path(frames[0].path).parent
    expected = [folder / f"frame_{i:03d}.jpg" for i in range(len(frames))]
    if [Path(f.path) for f in frames] != expected:
        raise ValueError("contact_sheet needs the frames exactly as extract_frames returned them")
    cols, rows = grid_shape(len(frames))
    out = Path(out_path)
    out.unlink(missing_ok=True)
    proc = subprocess.run(_sheet_cmd(folder / "frame_%03d.jpg", out, cols, rows),
                          capture_output=True, text=True, timeout=60.0, check=False)
    if proc.returncode != 0 or not out.exists() or not out.stat().st_size:
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"ffmpeg could not build the contact sheet: {tail or 'no output'}")
    return cols, rows
