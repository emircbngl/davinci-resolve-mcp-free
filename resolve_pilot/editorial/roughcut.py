"""Roughcut planning — turn raw analysis (scenes, transcript) into a TimelineSpec.

This is the "editorial intelligence" layer. It does not call an LLM directly —
the MCP server returns the analyzed material to Claude, Claude makes editing
decisions, and Claude calls back with a RoughCutPlan. This file just provides
the data types and helpers for that round trip, plus `validate_plan`, which
turns the mistakes an agent typically makes (clips past the end of the source,
dissolves without handles, overlapping reuse) into warnings it can act on.

Design rationale: keeping the model decoupled means the user's Claude (free,
local, or API) does the creative work; this code only does the deterministic
plumbing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from resolve_pilot.analyzers import MediaInfo, SceneCut, probe_media, probe_media_cached
from resolve_pilot.exporters.fcpxml import AudioBed, Clip, Layout, TimelineSpec, _frames, layout
from resolve_pilot.transcribe import Segment


@dataclass
class RoughCutClip:
    """One clip in the rough cut, sourced from a single file.

    `transition_out_seconds` adds a cross-dissolve into the next clip.
    """
    source_path: str
    in_seconds: float
    duration_seconds: float
    rationale: str = ""  # why this clip was picked — useful for review
    name: str | None = None
    transition_out_seconds: float | None = None  # None = plan default; 0 = hard cut

    @property
    def out_seconds(self) -> float:
        return self.in_seconds + self.duration_seconds


@dataclass
class RoughCutAudioBed:
    """A music / audio track laid under the whole cut (e.g. the song of a music video).

    `offset_seconds` is where it starts on the timeline; `duration_seconds=None`
    runs it to the end of the timeline.
    """
    source_path: str
    in_seconds: float = 0.0
    duration_seconds: float | None = None
    offset_seconds: float = 0.0
    name: str | None = None


@dataclass
class RoughCutPlan:
    """A complete rough cut waiting to be rendered to FCPXML."""
    title: str
    fps: float = 24.0
    width: int = 1920
    height: int = 1080
    clips: list[RoughCutClip] = field(default_factory=list)
    notes: str = ""
    # Cross-dissolve duration applied to every cut without a per-clip override.
    default_transition_seconds: float = 0.0
    # Fade up from / down to black at the start and end of the timeline.
    fade_in_seconds: float = 0.0
    fade_out_seconds: float = 0.0
    audio_bed: RoughCutAudioBed | None = None

    @property
    def total_seconds(self) -> float:
        return self.fade_in_seconds + sum(c.duration_seconds for c in self.clips) + self.fade_out_seconds

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RoughCutPlan:
        bed = data.get("audio_bed")
        return cls(
            title=data["title"],
            fps=float(data.get("fps", 24.0)),
            width=int(data.get("width", 1920)),
            height=int(data.get("height", 1080)),
            clips=[clip_from_dict(c) for c in data.get("clips", [])],
            notes=data.get("notes", ""),
            default_transition_seconds=float(data.get("default_transition_seconds", 0.0)),
            fade_in_seconds=float(data.get("fade_in_seconds", 0.0)),
            fade_out_seconds=float(data.get("fade_out_seconds", 0.0)),
            audio_bed=RoughCutAudioBed(**_known(RoughCutAudioBed, bed)) if bed else None,
        )


def _known(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def clip_from_dict(data: dict) -> RoughCutClip:
    """Build a clip from a dict, accepting `out_seconds` instead of `duration_seconds`."""
    d = dict(data)
    if d.get("duration_seconds") is None and d.get("out_seconds") is not None:
        d["duration_seconds"] = float(d["out_seconds"]) - float(d.get("in_seconds", 0.0))
    return RoughCutClip(**_known(RoughCutClip, d))


def plan_from_transcript(
    source_path: str | Path,
    segments: list[Segment],
    title: str = "Transcript Roughcut",
    pad_seconds: float = 0.2,
    fps: float | None = None,
    segment_indices: list[int] | None = None,
) -> RoughCutPlan:
    """Build a rough cut from transcript segments: one clip per spoken segment.

    `segment_indices` keeps only those segments, in that order (an editorial
    selection); by default every segment is used in source order.

    Padding never makes neighbouring clips overlap: when two clips that follow
    each other in the plan are also consecutive in the source, the boundary is
    placed between them, so the same words are never played twice.
    """
    src = Path(source_path).expanduser().resolve()
    info = probe_media(src)
    out_fps = fps if fps else (info.fps or 24.0)
    if segment_indices is None:
        picked = list(enumerate(segments))
    else:
        bad = [i for i in segment_indices if not 0 <= i < len(segments)]
        if bad:
            raise ValueError(
                f"segment_indices {bad} out of range: the transcript has {len(segments)} "
                f"segments (0-{len(segments) - 1})"
            )
        picked = [(i, segments[i]) for i in segment_indices]

    spans: list[tuple[int, Segment, float, float]] = []
    for k, s in picked:
        start = max(0.0, s.start - pad_seconds)
        end = min(info.duration_seconds or s.end + pad_seconds, s.end + pad_seconds)
        if spans:
            pk, ps, pstart, pend = spans[-1]
            if s.start >= ps.start and start < pend:
                mid = min(max((ps.end + s.start) / 2, pstart), end)
                spans[-1] = (pk, ps, pstart, mid)
                start = mid
        spans.append((k, s, start, end))

    clips = [
        RoughCutClip(
            source_path=str(src),
            in_seconds=start,
            duration_seconds=end - start,
            rationale=f"segment {k}: {s.text[:100]}",
            name=s.text[:40] or f"segment {k}",
        )
        for k, s, start, end in spans
        if end - start > 1e-3
    ]
    return RoughCutPlan(
        title=title,
        fps=out_fps,
        width=info.width or 1920,
        height=info.height or 1080,
        clips=clips,
        notes=f"Built from {len(clips)} of {len(segments)} transcript segments.",
    )


def plan_from_scenes(
    source_path: str | Path,
    scenes: list[SceneCut],
    title: str = "Scene Roughcut",
    min_duration: float = 1.0,
    fps: float | None = None,
) -> RoughCutPlan:
    """Build a baseline rough cut from scene cuts: one clip per detected scene."""
    src = Path(source_path).expanduser().resolve()
    info = probe_media(src)
    out_fps = fps if fps else (info.fps or 24.0)

    boundaries = [0.0] + sorted(s.pts_seconds for s in scenes) + [info.duration_seconds]
    clips: list[RoughCutClip] = []
    for a, b in zip(boundaries, boundaries[1:]):
        if b - a < min_duration:
            continue
        clips.append(RoughCutClip(
            source_path=str(src),
            in_seconds=a,
            duration_seconds=b - a,
            rationale=f"scene {a:.2f}s → {b:.2f}s",
        ))
    return RoughCutPlan(
        title=title,
        fps=out_fps,
        width=info.width or 1920,
        height=info.height or 1080,
        clips=clips,
        notes=f"Built from {len(scenes)} scene cuts.",
    )


def _transition_for(plan: RoughCutPlan, clip: RoughCutClip) -> float:
    if clip.transition_out_seconds is None:
        return plan.default_transition_seconds
    return clip.transition_out_seconds


def validate_plan(plan: RoughCutPlan) -> dict[str, list[str]]:
    """Return {"errors": [...], "warnings": [...]} for a plan.

    Errors make the FCPXML unusable and block export; warnings describe things
    Resolve will silently "fix" in ways the editor probably does not want.
    Source checks need ffprobe and are skipped for files it cannot read.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if plan.fps <= 0:
        errors.append(f"fps must be > 0 (got {plan.fps})")
    if not plan.clips:
        errors.append("the plan has no clips")
    tol = 1.0 / plan.fps if plan.fps > 0 else 0.05
    infos: dict[str, MediaInfo | None] = {}

    def info(path: str) -> MediaInfo | None:
        if path not in infos:
            infos[path] = probe_media_cached(path)
        return infos[path]

    for i, c in enumerate(plan.clips):
        if c.duration_seconds <= 0:
            errors.append(f"clip {i}: duration_seconds must be > 0 (got {c.duration_seconds:g})")
        if c.in_seconds < 0:
            errors.append(f"clip {i}: in_seconds must be >= 0 (got {c.in_seconds:g})")
        mi = info(c.source_path)
        if mi is None:
            if not Path(c.source_path).expanduser().exists():
                warnings.append(f"clip {i}: source file not found: {c.source_path}")
        elif mi.duration_seconds and c.out_seconds > mi.duration_seconds + tol:
            warnings.append(
                f"clip {i}: ends at {c.out_seconds:.2f}s but {Path(c.source_path).name} is only "
                f"{mi.duration_seconds:.2f}s long"
            )

    # Frame positions exactly as the exporter will write them.
    lay = layout(plan_to_timeline_spec(plan)) if plan.clips and plan.fps > 0 else None
    flagged: set[int] = set()  # clips already reported for an oversized dissolve
    for i in range(len(plan.clips) - 1):
        a, b = plan.clips[i], plan.clips[i + 1]
        d = _transition_for(plan, a)
        if d <= 0:
            continue
        ma, mb = info(a.source_path), info(b.source_path)
        if ma and mb and ma.audio_codec and mb.video_codec and not mb.audio_codec:
            # Measured on Resolve 20.3: a dissolve from a clip with sound into one
            # without makes Resolve wrap the silent clip in a compound clip on import.
            warnings.append(
                f"transition {i}->{i + 1}: clip {i + 1} has no audio, so Resolve imports it as a "
                f"compound clip; set transition_out_seconds=0 on clip {i} for a plain clip")
        if lay is not None:
            frames, clip_frames = lay.edges[i + 1], lay.clip_frames
            if frames > min(clip_frames[i], clip_frames[i + 1]):
                short = i if clip_frames[i] < clip_frames[i + 1] else i + 1
                flagged.add(short)
                errors.append(f"transition {i}->{i + 1} is {d:.2f}s ({frames} frames), longer "
                              f"than clip {short} ({clip_frames[short]} frames)")
                continue
        half = d / 2
        if b.in_seconds + tol < half:
            warnings.append(
                f"transition {i}->{i + 1}: clip {i + 1} starts {b.in_seconds:.2f}s into its source, "
                f"less than the {half:.2f}s handle the dissolve needs; Resolve will shorten or shift it"
            )
        mi = info(a.source_path)
        if mi and mi.duration_seconds and a.out_seconds + half > mi.duration_seconds + tol:
            warnings.append(
                f"transition {i}->{i + 1}: clip {i} has less than {half:.2f}s of media after its out "
                "point; Resolve will shorten or shift the dissolve"
            )

    if lay is not None:
        _check_frames(plan, lay, errors, warnings, flagged)

    if plan.audio_bed:
        bed = plan.audio_bed
        mi = info(bed.source_path)
        want = bed.duration_seconds or max(0.0, plan.total_seconds - bed.offset_seconds)
        if lay is not None and lay.bed_offset >= lay.total_frames:
            errors.append(
                f"audio bed starts at {bed.offset_seconds:.2f}s, at or after the end of the timeline "
                f"({plan.total_seconds:.2f}s). offset_seconds is the timeline position; the "
                "position inside the song is in_seconds"
            )
        elif lay is not None and lay.bed_frames < 1:
            errors.append("audio bed is shorter than one frame")
        if mi is None:
            if not Path(bed.source_path).expanduser().exists():
                warnings.append(f"audio bed: file not found: {bed.source_path}")
        elif mi.duration_seconds and bed.in_seconds + want > mi.duration_seconds + tol:
            warnings.append(
                f"audio bed: needs {want:.2f}s from {bed.in_seconds:.2f}s but "
                f"{Path(bed.source_path).name} is {mi.duration_seconds:.2f}s long; the music ends early"
            )
        if bed.duration_seconds and bed.offset_seconds + bed.duration_seconds + tol < plan.total_seconds:
            warnings.append(
                f"audio bed ends at {bed.offset_seconds + bed.duration_seconds:.2f}s, before the "
                f"timeline ({plan.total_seconds:.2f}s)"
            )

    for path in dict.fromkeys(c.source_path for c in plan.clips):
        mi = info(path)
        if mi and mi.fps and plan.fps > 0 and abs(mi.fps - plan.fps) > 0.01:
            warnings.append(
                f"{Path(path).name} is {mi.fps:.3f} fps but the timeline is {plan.fps:g} fps; "
                "Resolve will conform it, check the cut points"
            )

    by_source: dict[str, list[tuple[int, RoughCutClip]]] = {}
    for i, c in enumerate(plan.clips):
        by_source.setdefault(c.source_path, []).append((i, c))
    for path, items in by_source.items():
        items.sort(key=lambda ic: ic[1].in_seconds)
        for (i, a), (j, b) in zip(items, items[1:]):
            overlap = a.out_seconds - b.in_seconds
            if overlap > tol:
                warnings.append(
                    f"clips {i} and {j} reuse {overlap:.2f}s of the same footage from {Path(path).name}"
                )
    return {"errors": errors, "warnings": warnings}


