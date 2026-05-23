"""Roughcut planning — turn raw analysis (scenes, transcript) into a TimelineSpec.

This is the "editorial intelligence" layer. It does not call an LLM directly —
the MCP server returns the analyzed material to Claude, Claude makes editing
decisions, and Claude calls back with a RoughCutPlan. This file just provides
the data types and helpers for that round trip.

Design rationale: keeping the model decoupled means the user's Claude (free,
local, or API) does the creative work; this code only does the deterministic
plumbing.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

from resolve_pilot.analyzers import SceneCut, MediaInfo, probe_media
from resolve_pilot.exporters.fcpxml import Clip, TimelineSpec
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
    transition_out_seconds: float = 0.0


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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RoughCutPlan:
        clips = [RoughCutClip(**c) for c in data.get("clips", [])]
        return cls(
            title=data["title"],
            fps=float(data.get("fps", 24.0)),
            width=int(data.get("width", 1920)),
            height=int(data.get("height", 1080)),
            clips=clips,
            notes=data.get("notes", ""),
            default_transition_seconds=float(data.get("default_transition_seconds", 0.0)),
            fade_in_seconds=float(data.get("fade_in_seconds", 0.0)),
            fade_out_seconds=float(data.get("fade_out_seconds", 0.0)),
        )


def plan_from_transcript(
    source_path: str | Path,
    segments: list[Segment],
    title: str = "Transcript Roughcut",
    pad_seconds: float = 0.2,
    fps: float | None = None,
) -> RoughCutPlan:
    """Build a baseline rough cut from a transcript: one clip per spoken segment.

    Claude can then edit the plan — drop weak takes, reorder, tighten — before
    handing to plan_to_timeline_spec().
    """
    src = Path(source_path).expanduser().resolve()
    info = probe_media(src)
    out_fps = fps if fps else (info.fps or 24.0)
    clips: list[RoughCutClip] = []
    for s in segments:
        start = max(0.0, s.start - pad_seconds)
        end = min(info.duration_seconds, s.end + pad_seconds)
        if end <= start:
            continue
        clips.append(RoughCutClip(
            source_path=str(src),
            in_seconds=start,
            duration_seconds=end - start,
            rationale=f"transcript: {s.text[:80]}",
            name=s.text[:40],
        ))
    return RoughCutPlan(
        title=title,
        fps=out_fps,
        width=info.width or 1920,
        height=info.height or 1080,
        clips=clips,
        notes=f"Built from {len(segments)} transcript segments.",
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

    boundaries = [0.0] + [s.pts_seconds for s in scenes] + [info.duration_seconds]
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


def plan_to_timeline_spec(plan: RoughCutPlan) -> TimelineSpec:
    """Convert a RoughCutPlan into the TimelineSpec used by the FCPXML exporter."""
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
    )
