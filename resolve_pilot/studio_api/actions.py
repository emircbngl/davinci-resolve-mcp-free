"""High-level Studio actions, each returning plain dicts safe to serialize.

Each function fails fast with StudioNotAvailable if the bridge isn't usable.
We deliberately keep the surface narrow — the heavy lifting (e.g. building
rough cuts) belongs in the editorial layer, which is API-edition agnostic.
"""
from __future__ import annotations

from typing import Any

from resolve_pilot.studio_api.bridge import get_resolve


def _pm():
    return get_resolve().GetProjectManager()


def _current_project():
    p = _pm().GetCurrentProject()
    if p is None:
        raise RuntimeError("No project is open in Resolve.")
    return p


def _current_timeline():
    tl = _current_project().GetCurrentTimeline()
    if tl is None:
        raise RuntimeError("No timeline is open in Resolve.")
    return tl


def list_projects() -> list[str]:
    """Return the names of every project in the current Resolve database."""
    pm = _pm()
    return list(pm.GetProjectListInCurrentFolder() or [])


def list_timelines() -> list[dict[str, Any]]:
    """Return basic info for every timeline in the open project."""
    p = _current_project()
    count = int(p.GetTimelineCount() or 0)
    out: list[dict[str, Any]] = []
    for i in range(1, count + 1):
        tl = p.GetTimelineByIndex(i)
        if tl is None:
            continue
        out.append({
            "index": i,
            "name": tl.GetName(),
            "start_frame": tl.GetStartFrame(),
            "end_frame": tl.GetEndFrame(),
            "video_track_count": tl.GetTrackCount("video"),
            "audio_track_count": tl.GetTrackCount("audio"),
        })
    return out


def timeline_clips_summary(track_type: str = "video", track_index: int = 1) -> list[dict[str, Any]]:
    """Return a per-clip summary on a single track of the current timeline."""
    tl = _current_timeline()
    items = tl.GetItemListInTrack(track_type, int(track_index)) or []
    out: list[dict[str, Any]] = []
    for it in items:
        try:
            mpi = it.GetMediaPoolItem()
            src = mpi.GetClipProperty("File Path") if mpi else None
        except Exception:
            src = None
        out.append({
            "name": it.GetName(),
            "start": it.GetStart(),
            "end": it.GetEnd(),
            "duration": it.GetDuration(),
            "source_path": src,
        })
    return out


def add_marker(
    frame: int, color: str = "Blue", name: str = "Marker",
    note: str = "", duration: int = 1,
) -> bool:
    """Add a marker on the current timeline at `frame`."""
    tl = _current_timeline()
    return bool(tl.AddMarker(int(frame), color, name, note, int(duration), ""))


def set_clip_color(track: int = 1, color: str = "Orange") -> int:
    """Color-code every clip on V<track>. Returns count touched."""
    tl = _current_timeline()
    items = tl.GetItemListInTrack("video", int(track)) or []
    n = 0
    for it in items:
        if it.SetClipColor(color):
            n += 1
    return n


def apply_lut(lut_path: str, track: int = 1, node_index: int = 1) -> int:
    """Apply a LUT file to every clip on V<track> at node N. Returns clip count touched."""
    tl = _current_timeline()
    items = tl.GetItemListInTrack("video", int(track)) or []
    n = 0
    for it in items:
        if it.SetLUT(int(node_index), lut_path):
            n += 1
    return n


def queue_render_job(
    preset: str = "H.264 Master",
    target_dir: str = "~/Desktop/Renders",
    custom_name: str | None = None,
) -> str | None:
    """Queue one render job for the current timeline. Returns job id or None."""
    import os
    p = _current_project()
    p.LoadRenderPreset(preset)
    settings: dict[str, Any] = {"TargetDir": os.path.expanduser(target_dir)}
    if custom_name:
        settings["CustomName"] = custom_name
    p.SetRenderSettings(settings)
    return p.AddRenderJob()


def project_summary() -> dict[str, Any]:
    """One-call snapshot of the open project — useful as a first call each session."""
    p = _current_project()
    tl = p.GetCurrentTimeline()
    return {
        "project": p.GetName(),
        "timeline_count": p.GetTimelineCount(),
        "fps": p.GetSetting("timelineFrameRate"),
        "resolution": [
            p.GetSetting("timelineResolutionWidth"),
            p.GetSetting("timelineResolutionHeight"),
        ],
        "current_timeline": tl.GetName() if tl else None,
        "current_timeline_video_tracks": tl.GetTrackCount("video") if tl else None,
        "current_timeline_audio_tracks": tl.GetTrackCount("audio") if tl else None,
    }
