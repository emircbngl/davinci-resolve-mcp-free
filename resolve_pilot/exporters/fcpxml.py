"""FCPXML 1.10 exporter — generates timelines that DaVinci Resolve (Free & Studio) imports cleanly.

Resolve accepts FCPXML via File > Import > Timeline. This is the primary handoff
mechanism for delivering Claude-built rough cuts into the Free edition, since the
Python scripting API is Studio-only.

Targets the FCPXML 1.10 schema (compatible with Resolve 18+).
"""
from __future__ import annotations

import html
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
    transition_out_seconds: float | None = None  # None = timeline default; 0 = hard cut


@dataclass
class AudioBed:
    """Audio laid under the cut as a connected clip (lane -1 → its own audio track).

    `offset_seconds` is the timeline position it starts at; `duration_seconds`
    of None runs it to the end of the timeline.
    """
    source_path: str
    in_seconds: float = 0.0
    duration_seconds: float | None = None
    offset_seconds: float = 0.0
    name: str | None = None


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
    audio_bed: AudioBed | None = None


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


def _frames(seconds: float, fps: float) -> int:
    return round(seconds * fps)


def _rt(frames: int, fps: float) -> str:
    """Frame count → FCPXML rational time string."""
    num_per_sec, den = _fps_rational(fps)
    return f"{frames * den}/{num_per_sec}s"


def _rational(seconds: float, fps: float) -> str:
    """Convert seconds to FCPXML's <num>/<den>s rational time anchored to frame rate."""
    return _rt(_frames(seconds, fps), fps)


def _frame_duration(fps: float) -> str:
    """Per-frame duration for <format frameDuration='...'>."""
    num_per_sec, den = _fps_rational(fps)
    return f"{den}/{num_per_sec}s"


def _transition_duration_for(clip: Clip, default: float) -> float:
    """Per-clip transition override > timeline default. None = use the default; 0 = hard cut."""
    if clip.transition_out_seconds is None:
        return max(0.0, default)
    return clip.transition_out_seconds


def _media_flags(src: Path) -> tuple[bool, bool, float | None]:
    """(has_video, has_audio, duration) via ffprobe; assumes A/V when it can't tell."""
    from resolve_pilot.analyzers.media import probe_media_cached

    info = probe_media_cached(src)
    if info is None:
        return True, True, None
    has_video = info.video_codec is not None
    has_audio = info.audio_codec is not None
    if not has_video and not has_audio:
        return True, True, None
    return has_video, has_audio, info.duration_seconds or None


@dataclass
class Layout:
    """Frame positions of a timeline exactly as the exporter writes them.

    ``bounds[i]`` is where clip i starts (``bounds[-1]`` where the last ends);
    ``edges[i]`` is the dissolve length in frames at the edge before clip i
    (``edges[0]`` the fade-in, ``edges[-1]`` the fade-out). validate_plan uses
    the same numbers, so what it accepts is what the FCPXML contains.
    """
    bounds: list[int]
    edges: list[int]
    total_frames: int
    bed_offset: int = 0
    bed_frames: int = 0

    @property
    def clip_frames(self) -> list[int]:
        return [b - a for a, b in zip(self.bounds, self.bounds[1:])]

    @property
    def bed_fits(self) -> bool:
        return self.bed_frames > 0 and self.bed_offset < self.total_frames


def layout(spec: TimelineSpec) -> Layout:
    fps = spec.fps
    # Clip boundaries are rounded to frames cumulatively, so every clip starts
    # exactly where the previous one ends — rounding each duration on its own
    # left one-frame gaps/overlaps and drift across many short (beat) cuts.
    bounds = [_frames(spec.fade_in_seconds, fps)]
    t = spec.fade_in_seconds
    for c in spec.clips:
        t += c.duration_seconds
        bounds.append(_frames(t, fps))
    edges = [bounds[0]]
    edges += [_frames(_transition_duration_for(c, spec.default_transition_seconds), fps)
              for c in spec.clips[:-1]]
    edges.append(_frames(spec.fade_out_seconds, fps))
    total = bounds[-1] + edges[-1]
    out = Layout(bounds=bounds, edges=edges, total_frames=total)
    bed = spec.audio_bed
    if bed:
        out.bed_offset = _frames(bed.offset_seconds, fps)
        out.bed_frames = (_frames(bed.duration_seconds, fps) if bed.duration_seconds
                          else max(0, total - out.bed_offset))
    return out