def _check_frames(plan: RoughCutPlan, lay: Layout, errors: list[str], warnings: list[str],
                  flagged: set[int]) -> None:
    """Check clips, dissolves and fades after the exporter's frame rounding.

    Seconds that look fine can still collide once rounded: a dissolve as long as
    its clip leaves no room for the next one, and a short fade rounds to nothing.
    """
    fps = plan.fps
    edges = lay.edges
    for i, frames in enumerate(lay.clip_frames):
        if frames <= 0:
            if plan.clips[i].duration_seconds > 0:
                errors.append(f"clip {i} is shorter than one frame at {fps:g} fps")
            continue
        need = (edges[i] - edges[i] // 2) + edges[i + 1] // 2
        if need > frames and i not in flagged:
            errors.append(
                f"clip {i} is {frames} frames but the dissolves on its edges use {need}; "
                "shorten the dissolves or lengthen the clip"
            )
    for label, seconds in [("fade_in_seconds", plan.fade_in_seconds),
                           ("fade_out_seconds", plan.fade_out_seconds),
                           ("default_transition_seconds", plan.default_transition_seconds)]:
        if seconds > 0 and _frames(seconds, fps) == 0:
            warnings.append(f"{label}={seconds:g} rounds to 0 frames at {fps:g} fps and is skipped")
    for i, c in enumerate(plan.clips[:-1]):
        if (c.transition_out_seconds or 0) > 0 and _frames(c.transition_out_seconds, fps) == 0:
            warnings.append(f"clip {i}: transition_out_seconds rounds to 0 frames and is skipped")


def plan_to_timeline_spec(plan: RoughCutPlan) -> TimelineSpec:
    """Convert a RoughCutPlan into the TimelineSpec used by the FCPXML exporter."""
    bed = plan.audio_bed
    return TimelineSpec(
        name=plan.title,
        fps=plan.fps,
        width=plan.width,
        height=plan.height,
        clips=[
            Clip(
                source_path=c.source_path,
                in_seconds=c.in_seconds,
                duration_seconds=c.duration_seconds,
                name=c.name,
                notes=c.rationale,
                transition_out_seconds=c.transition_out_seconds,
            )
            for c in plan.clips
        ],
        default_transition_seconds=plan.default_transition_seconds,
        fade_in_seconds=plan.fade_in_seconds,
        fade_out_seconds=plan.fade_out_seconds,
        audio_bed=AudioBed(
            source_path=bed.source_path,
            in_seconds=bed.in_seconds,
            duration_seconds=bed.duration_seconds,
            offset_seconds=bed.offset_seconds,
            name=bed.name,
        ) if bed else None,
    )
