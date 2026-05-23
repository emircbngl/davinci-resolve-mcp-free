"""FCPXML 1.10 exporter — generates timelines that DaVinci Resolve (Free & Studio) imports cleanly.

Resolve accepts FCPXML via File > Import > Timeline. This is the primary handoff
mechanism for delivering Claude-built rough cuts into the Free edition, since the
Python scripting API is Studio-only.

Targets the FCPXML 1.10 schema (compatible with Resolve 18+).
"""
from __future__ import annotations

import html
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from xml.dom import minidom


@dataclass
class Clip:
    """A source-anchored clip in the rough cut.

    All times are in seconds; the exporter converts to FCPXML rational time.
    """
    source_path: str
    in_seconds: float
    duration_seconds: float
    name: str | None = None
    lane: int = 0
    notes: str | None = None


@dataclass
class TimelineSpec:
    name: str
    fps: float
    width: int = 1920
    height: int = 1080
    clips: list[Clip] = field(default_factory=list)


# NTSC fractional broadcast rates — FCPXML readers expect these specific
# rationals. `Fraction(fps).limit_denominator(1001)` can pick mathematically
# closer but non-standard fractions (e.g. 2997/125 for 23.976), which some
# tools reject.
_NTSC_RATIONALS: dict[float, tuple[int, int]] = {
    23.976: (24000, 1001),
    29.97:  (30000, 1001),
    47.952: (48000, 1001),
    59.94:  (60000, 1001),
    119.88: (120000, 1001),
}


def _fps_rational(fps: float) -> tuple[int, int]:
    """Return (num_per_sec, den) so that num_per_sec / den == fps."""
    if fps <= 0:
        raise ValueError("fps must be positive")
    if abs(fps - round(fps)) < 1e-6:
        return int(round(fps)), 1
    for canon, pair in _NTSC_RATIONALS.items():
        if abs(fps - canon) < 1e-3:
            return pair
    f = Fraction(fps).limit_denominator(1001)
    return f.numerator, f.denominator


def _rational(seconds: float, fps: float) -> str:
    """Convert seconds to FCPXML's <num>/<den>s rational time anchored to frame rate."""
    num_per_sec, den = _fps_rational(fps)
    frames = round(seconds * fps)
    return f"{frames * den}/{num_per_sec}s"


def _frame_duration(fps: float) -> str:
    """Per-frame duration for <format frameDuration='...'>."""
    num_per_sec, den = _fps_rational(fps)
    return f"{den}/{num_per_sec}s"


def build_fcpxml(spec: TimelineSpec) -> str:
    """Return a complete FCPXML 1.10 string ready to write to disk."""
    fmt_id = "r1"
    frame_dur = _frame_duration(spec.fps)
    total_duration = _rational(
        sum(c.duration_seconds for c in spec.clips), spec.fps
    )

    asset_lines: list[str] = []
    asset_ids: dict[str, str] = {}
    for i, clip in enumerate(spec.clips, start=2):
        src = Path(clip.source_path).expanduser().resolve()
        if str(src) in asset_ids:
            continue
        asset_id = f"r{i + 100}"
        asset_ids[str(src)] = asset_id
        # Resolve accepts file:// URIs with %20 escaping for spaces
        uri = "file://" + str(src).replace(" ", "%20")
        asset_lines.append(
            f'    <asset id="{asset_id}" name="{html.escape(src.stem)}" '
            f'src="{uri}" hasVideo="1" hasAudio="1" format="{fmt_id}"/>'
        )

    spine_clips: list[str] = []
    offset = 0.0
    for clip in spec.clips:
        asset_id = asset_ids[str(Path(clip.source_path).expanduser().resolve())]
        clip_name = clip.name or Path(clip.source_path).stem
        spine_clips.append(
            f'        <asset-clip name="{html.escape(clip_name)}" '
            f'ref="{asset_id}" '
            f'offset="{_rational(offset, spec.fps)}" '
            f'start="{_rational(clip.in_seconds, spec.fps)}" '
            f'duration="{_rational(clip.duration_seconds, spec.fps)}" '
            f'lane="{clip.lane}"/>'
        )
        offset += clip.duration_seconds

    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
  <resources>
    <format id="{fmt_id}" name="FFVideoFormat{spec.height}p{int(spec.fps)}"
            frameDuration="{frame_dur}" width="{spec.width}" height="{spec.height}"/>
{chr(10).join(asset_lines)}
  </resources>
  <library>
    <event name="{html.escape(spec.name)}">
      <project name="{html.escape(spec.name)}">
        <sequence format="{fmt_id}" duration="{total_duration}"
                  tcStart="0/1s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
{chr(10).join(spine_clips)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''
    # Pretty-print for readability
    return minidom.parseString(xml).toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")


def write_fcpxml(spec: TimelineSpec, out_path: str | Path) -> Path:
    """Convenience: build + write to disk. Returns final path."""
    p = Path(out_path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_fcpxml(spec), encoding="utf-8")
    return p
