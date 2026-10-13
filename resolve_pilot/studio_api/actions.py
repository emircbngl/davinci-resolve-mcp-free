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


# ────────────────────────────────────────────────────────────────────────────
# Live timeline building (twins of snippet_import_media / append_clips / remove_clips)
# ────────────────────────────────────────────────────────────────────────────

def _call(obj, method: str, *args):
    fn = getattr(obj, method, None) if obj is not None else None
    if not callable(fn):
        return None
    try:
        return fn(*args)
    except Exception:
        return None


def _numeric(value) -> float | None:
    """'23.976' / '25' / 25 -> float; a leading number in strings like '29.97 DF'."""
    import re
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"[\d.]+", value) if isinstance(value, str) else None
    try:
        return float(m.group(0)) if m else None
    except ValueError:
        return None


def _clip_path(mpi):
    return _call(mpi, "GetClipProperty", "File Path")


def _same_path(a, b) -> bool:
    if not isinstance(a, str) or not isinstance(b, str) or not a:
        return False
    return a == b or a.replace("\\", "/") == b.replace("\\", "/")


def _find_item(folder, pred, recursive: bool):
    for mpi in _call(folder, "GetClipList") or []:
        if pred(mpi):
            return mpi
    if recursive:
        for sub in _call(folder, "GetSubFolderList") or []:
            hit = _find_item(sub, pred, True)
            if hit is not None:
                return hit
    return None


def _open_bin(mp, path: str):
    folder, created = mp.GetRootFolder(), []
    for name in [p for p in path.split("/") if p]:
        nxt = next((s for s in _call(folder, "GetSubFolderList") or [] if _call(s, "GetName") == name), None)
        if nxt is None:
            nxt = mp.AddSubFolder(folder, name)
            if not nxt:
                return None, f"Resolve could not create the bin '{name}'", created
            created.append(name)
        folder = nxt
    return folder, None, created


def _describe_mpi(mpi) -> dict[str, Any]:
    return {"name": _call(mpi, "GetName"), "media_pool_id": _call(mpi, "GetUniqueId"),
            "path": _clip_path(mpi), "fps": _numeric(_call(mpi, "GetClipProperty", "FPS")),
            "frames": _numeric(_call(mpi, "GetClipProperty", "Frames"))}


def _import_into(mp, path: str, folder):
    hit = _find_item(folder, lambda m: _same_path(_clip_path(m), path), False)
    if hit is not None:
        return hit, True, None
    if folder is not None:
        mp.SetCurrentFolder(folder)
    got = mp.ImportMedia([path])
    if not got:
        return None, False, f"Resolve imported nothing from {path} (unsupported or unreadable file?)"
    return got[0], False, None


def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
    """Lua tables drop nil values; drop None so both channels return the same keys."""
    return {k: v for k, v in d.items() if v is not None}


def import_media(paths: list[str], bin: str | None = None) -> dict[str, Any]:
    """Twin of snippet_import_media."""
    mp = _current_project().GetMediaPool()
    before = mp.GetCurrentFolder()
    folder, created = before, []
    if bin:
        folder, err, created = _open_bin(mp, bin)
        if folder is None:
            return {"ok": False, "error": err, "bins_created": created}
    items, imported, reused, failed = [], 0, 0, 0
    for i, path in enumerate(paths):
        mpi, already, err = _import_into(mp, path, folder)
        row: dict[str, Any] = {"index": i, "path": path, "ok": mpi is not None}
        if mpi is not None:
            row.update(_strip_none(_describe_mpi(mpi)))
            row["path"] = path
            row["pool_path"] = _clip_path(mpi)
            row["already_in_pool"] = already
            row["in_bin"] = _find_item(
                folder, lambda m: _call(m, "GetUniqueId") == row.get("media_pool_id")
                and _same_path(_clip_path(m), row["pool_path"]), False) is not None
            if already:
                reused += 1
            else:
                imported += 1
        else:
            row["error"] = err
            failed += 1
        items.append(_strip_none(row))
    if before is not None and folder is not before:
        mp.SetCurrentFolder(before)
    return _strip_none({"ok": failed == 0, "bin": _call(folder, "GetName"), "bins_created": created,
                        "imported": imported, "reused": reused, "failed": failed, "items": items})


