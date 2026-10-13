"""High-level Studio actions, each returning plain dicts safe to serialize.

Each function fails fast with StudioNotAvailable if the bridge isn't usable.
We deliberately keep the surface narrow — the heavy lifting (e.g. building
rough cuts) belongs in the editorial layer, which is API-edition agnostic.
"""
from __future__ import annotations

import os
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


def _is_transition(item) -> bool:
    """GetItemListInTrack also returns transitions; they have no media pool item and
    none of the transform properties clips, titles and generators carry (Resolve 20.3)."""
    if item.GetMediaPoolItem():
        return False
    props = item.GetProperty()
    return not isinstance(props, dict) or "Opacity" not in props


def clip_items(tl, kind: str, index: int) -> list:
    """Items on a track, without transitions."""
    return [it for it in (tl.GetItemListInTrack(kind, int(index)) or []) if not _is_transition(it)]


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
    items = clip_items(tl, track_type, track_index)
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
    items = clip_items(tl, "video", track)
    n = 0
    for it in items:
        if it.SetClipColor(color):
            n += 1
    return n


def apply_lut(lut_path: str, track: int = 1, node_index: int = 1) -> int:
    """Apply a LUT file to every clip on V<track> at node N. Returns clip count touched."""
    tl = _current_timeline()
    items = clip_items(tl, "video", track)
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
        all_items = tl.GetItemListInTrack(track_type, int(track_index)) or []
        items = [it for it in all_items if not _is_transition(it)]
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
        cur["transition_count"] = len(all_items) - len(items)
        cur["clips"] = clips
    state["current_timeline"] = cur
    return state


