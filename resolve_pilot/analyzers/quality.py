"""Cheap ffmpeg-based signals for finding questionable and notable footage.

Spans: ``silence`` (below -50 dB for 0.5 s+), ``black`` (0.5 s+), ``freeze`` (1 s+),
and ``loud`` / ``quiet`` audio relative to the *local* level (a rolling median over
``window_seconds``), because absolute loudness differs radically between locations.
All times are media-relative seconds.
"""
from __future__ import annotations

import math
import re
import shutil
import statistics
import subprocess
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from resolve_pilot.analyzers.media import probe_media


@dataclass
class Span:
    """A detected silence, black, freeze, quiet, or loud interval."""

    start: float
    end: float
    kind: str
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


KINDS = ("silence", "black", "freeze", "loud", "quiet")

_INTERVALS = {
    "silence": re.compile(r"silence_(start|end):\s*(-?[0-9.]+)"),
    "black": re.compile(r"black_start:\s*(-?[0-9.]+)\s+black_end:\s*(-?[0-9.]+)"),
    "freeze": re.compile(r"freeze_(start|end):\s*(-?[0-9.]+)"),
}
_PTS = re.compile(r"pts_time:(-?[0-9.]+)")
# Loudness is measured per block of this many samples (~0.1 s at 48 kHz) instead of
# per decoded frame, which keeps the log of an hour-long file small.
_RMS_BLOCK_SAMPLES = 4800
# A "loud" or "quiet" block must differ from the local median by at least this much;
# ~6 dB is a clearly audible change (double the amplitude).
LOUDNESS_MIN_DELTA_DB = 6.0
_RMS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=([^\s]+)")