def _describe_item(it, start: int, fps: float) -> dict[str, Any]:
    s, e = it.GetStart(), it.GetEnd()
    out = {"name": _call(it, "GetName"), "start_frame": s, "end_frame": e, "duration_frames": e - s,
           "start_seconds": (s - start) / fps, "end_seconds": (e - start) / fps,
           "duration_seconds": (e - s) / fps, "source_in_frame": _call(it, "GetLeftOffset")}
    tt = _call(it, "GetTrackTypeAndIndex")
    if isinstance(tt, (list, tuple)) and tt and tt[0]:
        out.update(track_type=tt[0], track_index=tt[1], track=f"{tt[0]} {tt[1]}")
    return _strip_none(out)


def append_clips(clips: list[dict], timeline: str | None = None, new_timeline: str | None = None,
                 new_timeline_fps: float | None = None) -> dict[str, Any]:
    """Twin of snippet_append_clips (clips already normalized by normalize_clips)."""
    project = _current_project()
    mp = project.GetMediaPool()

    def find_timeline(name):
        for i in range(1, int(project.GetTimelineCount() or 0) + 1):
            t = project.GetTimelineByIndex(i)
            if t and t.GetName() == name:
                return t
        return None

    created, fps_applied = False, None
    if new_timeline:
        if find_timeline(new_timeline):
            return {"ok": False, "error": f"A timeline named '{new_timeline}' already exists. Pick another "
                    f"name, or append to it with timeline='{new_timeline}'."}
        tl = mp.CreateEmptyTimeline(new_timeline)
        if not tl:
            return {"ok": False, "error": f"Resolve did not create the timeline '{new_timeline}' "
                    "(CreateEmptyTimeline returned nil)."}
        created = True
        if new_timeline_fps:
            _call(tl, "SetSetting", "useCustomSettings", "1")
            fps_applied = bool(_call(tl, "SetSetting", "timelineFrameRate", str(new_timeline_fps)))
    elif timeline:
        tl = find_timeline(timeline)
        if tl is None:
            return {"ok": False, "error": f"No timeline named '{timeline}' in this project."}
    else:
        tl = project.GetCurrentTimeline()
        if tl is None:
            return {"ok": False, "error": "No active timeline. Pass timeline or new_timeline."}
    project.SetCurrentTimeline(tl)
    cur = project.GetCurrentTimeline()
    if cur is None or cur.GetName() != tl.GetName():
        return {"ok": False, "error": f"Resolve did not switch to timeline '{tl.GetName()}'; nothing was appended."}
    fps = _timeline_fps(project, tl)
    start = tl.GetStartFrame()
    root = mp.GetRootFolder()
    results, appended, failed, tracks_added = [], 0, 0, []

    def ensure_track(kind: str, index: int) -> bool:
        n = int(tl.GetTrackCount(kind) or 0)
        while n < index:
            if not tl.AddTrack(kind):
                return False
            m = int(tl.GetTrackCount(kind) or 0)
            if m <= n:
                return False
            n = m
            tracks_added.append(f"{kind} {n}")
        return True

    for c in clips:
        row: dict[str, Any] = {"index": c["index"], "ok": False}
        warnings: list[str] = []
        err = None
        if c.get("media_pool_id"):
            mpi = _find_item(root, lambda m: _call(m, "GetUniqueId") == c["media_pool_id"], True)
            if mpi is None:
                err = f"No media pool item with id {c['media_pool_id']}"
        else:
            mpi = _find_item(root, lambda m: _same_path(_clip_path(m), c["path"]), True)
            if mpi is None:
                mpi, already, err = _import_into(mp, c["path"], mp.GetCurrentFolder())
                row["imported"] = mpi is not None and not already
        if mpi is not None:
            src = _strip_none(_describe_mpi(mpi))
            row["source"] = src
            sfps = src.get("fps")
            if not sfps or sfps <= 0:
                sfps = fps
                warnings.append(f"Resolve reported no FPS for the source; used the timeline fps {fps}")
            s = int(c["in_seconds"] * sfps + 0.5)
            frames = src.get("frames")
            en = int(c["out_seconds"] * sfps + 0.5) if c.get("out_seconds") is not None else frames
            if en is None:
                err = "Resolve did not report the clip length; give out_seconds or duration_seconds"
            elif frames and en > frames:
                warnings.append(f"out point {int(en)} is past the end of the source ({int(frames)} frames); used the end")
                en = frames
            if err is None and en <= s:
                err = f"empty range: source frames {s}-{int(en)}"
            kinds = {"video": ["video"], "audio": ["audio"]}.get(c["media"], ["video", "audio"])
            if err is None and c.get("track"):
                for k in kinds:
                    if not ensure_track(k, c["track"]):
                        err = f"Resolve could not add {k} track {c['track']}"
            if err is None:
                en = int(en)
                info: dict[str, Any] = {"mediaPoolItem": mpi, "startFrame": s, "endFrame": en}
                if c["media"] == "video":
                    info["mediaType"] = 1
                elif c["media"] == "audio":
                    info["mediaType"] = 2
                if c.get("track"):
                    info["trackIndex"] = c["track"]
                if c.get("record_seconds") is not None:
                    info["recordFrame"] = start + int(c["record_seconds"] * fps + 0.5)
                expected = int((en - s) / sfps * fps + 0.5)
                row["requested"] = _strip_none({
                    "source_start_frame": s, "source_end_frame": en, "source_fps": sfps,
                    "track": c.get("track"), "media": c["media"], "record_frame": info.get("recordFrame"),
                    "expected_duration_frames": expected})
                got = mp.AppendToTimeline([info])
                if not got:
                    err = f"Resolve appended nothing (AppendToTimeline returned {got!r})"
                else:
                    row["items"] = []
                    for it in got:
                        d = _describe_item(it, start, fps)
                        row["items"].append(d)
                        where = d.get("track", "item")
                        if d["duration_frames"] != expected:
                            warnings.append(f"{where} landed with {d['duration_frames']} frames, expected {expected}")
                        if "recordFrame" in info and d["start_frame"] != info["recordFrame"]:
                            warnings.append(f"{where} starts at frame {d['start_frame']}, asked for {info['recordFrame']}")
                        if c.get("track") and d.get("track_index") and d["track_index"] != c["track"]:
                            warnings.append(f"{where} landed on track {d['track_index']}, asked for {c['track']}")
                    row["ok"] = True
        if err:
            row["error"] = err
        if warnings:
            row["warnings"] = warnings
        appended += row["ok"]
        failed += not row["ok"]
        results.append(row)
    end = tl.GetEndFrame()
    info_tl: dict[str, Any] = {"name": tl.GetName(), "created": created, "fps": fps, "start_frame": start,
                               "end_frame": end, "duration_seconds": (end - start) / fps,
                               "video_tracks": tl.GetTrackCount("video"), "audio_tracks": tl.GetTrackCount("audio")}
    if new_timeline_fps:
        info_tl.update(requested_fps=new_timeline_fps, fps_applied=fps_applied)
    return {"ok": failed == 0, "timeline": info_tl, "appended": appended, "failed": failed,
            "tracks_added": tracks_added, "clips": results}


