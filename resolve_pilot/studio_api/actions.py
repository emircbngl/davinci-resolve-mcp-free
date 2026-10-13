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


# ────────────────────────────────────────────────────────────────────────────
# Dict-returning actions mirroring the Lua snippets, so every channel reports
# results in the same shape (see resolve_pilot.control).
# ────────────────────────────────────────────────────────────────────────────

def _try(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _timeline_fps(project, timeline) -> float:
    return (
        _num(_try(timeline.GetSetting, "timelineFrameRate"))
        or _num(project.GetSetting("timelineFrameRate"))
        or 24.0
    )


def resolve_state(
    include_clips: bool = False,
    track_type: str = "video",
    track_index: int = 1,
    include_markers: bool = False,
    max_clips: int = 200,
) -> dict[str, Any]:
    """Project / timeline snapshot in the same shape as the `get_state` snippet."""
    r = get_resolve()
    state: dict[str, Any] = {
        "ok": True,
        "product": _try(r.GetProductName),
        "version": _try(r.GetVersionString),
        "page": _try(r.GetCurrentPage),
    }
    p = r.GetProjectManager().GetCurrentProject()
    if p is None:
        state["project"] = "(none)"
        return state
    state["project"] = p.GetName()
    state["project_fps"] = _num(p.GetSetting("timelineFrameRate"))
    state["resolution"] = [
        _num(p.GetSetting("timelineResolutionWidth")),
        _num(p.GetSetting("timelineResolutionHeight")),
    ]
    timelines = [p.GetTimelineByIndex(i) for i in range(1, int(p.GetTimelineCount() or 0) + 1)]
    state["timelines"] = [t.GetName() for t in timelines if t]
    tl = p.GetCurrentTimeline()
    if tl is None:
        return state
    fps = _timeline_fps(p, tl)
    start = tl.GetStartFrame()
    markers = _try(tl.GetMarkers) or {}
    cur: dict[str, Any] = {
        "name": tl.GetName(),
        "fps": fps,
        "start_frame": start,
        "end_frame": tl.GetEndFrame(),
        "duration_seconds": (tl.GetEndFrame() - start) / fps,
        "start_timecode": _try(tl.GetStartTimecode),
        "playhead_timecode": _try(tl.GetCurrentTimecode),
        "video_tracks": tl.GetTrackCount("video"),
        "audio_tracks": tl.GetTrackCount("audio"),
        "subtitle_tracks": _try(tl.GetTrackCount, "subtitle"),
        "marker_count": len(markers),
    }
    if include_markers:
        cur["markers"] = [
            {"frame": f, "seconds": f / fps, "color": m.get("color"), "name": m.get("name"),
             "note": m.get("note"), "duration": m.get("duration")}
            for f, m in sorted(markers.items())
        ]
    if include_clips:
        items = tl.GetItemListInTrack(track_type, int(track_index)) or []
        clips = []
        for i, it in enumerate(items[:max_clips]):
            mpi = _try(it.GetMediaPoolItem)
            clips.append({
                "index": i,
                "name": it.GetName(),
                "start_seconds": (it.GetStart() - start) / fps,
                "duration_seconds": it.GetDuration() / fps,
                "start_frame": it.GetStart(),
                "end_frame": it.GetEnd(),
                "source_in_frame": _try(it.GetLeftOffset),
                "clip_color": _try(it.GetClipColor),
                "source_path": _try(mpi.GetClipProperty, "File Path") if mpi else None,
            })
        cur["track"] = f"{track_type} {track_index}"
        cur["clip_count"] = len(items)
        cur["clips"] = clips
    state["current_timeline"] = cur
    return state


def add_markers_batch(markers: list[dict]) -> dict[str, Any]:
    """Add normalized markers ({frame|seconds, color, name, note, duration})."""
    p = _current_project()
    tl = _current_timeline()
    fps = _timeline_fps(p, tl)
    added, skipped = 0, []
    for m in markers:
        frame = int(m["frame"]) if m.get("frame") is not None else int(float(m["seconds"]) * fps + 0.5)
        if tl.AddMarker(frame, m["color"], m["name"], m["note"], int(m["duration"]), ""):
            added += 1
        else:
            skipped.append(frame)
    return {"ok": True, "added": added, "requested": len(markers), "skipped_frames": skipped,
            "timeline": tl.GetName(), "timeline_fps": fps}


def import_timeline(path: str, timeline_name: str | None = None,
                    import_source_clips: bool = True) -> dict[str, Any]:
    """Import FCPXML/EDL/AAF/DRT/OTIO as a new timeline and make it current."""
    import os
    p = _current_project()
    opts: dict[str, Any] = {"importSourceClips": bool(import_source_clips)}
    if timeline_name:
        opts["timelineName"] = timeline_name
    full = os.path.expanduser(path)
    tl = p.GetMediaPool().ImportTimelineFromFile(full, opts)
    if not tl:
        return {"ok": False, "error": f"Resolve could not import {full}. Check the file exists, "
                "is valid, and that no timeline with the same name already exists."}
    p.SetCurrentTimeline(tl)
    n = sum(len(tl.GetItemListInTrack("video", t) or [])
            for t in range(1, int(tl.GetTrackCount("video") or 0) + 1))
    return {"ok": True, "timeline": tl.GetName(), "video_clip_count": n,
            "start_frame": tl.GetStartFrame(), "end_frame": tl.GetEndFrame()}


def render_current_timeline(preset: str = "H.264 Master", target_dir: str = "~/Desktop/Renders",
                            custom_name: str | None = None, start: bool = False) -> dict[str, Any]:
    import os
    p = _current_project()
    tl = _current_timeline()
    if not p.LoadRenderPreset(preset):
        return {"ok": False, "error": f"Unknown render preset: {preset}",
                "available_presets": list(p.GetRenderPresetList() or [])}
    settings: dict[str, Any] = {"TargetDir": os.path.expanduser(target_dir)}
    if custom_name:
        settings["CustomName"] = custom_name
    p.SetRenderSettings(settings)
    job = p.AddRenderJob()
    if not job:
        return {"ok": False, "error": "AddRenderJob failed. Check the Deliver page."}
    if start:
        p.StartRendering(job)
    return {"ok": True, "job_id": job, "timeline": tl.GetName(), "preset": preset, "rendering": start}


def open_page(page: str) -> dict[str, Any]:
    return {"ok": bool(get_resolve().OpenPage(page)), "page": page}


def apply_lut_report(lut_path: str, track: int = 1, node_index: int = 1) -> dict[str, Any]:
    """apply_lut with the same result shape as the Lua snippet."""
    tl = _current_timeline()
    items = tl.GetItemListInTrack("video", int(track)) or []
    applied = sum(1 for it in items if it.SetLUT(int(node_index), lut_path))
    return {"ok": True, "applied": applied, "total": len(items), "node_index": int(node_index)}
