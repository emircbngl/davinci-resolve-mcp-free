"""Is the timeline Resolve built the one the plan describes?

After an import the clips are read back from Resolve and compared with the plan, on the
frame positions the exporter wrote (`fcpxml.layout`). A plan whose frame rate Resolve
rounded came in with a one-frame hole after one clip of 198; nothing reported it.
"""
from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from resolve_pilot.editorial.roughcut import RoughCutPlan, plan_to_timeline_spec
from resolve_pilot.exporters.fcpxml import _FCPXML_ROTATION, _shown_size, layout, turn_scale

_LISTED = 10  # differences shown per kind; the counts are always complete


def _same_file(a: str, b: str) -> bool:
    def key(p: str) -> str:   # the file system may hand back another Unicode form of the same name
        return unicodedata.normalize("NFC", str(Path(p).expanduser().resolve()))
    return bool(a) and key(a) == key(b)


def check(plan: RoughCutPlan, rows: list[list[Any]] | None, timeline_fps: float | None) -> dict[str, Any]:
    """Compare the clips on video track 1 with the plan.

    ``rows``: per clip ``[start, end, source_in, file path, RotationAngle, ZoomX, ZoomY]``,
    frames counted from the timeline start. ``ok`` is true only when every clip is on the
    plan's frames, plays the plan's file from the plan's in-point and is turned as planned.
    """
    rows = rows or []
    out: dict[str, Any] = {"ok": False, "clips_in_plan": len(plan.clips), "clips_on_timeline": len(rows)}
    fps = float(timeline_fps or plan.fps)
    if abs(fps - plan.fps) > 0.01:
        out["frame_rate"] = {"plan": plan.fps, "timeline": fps}
    if len(rows) != len(plan.clips):
        out["note"] = "The clip counts differ, so the clips were not compared one by one."
        return out
    bounds = layout(plan_to_timeline_spec(plan)).bounds
    frame = (plan.width, plan.height)
    off: dict[str, list[dict[str, Any]]] = {"positions_off": [], "sources_off": [], "turns_off": []}
    for i, (clip, row) in enumerate(zip(plan.clips, rows)):
        start, end, source_in, path, rotation, zoom_x, zoom_y = row
        if (start, end) != (bounds[i], bounds[i + 1]):
            off["positions_off"].append({"clip": i, "timeline": [start, end], "plan": [bounds[i], bounds[i + 1]]})
        in_seconds = source_in / fps
        if not _same_file(path, clip.source_path) or abs(in_seconds - clip.in_seconds) > 1.0 / fps + 1e-6:
            off["sources_off"].append({"clip": i, "timeline": [Path(path).name, round(in_seconds, 3)],
                                       "plan": [Path(clip.source_path).name, round(clip.in_seconds, 3)]})
        turn = plan.source_turns.get(clip.source_path, 0) % 360
        want_rotation = _FCPXML_ROTATION.get(turn, 0)
        want_zoom = turn_scale(_shown_size(Path(clip.source_path).expanduser()), frame, turn) if turn else 1.0
        if abs(rotation - want_rotation) > 0.01 or max(abs(zoom_x - want_zoom), abs(zoom_y - want_zoom)) > 0.001:
            off["turns_off"].append({"clip": i, "timeline": [rotation, round(zoom_x, 4), round(zoom_y, 4)],
                                     "plan": [want_rotation, round(want_zoom, 4)]})
    for kind, found in off.items():
        if found:
            out[kind] = found[:_LISTED]
            out[kind + "_count"] = len(found)
    out["ok"] = not any(off.values()) and "frame_rate" not in out
    return out