def remove_clips(indices: list[int], track_type: str = "video", track_index: int = 1,
                 names: list[str] | None = None, ripple: bool = False) -> dict[str, Any]:
    """Twin of snippet_remove_clips."""
    resolve = get_resolve()
    project = _current_project()
    tl = _current_timeline()
    fps = _timeline_fps(project, tl)
    start = tl.GetStartFrame()
    items = clip_items(tl, track_type, track_index)
    targets, removed = [], []
    for n, i in enumerate(indices):
        if i >= len(items):
            return {"ok": False, "error": f"{track_type} {track_index} has {len(items)} clips (indices "
                    f"0-{len(items) - 1}); index {i} does not exist. Nothing was deleted."}
        it = items[i]
        if names is not None and names[n] != it.GetName():
            return {"ok": False, "error": f"clip {i} is '{it.GetName()}', not '{names[n]}'; the timeline "
                    "changed. Read it again with get_resolve_state. Nothing was deleted."}
        targets.append(it)
        removed.append({"index": i, "name": it.GetName(), "start_seconds": (it.GetStart() - start) / fps,
                        "end_seconds": (it.GetEnd() - start) / fps})
    page = _call(resolve, "GetCurrentPage")
    if page != "edit":
        resolve.OpenPage("edit")
    try:
        ok = tl.DeleteClips(targets, bool(ripple))
    finally:
        if page and page != "edit":
            resolve.OpenPage(page)
    after = clip_items(tl, track_type, track_index)
    deleted = len(items) - len(after)
    out = {"ok": bool(ok) and deleted == len(targets), "deleted": deleted, "requested": len(targets),
           "removed": removed, "ripple": bool(ripple), "track": f"{track_type} {track_index}",
           "clips_before": len(items), "clips_after": len(after), "timeline": tl.GetName(),
           "timeline_duration_seconds": (tl.GetEndFrame() - start) / fps}
    if not out["ok"]:
        out["error"] = (f"DeleteClips returned {ok} and {deleted} of {len(targets)} clips are gone. "
                        "Check the timeline with get_resolve_state.")
    return out