def build_fcpxml(spec: TimelineSpec) -> str:
    """Return a complete FCPXML 1.10 string ready to write to disk.

    Transition / fade model: Cross Dissolves are emitted as FCP-X-style centered
    overlaps — the transition's duration is split half-into-A, half-into-B, so
    spine total length is unaffected by internal transitions. Fades prepend /
    append a black `<gap>` whose duration *does* add to the spine.

    An audio bed becomes a connected clip on lane -1 of the first spine element,
    which Resolve imports as its own audio track under the picture.
    """
    fmt_id = "r1"
    fps = spec.fps
    frame_dur = _frame_duration(fps)

    lay = layout(spec)
    bounds = lay.bounds
    fade_in_frames = bounds[0]
    fade_out_frames = lay.edges[-1]
    total_frames = lay.total_frames

    asset_lines: list[str] = []
    asset_ids: dict[str, str] = {}

    def asset_for(path: str) -> str:
        src = Path(path).expanduser().resolve()
        key = str(src)
        if key in asset_ids:
            return asset_ids[key]
        asset_id = f"r{len(asset_ids) + 102}"
        asset_ids[key] = asset_id
        has_video, has_audio, duration = _media_flags(src)
        attrs = [
            f'id="{asset_id}"',
            f'name="{html.escape(src.stem)}"',
            f'src="{html.escape(src.as_uri())}"',
            'start="0/1s"',
        ]
        if duration:
            attrs.append(f'duration="{_rational(duration, fps)}"')
        attrs.append(f'hasVideo="{int(has_video)}"')
        attrs.append(f'hasAudio="{int(has_audio)}"')
        if has_video:
            attrs.append(f'format="{fmt_id}"')
        asset_lines.append(f"    <asset {' '.join(attrs)}/>")
        return asset_id

    for clip in spec.clips:
        asset_for(clip.source_path)

    bed_xml = ""
    bed = spec.audio_bed
    bed_offset, bed_frames = lay.bed_offset, lay.bed_frames
    if bed and lay.bed_fits:  # a bed that would import as nothing; validate_plan flags it
        bed_ref = asset_for(bed.source_path)
        # A connected clip's offset is measured in its parent's source time.
        if fade_in_frames > 0 or not spec.clips:
            parent_start_frames = 0
        else:
            parent_start_frames = _frames(spec.clips[0].in_seconds, fps)
        bed_name = bed.name or Path(bed.source_path).stem
        bed_xml = (
            f'<asset-clip name="{html.escape(bed_name)}" ref="{bed_ref}" lane="-1" '
            f'offset="{_rt(parent_start_frames + bed_offset, fps)}" '
            f'start="{_rational(bed.in_seconds, fps)}" '
            f'duration="{_rt(bed_frames, fps)}" srcEnable="audio"/>'
        )

    def transition(boundary: int, d: int) -> str:
        # Centered on the cut: half the dissolve on each side of the boundary.
        return (f'<transition name="Cross Dissolve" offset="{_rt(boundary - d // 2, fps)}" '
                f'duration="{_rt(d, fps)}"/>')

    spine_elements: list[str] = []

    if fade_in_frames > 0 or (bed_xml and not spec.clips):
        gap = f'<gap name="Gap" offset="{_rt(0, fps)}" duration="{_rt(bounds[0], fps)}" start="0/1s"'
        spine_elements.append(f"{gap}>{bed_xml}</gap>" if bed_xml else f"{gap}/>")
        bed_xml = ""
        if fade_in_frames > 0:
            spine_elements.append(transition(bounds[0], fade_in_frames))

    last_index = len(spec.clips) - 1
    for i, clip in enumerate(spec.clips):
        asset_id = asset_for(clip.source_path)
        clip_name = clip.name or Path(clip.source_path).stem
        attrs = (
            f'<asset-clip name="{html.escape(clip_name)}" '
            f'ref="{asset_id}" '
            f'offset="{_rt(bounds[i], fps)}" '
            f'start="{_rational(clip.in_seconds, fps)}" '
            f'duration="{_rt(bounds[i + 1] - bounds[i], fps)}" '
            f'lane="{clip.lane}"'
        )
        if i == 0 and bed_xml:
            spine_elements.append(f"{attrs}>{bed_xml}</asset-clip>")
        else:
            spine_elements.append(f"{attrs}/>")
        if i < last_index and lay.edges[i + 1] > 0:
            spine_elements.append(transition(bounds[i + 1], lay.edges[i + 1]))

    if fade_out_frames > 0:
        spine_elements.append(transition(bounds[-1], fade_out_frames))
        spine_elements.append(
            f'<gap name="Gap" offset="{_rt(bounds[-1], fps)}" '
            f'duration="{_rt(fade_out_frames, fps)}" start="0/1s"/>'
        )

    total_duration = _rt(total_frames, fps)
    spine_body = "\n".join("        " + e for e in spine_elements)
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
  <resources>
    <format id="{fmt_id}" name="FFVideoFormat{spec.height}p{int(fps)}"
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