def add_markers_batch(markers: list[dict], replace_owned_prefix: str | None = None) -> dict[str, Any]:
    """Add normalized markers; mirrors lua_snippets.snippet_add_markers."""
    from resolve_pilot.lua_snippets.library import validate_owned_prefix

    prefix = validate_owned_prefix(replace_owned_prefix) if replace_owned_prefix else None
    p = _current_project()
    tl = _current_timeline()
    fps = _timeline_fps(p, tl)
    removed = 0
    if prefix:
        owned = [m.get("customData") for m in (tl.GetMarkers() or {}).values()]
        for data in owned:
            if isinstance(data, str) and data.startswith(prefix) and tl.DeleteMarkerByCustomData(data):
                removed += 1
    added, skipped = 0, []
    for m in markers:
        frame = int(m["frame"]) if m.get("frame") is not None else int(float(m["seconds"]) * fps + 0.5)
        duration = int(m["duration"])
        if m.get("duration_seconds"):
            duration = max(1, int(float(m["duration_seconds"]) * fps + 0.5))
        if tl.AddMarker(frame, m["color"], m["name"], m["note"], duration, m.get("custom_data") or ""):
            added += 1
        else:
            skipped.append(frame)
    return {"ok": True, "added": added, "requested": len(markers), "skipped_frames": skipped,
            "removed_previous": removed, "timeline": tl.GetName(), "timeline_fps": fps}


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
    n = sum(len(clip_items(tl, "video", t))
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
    project = _current_project()
    if hasattr(project, "RefreshLUTList"):
        project.RefreshLUTList()
    items = clip_items(tl, "video", int(track))
    applied = sum(1 for it in items if it.SetLUT(int(node_index), lut_path))
    out = {"ok": applied > 0 or not items, "applied": applied, "total": len(items),
           "node_index": int(node_index)}
    if not out["ok"]:
        out["error"] = ("Resolve rejected the LUT for every clip. It must sit inside Resolve's LUT "
                        "folder, and the node index must exist on each clip.")
    return out


_PLAYHEAD_PAGES = ("cut", "edit", "color", "fairlight", "deliver")


def frames_to_timecode(total: int, fps: float, drop_frame: bool) -> str:
    """Frame index -> timecode, drop-frame aware (same rule as the Lua snippet)."""
    base = int(fps + 0.5)
    if drop_frame:
        drop = base // 15
        per10, per1 = base * 600 - drop * 9, base * 60 - drop
        tens, rem = divmod(total, per10)
        total += drop * 9 * tens
        if rem > drop:
            total += drop * ((rem - drop) // per1)
    s = total // base
    sep = ";" if drop_frame else ":"
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}{sep}{total % base:02d}"


def export_timeline_stills(positions: list[dict], out_dir: str) -> dict[str, Any]:
    """Studio twin of snippet_export_timeline_stills."""
    resolve = get_resolve()
    project = _current_project()
    tl = _current_timeline()
    fps = _timeline_fps(project, tl)
    df = str(tl.GetSetting("timelineDropFrameTimecode")) == "1"
    start, last = tl.GetStartFrame(), tl.GetEndFrame() - 1
    page = resolve.GetCurrentPage()
    switched = page not in _PLAYHEAD_PAGES
    if switched:
        resolve.OpenPage("edit")
    before = tl.GetCurrentTimecode()
    stills = []
    try:
        for i, pos in enumerate(positions):
            offset = int(pos["frame"]) if pos.get("frame") is not None else int(round(pos["seconds"] * fps))
            row: dict[str, Any] = {"index": i, "frame": offset, "seconds": offset / fps}
            if offset < 0 or start + offset > last:
                row.update(ok=False, error=f"outside the timeline (0-{last - start} frames)")
            else:
                row["timecode"] = frames_to_timecode(start + offset, fps, df)
                if not tl.SetCurrentTimecode(row["timecode"]):
                    row.update(ok=False, error=f"Resolve refused to move the playhead to {row['timecode']}")
                else:
                    row["path"] = os.path.join(out_dir, f"still_{i:03d}.png")
                    row["ok"] = bool(project.ExportCurrentFrameAsStill(row["path"]))
                    if not row["ok"]:
                        row["error"] = "ExportCurrentFrameAsStill failed"
            stills.append(row)
    finally:
        if before:
            tl.SetCurrentTimecode(before)
        if switched and page:
            resolve.OpenPage(page)
    return {"ok": True, "timeline": tl.GetName(), "fps": fps, "drop_frame": df, "stills": stills}


def render_jobs_status() -> dict[str, Any]:
    """Every render job with its status, plus whether Resolve is rendering now."""
    project = _current_project()
    jobs = []
    for job in project.GetRenderJobList() or []:
        status = project.GetRenderJobStatus(job.get("JobId")) or {}
        jobs.append({"job_id": job.get("JobId"), "name": job.get("RenderJobName"),
                     "timeline": job.get("TimelineName"),
                     "output": os.path.join(job.get("TargetDir") or "", job.get("OutputFilename") or ""),
                     "status": status.get("JobStatus"), "percent": status.get("CompletionPercentage"),
                     "seconds_left": (status.get("EstimatedTimeRemainingInMs") or 0) / 1000 or None,
                     "error": status.get("Error") or None})
    return {"ok": True, "rendering": bool(project.IsRenderingInProgress()), "jobs": jobs}


def _call(obj, method: str, *args) -> tuple[bool, Any]:
    """(succeeded, value or error text), like the Lua snippets' pcall wrapper."""
    fn = getattr(obj, method, None)
    if not callable(fn):
        return False, f"{method} is not available"
    try:
        return True, fn(*args)
    except Exception as e:  # the native module raises on bad keys / types
        return False, str(e)


def set_clip_properties(track_type: str = "video", track: int = 1, indices: list[int] | None = None,
                        properties: dict[str, Any] | None = None,
                        enabled: bool | None = None) -> dict[str, Any]:
    """Studio twin of snippet_set_clip_properties (same result shape, read-back `ok`)."""
    from resolve_pilot.lua_snippets.library import normalize_clip_properties, values_match

    rows = normalize_clip_properties(properties)
    resolve = get_resolve()
    resolved: dict[str, Any] = {}
    for p in rows:
        if "const" in p:
            value = getattr(resolve, p["const"], None)
            if value is None:
                return {"ok": False, "error": f"This Resolve has no resolve.{p['const']} constant; "
                                              f"pass the integer value for {p['key']} instead."}
            p["value"] = resolved[p["key"]] = value
    tl = _current_timeline()
    items = clip_items(tl, track_type, int(track))
    if indices is None:
        targets, missing = list(enumerate(items)), []
    else:
        targets = [(i, items[i]) for i in indices if 0 <= i < len(items)]
        missing = [i for i in indices if not 0 <= i < len(items)]
    clips, changed = [], 0
    for index, it in targets:
        row: dict[str, Any] = {"index": index, "name": it.GetName(), "requested": {}, "actual": {},
                               "mismatched": [], "contradicted": [], "ok": True}
        checks = [(p["key"], p["kind"], p["value"], p["requested"], ("SetProperty", p["key"], p["value"]),
                   ("GetProperty", p["key"])) for p in rows]
        if enabled is not None:
            checks.append(("enabled", "bool", enabled, enabled, ("SetClipEnabled", enabled),
                           ("GetClipEnabled",)))
        for key, kind, value, requested, setter, getter in checks:
            row["requested"][key] = requested
            done, said = _call(it, *setter)
            read, got = _call(it, *getter)
            if read and got is not None:
                row["actual"][key] = got
            if not (done and read and values_match(kind, value, got)):
                row["ok"] = False
                row["mismatched"].append(key)
                if not done:
                    row["error"] = str(said)
                elif said is True:
                    row["contradicted"].append(key)
        changed += row["ok"]
        clips.append(row)
    out: dict[str, Any] = {
        "ok": bool(targets) and changed == len(targets) and not missing, "track_type": track_type,
        "track": int(track), "clip_count": len(items), "changed": changed, "clips": clips,
        "missing_indices": missing, "resolved_values": resolved, "timeline": tl.GetName()}
    if not targets:
        out["error"] = (f"No clips to change on {track_type} track {track} "
                        f"({len(items)} clips there).")
    elif not out["ok"]:
        out["error"] = ("Some values did not stick: Resolve's read-back differs from what was "
                        "requested (see clips[].mismatched), or indices are missing.")
    return out


_TRACK_FIELDS = (("name", "SetTrackName", "GetTrackName", "string"),
                 ("enabled", "SetTrackEnable", "GetIsTrackEnabled", "bool"),
                 ("locked", "SetTrackLock", "GetIsTrackLocked", "bool"))


def set_tracks(changes: list[dict] | None = None, add: list[dict] | None = None) -> dict[str, Any]:
    """Studio twin of snippet_set_tracks (same result shape, read-back `ok`)."""
    from resolve_pilot.lua_snippets.library import normalize_track_changes, values_match

    rows, adds = normalize_track_changes(changes, add)
    tl = _current_timeline()

    def count(kind: str) -> int:
        done, n = _call(tl, "GetTrackCount", kind)
        try:
            return int(n) if done and n is not None else 0
        except (TypeError, ValueError):
            return 0

    added, all_ok = [], True
    for a in adds:
        before = count(a["track_type"])
        done, said = _call(tl, "AddTrack", a["track_type"], *([a["sub_type"]] if a.get("sub_type") else []))
        after = count(a["track_type"])
        row: dict[str, Any] = {"track_type": a["track_type"], "before": before, "after": after,
                               "ok": after == before + 1}
        if a.get("sub_type"):
            row["sub_type"] = a["sub_type"]
        if row["ok"]:
            row["index"] = after
        if not done:
            row["error"] = str(said)
        elif not row["ok"]:
            row["error"] = f"AddTrack returned {said} but the track count did not grow"
        all_ok = all_ok and row["ok"]
        added.append(row)
    results = []
    for c in rows:
        row = {"track_type": c["track_type"], "index": c["index"], "requested": {}, "actual": {},
               "mismatched": [], "contradicted": [], "ok": True}
        n = count(c["track_type"])
        if c["index"] > n:
            row.update(ok=False, error=f"There is no {c['track_type']} track {c['index']} ({n} tracks).")
        else:
            for key, setter, getter, kind in _TRACK_FIELDS:
                if key not in c:
                    continue
                row["requested"][key] = c[key]
                done, said = _call(tl, setter, c["track_type"], c["index"], c[key])
                read, got = _call(tl, getter, c["track_type"], c["index"])
                if read and got is not None:
                    row["actual"][key] = got
                same = read and (got == c[key] if kind == "string" else values_match("bool", c[key], got))
                if not (done and same):
                    row["ok"] = False
                    row["mismatched"].append(key)
                    if not done:
                        row["error"] = str(said)
                    elif said is True:
                        row["contradicted"].append(key)
        all_ok = all_ok and row["ok"]
        results.append(row)
    out: dict[str, Any] = {"ok": all_ok, "added": added, "changes": results, "timeline": tl.GetName(),
                           "track_counts": {k: count(k) for k in ("video", "audio", "subtitle")}}
    if not all_ok:
        out["error"] = "Some track changes did not stick (see added[].error and changes[].mismatched)."
    return out


def export_timeline(out_path: str | None = None, export_type: str = "FCPXML_1_10",
                    export_subtype: str | None = None, timeline_name: str | None = None,
                    out_dir: str | None = None, stamp: str | None = None) -> dict[str, Any]:
    """Studio twin of snippet_export_timeline (same result shape)."""
    import re

    from resolve_pilot.lua_snippets.library import EXPORT_FORMATS

    if export_subtype is None:
        export_subtype = {"AAF": "AAF_NEW", "EDL": "NONE"}.get(export_type)
    resolve = get_resolve()
    kind = getattr(resolve, f"EXPORT_{export_type}", None)
    if kind is None:
        return {"ok": False, "error": f"Unknown export_type {export_type} in this Resolve version"}
    sub = getattr(resolve, f"EXPORT_{export_subtype}", None) if export_subtype else None
    if export_subtype and sub is None:
        return {"ok": False, "error": f"This Resolve has no resolve.EXPORT_{export_subtype}"}
    project = _current_project()
    current = project.GetCurrentTimeline()
    timeline = current
    if timeline_name:
        timelines = [project.GetTimelineByIndex(i) for i in range(1, int(project.GetTimelineCount() or 0) + 1)]
        names = [t.GetName() for t in timelines if t]
        timeline = next((t for t in timelines if t and t.GetName() == timeline_name), None)
        if timeline is None:
            return {"ok": False, "error": f"No timeline named '{timeline_name}'.", "timelines": names}
    if timeline is None:
        return {"ok": False, "error": "No active timeline. Open a timeline on the Edit page first."}
    name = timeline.GetName()
    if out_path:
        out = os.path.expanduser(out_path)
    else:
        ext = next((e for t, _, e in EXPORT_FORMATS.values() if t == export_type), ".txt")
        slug = re.sub(r"[^A-Za-z0-9\-]+", "_", name).strip("_") or "timeline"
        out = os.path.join(os.path.expanduser(out_dir or "."), f"{slug}_{stamp or 'export'}{ext}")
    args = (out, kind, sub) if export_subtype else (out, kind)
    done, ok = _call(timeline, "Export", *args)
    switched = False
    if not (done and ok) and current and name != current.GetName():
        switched = bool(project.SetCurrentTimeline(timeline))
        if switched:
            try:
                done, ok = _call(timeline, "Export", *args)
            finally:
                project.SetCurrentTimeline(current)
    result: dict[str, Any] = {"ok": bool(done and ok), "path": out, "timeline": name,
                              "export_type": export_type, "export_subtype": export_subtype,
                              "switched_timeline": switched}
    if not done:
        result["error"] = str(ok)
    elif not ok:
        result["error"] = f"Resolve's Timeline:Export returned {ok}"
    return result