def add_subtitles(srt_path: str, replace: bool = False) -> dict[str, Any]:
    """Studio twin of snippet_add_subtitles (same result shape and the same crash guard)."""
    resolve = get_resolve()
    project = _current_project()
    tl = _current_timeline()
    fps = _timeline_fps(project, tl)
    mp = project.GetMediaPool()
    item = None

    def scan(folder) -> None:
        nonlocal item
        for c in folder.GetClipList() or []:
            if item is None and c.GetClipProperty("File Path") == srt_path:
                item = c
        for f in folder.GetSubFolderList() or []:
            if item is None:
                scan(f)

    scan(mp.GetRootFolder())
    imported = False
    if item is None:
        got = mp.ImportMedia([srt_path]) or []
        item = got[0] if got else None
        imported = item is not None
    if item is None:
        return {"ok": False, "error": f"Resolve could not import {srt_path}"}
    kind = item.GetClipProperty("Type")
    if kind != "Subtitle":
        return {"ok": False, "error": f"Resolve did not read {srt_path} as subtitles (Type: {kind})"}
    if not tl.GetTrackCount("subtitle"):
        tl.AddTrack("subtitle")
    existing = tl.GetItemListInTrack("subtitle", 1) or []
    removed = 0
    if existing:
        if not replace:
            return {"ok": False, "error": f"Subtitle track 1 already has {len(existing)} captions. "
                    "Pass replace=true to swap them for this file."}
        page = resolve.GetCurrentPage()
        if page != "edit":
            resolve.OpenPage("edit")
        tl.DeleteClips(existing, False)
        if page and page != "edit":
            resolve.OpenPage(page)
        removed = len(existing) - len(tl.GetItemListInTrack("subtitle", 1) or [])
    # Subtitles land at the playhead: park it on the first frame, then put it back.
    # Never pass frame ranges for a subtitle item: that crashes Resolve 20.3.
    playhead = tl.GetCurrentTimecode()
    tl.SetCurrentTimecode(tl.GetStartTimecode())
    mp.AppendToTimeline([{"mediaPoolItem": item}])
    if playhead:
        tl.SetCurrentTimecode(playhead)
    start = tl.GetStartFrame()
    cues = [{"start_seconds": (it.GetStart() - start) / fps, "end_seconds": (it.GetEnd() - start) / fps,
             "text": it.GetName()} for it in tl.GetItemListInTrack("subtitle", 1) or []]
    return {"ok": bool(cues), "timeline": tl.GetName(), "fps": fps, "imported": imported,
            "media_pool_item": item.GetName(), "removed": removed, "cues": cues}


def _pcall(obj, method: str, *args) -> tuple[bool, Any]:
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
            done, said = _pcall(it, *setter)
            read, got = _pcall(it, *getter)
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
        done, n = _pcall(tl, "GetTrackCount", kind)
        try:
            return int(n) if done and n is not None else 0
        except (TypeError, ValueError):
            return 0

    added, all_ok = [], True
    for a in adds:
        before = count(a["track_type"])
        done, said = _pcall(tl, "AddTrack", a["track_type"], *([a["sub_type"]] if a.get("sub_type") else []))
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
                done, said = _pcall(tl, setter, c["track_type"], c["index"], c[key])
                read, got = _pcall(tl, getter, c["track_type"], c["index"])
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
    done, ok = _pcall(timeline, "Export", *args)
    switched = False
    if not (done and ok) and current and name != current.GetName():
        switched = bool(project.SetCurrentTimeline(timeline))
        if switched:
            try:
                done, ok = _pcall(timeline, "Export", *args)
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
