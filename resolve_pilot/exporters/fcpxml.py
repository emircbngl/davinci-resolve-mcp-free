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
    `transition_out_seconds` adds a Cross Dissolve into the *next* clip; ignored
    on the last clip. Overrides `TimelineSpec.default_transition_seconds`.
    """
    source_path: str
    in_seconds: float
    duration_seconds: float
    name: str | None = None
    lane: int = 0
    notes: str | None = None
    transition_out_seconds: float = 0.0


@dataclass
class TimelineSpec:
    name: str
    fps: float
    width: int = 1920
    height: int = 1080
    clips: list[Clip] = field(default_factory=list)
    # If > 0, emit a Cross Dissolve of this duration at every cut that does not
    # have a per-clip override via `Clip.transition_out_seconds`.
    default_transition_seconds: float = 0.0
    # Fade up from black at the start / down to black at the end. Implemented as
    # a black <gap> + Cross Dissolve at the boundary.
    fade_in_seconds: float = 0.0
    fade_out_seconds: float = 0.0


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


def _transition_duration_for(clip: Clip, default: float) -> float:
    """Per-clip transition override > timeline default. 0 means none."""
    return clip.transition_out_seconds if clip.transition_out_seconds > 0 else max(0.0, default)


def _gap_element(offset_sec: float, duration_sec: float, fps: float) -> str:
    return (
        f'<gap name="Gap" '
        f'offset="{_rational(offset_sec, fps)}" '
        f'duration="{_rational(duration_sec, fps)}" '
        f'start="0/1s"/>'
    )


def _transition_element(offset_sec: float, duration_sec: float, fps: float) -> str:
    return (
        f'<transition name="Cross Dissolve" '
        f'offset="{_rational(offset_sec, fps)}" '
        f'duration="{_rational(duration_sec, fps)}"/>'
    )


def build_fcpxml(spec: TimelineSpec) -> str:
    """Return a complete FCPXML 1.10 string ready to write to disk.

    Transition / fade model: Cross Dissolves are emitted as FCP-X-style centered
    overlaps — the transition's duration is split half-into-A, half-into-B, so
    spine total length is unaffected by internal transitions. Fades prepend /
    append a black `<gap>` whose duration *does* add to the spine.
    """
    fmt_id = "r1"
    frame_dur = _frame_duration(spec.fps)

    clips_duration = sum(c.duration_seconds for c in spec.clips)
    total_duration_sec = spec.fade_in_seconds + clips_duration + spec.fade_out_seconds
    total_duration = _rational(total_duration_sec, spec.fps)

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

    spine_elements: list[str] = []
    offset = 0.0

    if spec.fade_in_seconds > 0:
        spine_elements.append(_gap_element(0.0, spec.fade_in_seconds, spec.fps))
        # Centered transition at the gap → clip 1 boundary
        spine_elements.append(_transition_element(
            spec.fade_in_seconds - spec.fade_in_seconds / 2,
            spec.fade_in_seconds, spec.fps,
        ))
        offset = spec.fade_in_seconds

    last_index = len(spec.clips) - 1
    for i, clip in enumerate(spec.clips):
        asset_id = asset_ids[str(Path(clip.source_path).expanduser().resolve())]
        clip_name = clip.name or Path(clip.source_path).stem
        spine_elements.append(
            f'<asset-clip name="{html.escape(clip_name)}" '
            f'ref="{asset_id}" '
            f'offset="{_rational(offset, spec.fps)}" '
            f'start="{_rational(clip.in_seconds, spec.fps)}" '
            f'duration="{_rational(clip.duration_seconds, spec.fps)}" '
            f'lane="{clip.lane}"/>'
        )
        if i < last_index:
            trans = _transition_duration_for(clip, spec.default_transition_seconds)
            if trans > 0:
                # Centered overlap at the clip-to-clip boundary
                spine_elements.append(_transition_element(
                    offset + clip.duration_seconds - trans / 2, trans, spec.fps,
                ))
        offset += clip.duration_seconds

    if spec.fade_out_seconds > 0:
        spine_elements.append(_transition_element(
            offset - spec.fade_out_seconds / 2,
            spec.fade_out_seconds, spec.fps,
        ))
        spine_elements.append(_gap_element(offset, spec.fade_out_seconds, spec.fps))

    spine_body = "\n".join("        " + e for e in spine_elements)
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
{spine_body}
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