def _run(command: list[str], timeout: float = 120.0) -> str:
    result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                            timeout=timeout, check=False)
    if result.returncode != 0:
        tail = " | ".join((result.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"ffmpeg quality analysis failed: {tail or 'no output'}")
    return result.stderr + "\n" + result.stdout


def _parse_intervals(text: str, kind: str, duration: float) -> list[Span]:
    pattern = _INTERVALS[kind]
    if kind == "black":
        return [Span(max(0.0, float(a)), float(b), kind, 1.0) for a, b in pattern.findall(text)]
    spans: list[Span] = []
    start: float | None = None
    for edge, value in pattern.findall(text):
        if edge == "start":
            start = max(0.0, float(value))
        elif start is not None:
            spans.append(Span(start, float(value), kind, 1.0))
            start = None
    if start is not None:
        spans.append(Span(start, duration, kind, 1.0))
    return spans


def _metadata_values(text: str, value_pattern: re.Pattern[str]) -> list[tuple[float, float]]:
    values: list[tuple[float, float]] = []
    pts: float | None = None
    for line in text.splitlines():
        match = _PTS.search(line)
        if match:
            pts = float(match.group(1))
        match = value_pattern.search(line)
        if match and pts is not None:
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if math.isfinite(pts) and math.isfinite(value):
                values.append((pts, value))
    return values


def _relative_spans(
    values: list[tuple[float, float]], kind_low: str, kind_high: str,
    window_seconds: float, duration: float, min_delta: float = 0.0,
) -> list[Span]:
    """Spans where a value departs from its rolling local median.

    ``min_delta`` is an absolute floor in the values' own units: on a very steady signal
    the MAD is tiny, so without it a fraction of a dB would read as "loud".
    """
    finite_values = [(timestamp, value) for timestamp, value in values
                     if math.isfinite(timestamp) and math.isfinite(value)]
    spans: list[Span] = []
    radius = window_seconds / 2.0
    local: deque[tuple[float, float]] = deque()
    right = 0
    for index, (timestamp, value) in enumerate(finite_values):
        while right < len(finite_values) and finite_values[right][0] <= timestamp + radius:
            local.append(finite_values[right])
            right += 1
        while local and local[0][0] < timestamp - radius:
            local.popleft()
        local_values = [sample for _, sample in local]
        median = statistics.median(local_values)
        mad = statistics.median(abs(sample - median) for sample in local_values)
        # One percent of the local signal magnitude is an absolute floor in the
        # detector's own units, preventing tiny numerical jitter from dominating.
        spread = max(mad, max(1e-6, statistics.median(map(abs, local_values)) * 0.01))
        delta = (value - median) / spread
        if not math.isfinite(delta) or abs(delta) < 2.0 or abs(value - median) < min_delta:
            continue
        end = finite_values[index + 1][0] if index + 1 < len(finite_values) else duration
        spans.append(Span(timestamp, max(timestamp, end), kind_high if delta > 0 else kind_low,
                          min(1.0, abs(delta) / 6.0)))
    return spans


def _merge_adjacent_spans(spans: list[Span], gap_seconds: float) -> list[Span]:
    """Merge same-kind neighbors, keeping the strongest score in each run.

    Runs are tracked per kind, so a span of another kind in between (black next to a
    loud burst, say) does not split a run in two.
    """
    merged: list[Span] = []
    last: dict[str, Span] = {}
    for span in sorted(spans, key=lambda item: (item.start, item.end, item.kind)):
        prev = last.get(span.kind)
        if prev is not None and span.start - prev.end < gap_seconds:
            prev.end = max(prev.end, span.end)
            prev.score = max(prev.score, span.score)
        else:
            last[span.kind] = Span(span.start, span.end, span.kind, span.score)
            merged.append(last[span.kind])
    return merged


# A weaker finding inside a stronger one says nothing new: black frames are trivially
# frozen, and digital silence is trivially quiet.
_IMPLIED_BY = {"freeze": "black", "quiet": "silence"}


def _drop_implied_spans(spans: list[Span]) -> list[Span]:
    """Drop freeze spans inside black and quiet spans inside silence."""
    def covered(s: Span) -> bool:
        stronger = [o for o in spans if o.kind == _IMPLIED_BY[s.kind]]
        length = s.end - s.start
        overlap = sum(max(0.0, min(s.end, o.end) - max(s.start, o.start)) for o in stronger)
        return length <= 0 or overlap >= 0.9 * length

    return [s for s in spans if s.kind not in _IMPLIED_BY or not covered(s)]


def _available_filters(ffmpeg: str) -> set[str]:
    output = _run([ffmpeg, "-hide_banner", "-filters"], timeout=30.0)
    return {match.group(1) for match in re.finditer(r"^\s*[.A-Z]{2,3}\s+(\w+)", output, re.MULTILINE)}


def analyze_quality(
    path: str | Path, *, window_seconds: float = 30.0, merge_gap_seconds: float = 0.5,
) -> tuple[list[Span], dict]:
    """Return detected spans and detector metadata for a media file."""
    if not math.isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if not math.isfinite(merge_gap_seconds) or merge_gap_seconds < 0:
        raise ValueError("merge_gap_seconds must be non-negative")
    source = Path(path).expanduser().resolve()
    info = probe_media(source)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    available = _available_filters(ffmpeg)
    requested = ["silencedetect", "astats", "blackdetect", "freezedetect"]
    ran: list[str] = []
    skipped = [name for name in requested if name not in available]
    spans: list[Span] = []
    timeout = 120.0 + info.duration_seconds * 0.5
    has_audio = info.audio_codec is not None
    has_video = info.video_codec is not None

    if "silencedetect" in available and has_audio:
        text = _run([ffmpeg, "-hide_banner", "-i", str(source), "-vn", "-af",
                     "silencedetect=n=-50dB:d=0.5", "-f", "null", "-"], timeout=timeout)
        spans.extend(_parse_intervals(text, "silence", info.duration_seconds))
        ran.append("silencedetect")
    elif "silencedetect" in available:
        skipped.append("silencedetect")

    video_detectors = [name for name in ("blackdetect", "freezedetect") if name in available]
    if video_detectors and has_video:
        filters = ["scale=-2:360"]
        filters.extend("blackdetect=d=0.5" if name == "blackdetect" else "freezedetect=d=1"
                       for name in video_detectors)
        text = _run([ffmpeg, "-hide_banner", "-i", str(source), "-vf", ",".join(filters),
                     "-an", "-f", "null", "-"], timeout=timeout)
        for detector, kind in (("blackdetect", "black"), ("freezedetect", "freeze")):
            if detector in video_detectors:
                spans.extend(_parse_intervals(text, kind, info.duration_seconds))
                ran.append(detector)
    elif video_detectors:
        skipped.extend(video_detectors)

    if "astats" in available and has_audio:
        text = _run([ffmpeg, "-hide_banner", "-i", str(source), "-vn", "-af",
                     f"asetnsamples=n={_RMS_BLOCK_SAMPLES},astats=metadata=1:reset=1,"
                     "ametadata=print:key=lavfi.astats.Overall.RMS_level", "-f", "null", "-"],
                    timeout=timeout)
        # A local baseline is essential: absolute loudness differs radically by location.
        spans.extend(_relative_spans(_metadata_values(text, _RMS), "quiet", "loud",
                                     window_seconds, info.duration_seconds,
                                     min_delta=LOUDNESS_MIN_DELTA_DB))
        ran.append("astats")
    elif "astats" in available:
        skipped.append("astats")
    # Half a second bridges adjacent metadata windows without joining distinct events.
    spans = _drop_implied_spans(_merge_adjacent_spans(spans, merge_gap_seconds))
    return spans, {"duration": info.duration_seconds, "fps": info.fps,
                   "detectors_ran": ran, "detectors_skipped": skipped,
                   "window_seconds": window_seconds, "merge_gap_seconds": merge_gap_seconds}
