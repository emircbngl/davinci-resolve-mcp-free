"""Curated Lua snippets for the DaVinci Resolve Console.

All snippets:
- Use only Resolve's built-in globals (resolve, fusion, bmd, app).
- Print a clear status line so the user can see they worked.
- ``return`` a table describing what happened, so the same code gives the
  agent structured results when it runs through the live bridge.
- Embed every caller-supplied value through :func:`to_lua`, so paths or names
  containing quotes, brackets or backslashes cannot break (or inject into) the Lua.
- Work in Free edition (Console is available) and Studio.

Every ``snippet_<name>`` function below is registered automatically; its
signature is what ``list_snippets`` advertises and ``render_snippet`` validates.
"""
from __future__ import annotations

import inspect
import math
import os
import re
from pathlib import Path
from typing import Any, Callable

# ────────────────────────────────────────────────────────────────────────────
# Python → Lua value encoding
# ────────────────────────────────────────────────────────────────────────────

_LUA_KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "goto",
    "if", "in", "local", "nil", "not", "or", "repeat", "return", "then", "true",
    "until", "while",
}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def lua_string(s: str) -> str:
    """Quote ``s`` as a Lua string literal (UTF-8 passes through untouched)."""
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 32 or o == 127:
            out.append(f"\\{o:03d}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def to_lua(value: Any) -> str:
    """Encode a JSON-like Python value as a Lua expression."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Cannot encode non-finite number {value!r} for Lua")
        return repr(value)
    if isinstance(value, (str, Path)):
        return lua_string(str(value))
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(to_lua(v) for v in value) + "}"
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            if v is None:
                continue
            key = str(k)
            if _IDENT_RE.match(key) and key not in _LUA_KEYWORDS:
                parts.append(f"{key} = {to_lua(v)}")
            else:
                parts.append(f"[{lua_string(key)}] = {to_lua(v)}")
        return "{" + ", ".join(parts) + "}"
    raise TypeError(f"Cannot encode {type(value).__name__} for Lua")


# ────────────────────────────────────────────────────────────────────────────
# Colors / names Resolve accepts
# ────────────────────────────────────────────────────────────────────────────

MARKER_COLORS = (
    "Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia",
    "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
)
CLIP_COLORS = (
    "Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy",
    "Blue", "Purple", "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate",
)
PAGES = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")

# Kept for backwards compatibility with code that imported the old set.
_MARKER_COLORS = {c.lower() for c in MARKER_COLORS}


def _pick(value: str, allowed: tuple[str, ...], what: str) -> str:
    v = value.strip().lower()
    for a in allowed:
        if a.lower() == v:
            return a
    raise ValueError(f"Unknown {what}: {value!r}. Use one of: {', '.join(allowed)}")


def _validate_color(color: str) -> str:
    """Normalize a marker color name (case-insensitive)."""
    return _pick(color, MARKER_COLORS, "marker color")


def validate_clip_color(color: str) -> str:
    return _pick(color, CLIP_COLORS, "clip color")


# ────────────────────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────────────────────

_SNIPPETS: dict[str, Callable[..., str]] = {}


def _snippet(fn: Callable[..., str]) -> Callable[..., str]:
    assert fn.__name__.startswith("snippet_")
    _SNIPPETS[fn.__name__[len("snippet_"):]] = fn
    return fn


_TIMELINE_PREAMBLE = """\
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project and project:GetCurrentTimeline()
if not timeline then
  print("[resolve-pilot] No active timeline - open one on the Edit page first.")
  return {ok = false, error = "No active timeline. Open a timeline on the Edit page first."}
end
"""

_PROJECT_PREAMBLE = """\
local project = resolve:GetProjectManager():GetCurrentProject()
if not project then
  print("[resolve-pilot] No project is open.")
  return {ok = false, error = "No project is open in Resolve."}
end
"""

# Timeline fps as a number. Timeline:GetSetting returns "" when the timeline
# follows the project settings, hence the fallback chain.
_TIMELINE_FPS = (
    'local fps = tonumber(timeline:GetSetting("timelineFrameRate"))'
    ' or tonumber(project:GetSetting("timelineFrameRate")) or 24\n'
)


# ────────────────────────────────────────────────────────────────────────────
# Snippets
# ────────────────────────────────────────────────────────────────────────────

def normalize_markers(markers: list[dict]) -> list[dict]:
    """Validate marker dicts and fill defaults; shared by the Lua and Studio paths."""
    if not markers:
        raise ValueError("markers is empty")
    rows = []
    for i, m in enumerate(markers):
        if m.get("frame") is None and m.get("seconds") is None:
            raise ValueError(f"marker #{i} needs 'frame' or 'seconds'")
        row: dict[str, Any] = {
            "color": _validate_color(m.get("color") or "Blue"),
            "name": str(m.get("name") or f"Marker {i + 1}"),
            "note": str(m.get("note") or ""),
            "duration": max(1, int(m.get("duration") or 1)),
        }
        if m.get("frame") is not None:
            row["frame"] = int(m["frame"])
        else:
            row["seconds"] = float(m["seconds"])
        rows.append(row)
    return rows


@_snippet
def snippet_add_markers(markers: list[dict]) -> str:
    """Add markers to the current timeline; positions in frames or seconds.

    markers: list of {frame | seconds, color="Blue", name="", note="", duration=1}.
    `frame` is an offset from the timeline start; `seconds` is converted inside
    Resolve using the timeline's real frame rate. `duration` is in frames.
    """
    rows = normalize_markers(markers)
    body = ",\n  ".join(to_lua(r) for r in rows)
    return f"""-- resolve-pilot: add_markers
{_TIMELINE_PREAMBLE}{_TIMELINE_FPS}local markers = {{
  {body}
}}
local added, skipped = 0, {{}}
for _, m in ipairs(markers) do
  local frame = m.frame or math.floor(m.seconds * fps + 0.5)
  if timeline:AddMarker(frame, m.color, m.name, m.note, m.duration, "") then
    added = added + 1
  else
    skipped[#skipped + 1] = frame
  end
end
print(string.format("[resolve-pilot] Added %d / %d markers on '%s'", added, #markers, timeline:GetName()))
if #skipped > 0 then
  print("[resolve-pilot] Skipped frames (marker already there or out of range): " .. table.concat(skipped, ", "))
end
return {{ok = true, added = added, requested = #markers, skipped_frames = skipped,
  timeline = timeline:GetName(), timeline_fps = fps}}
"""


@_snippet
def snippet_batch_markers(
    frames: list[int],
    color: str = "Blue",
    name_prefix: str = "Cue",
    note: str = "",
) -> str:
    """Add same-colored markers at a list of frame offsets on the current timeline."""
    return snippet_add_markers([
        {"frame": int(f), "color": color, "name": f"{name_prefix} {i}", "note": note}
        for i, f in enumerate(frames, start=1)
    ])


@_snippet
def snippet_get_state(
    include_clips: bool = False,
    track_type: str = "video",
    track_index: int = 1,
    include_markers: bool = False,
    max_clips: int = 200,
) -> str:
    """Return project / timeline state as a table (pretty-printed in the Console)."""
    track_type = _pick(track_type, ("video", "audio", "subtitle"), "track type")
    return f"""-- resolve-pilot: get_state
local include_clips, track_type, track_index = {to_lua(bool(include_clips))}, {to_lua(track_type)}, {int(track_index)}
local include_markers, max_clips = {to_lua(bool(include_markers))}, {int(max_clips)}
local function try(f, ...)
  if type(f) ~= "function" then return nil end
  local ok, v = pcall(f, ...)
  if ok then return v end
  return nil
end
local r = resolve
local state = {{ok = true, product = try(r.GetProductName, r), version = try(r.GetVersionString, r),
  page = try(r.GetCurrentPage, r)}}
local project = r:GetProjectManager():GetCurrentProject()
if not project then
  print("[resolve-pilot] No project open.")
  state.project = "(none)"
  return state
end
state.project = project:GetName()
state.project_fps = tonumber(project:GetSetting("timelineFrameRate"))
state.resolution = {{tonumber(project:GetSetting("timelineResolutionWidth")),
  tonumber(project:GetSetting("timelineResolutionHeight"))}}
state.timelines = {{}}
for i = 1, (project:GetTimelineCount() or 0) do
  local t = project:GetTimelineByIndex(i)
  if t then state.timelines[#state.timelines + 1] = t:GetName() end
end
local tl = project:GetCurrentTimeline()
if tl then
  local fps = tonumber(try(tl.GetSetting, tl, "timelineFrameRate")) or state.project_fps or 24
  local start_frame = tl:GetStartFrame()
  local markers = try(tl.GetMarkers, tl) or {{}}
  local marker_count = 0
  for _ in pairs(markers) do marker_count = marker_count + 1 end
  local cur = {{name = tl:GetName(), fps = fps, start_frame = start_frame, end_frame = tl:GetEndFrame(),
    duration_seconds = (tl:GetEndFrame() - start_frame) / fps,
    start_timecode = try(tl.GetStartTimecode, tl), playhead_timecode = try(tl.GetCurrentTimecode, tl),
    video_tracks = tl:GetTrackCount("video"), audio_tracks = tl:GetTrackCount("audio"),
    subtitle_tracks = try(tl.GetTrackCount, tl, "subtitle"), marker_count = marker_count}}
  if include_markers then
    local list = {{}}
    for frame, m in pairs(markers) do
      list[#list + 1] = {{frame = frame, seconds = frame / fps, color = m.color, name = m.name,
        note = m.note, duration = m.duration}}
    end
    table.sort(list, function(a, b) return a.frame < b.frame end)
    cur.markers = list
  end
  if include_clips then
    local items = tl:GetItemListInTrack(track_type, track_index) or {{}}
    local clips = {{}}
    for i, it in ipairs(items) do
      if i > max_clips then break end
      local mpi = try(it.GetMediaPoolItem, it)
      clips[#clips + 1] = {{index = i - 1, name = it:GetName(),
        start_seconds = (it:GetStart() - start_frame) / fps, duration_seconds = it:GetDuration() / fps,
        start_frame = it:GetStart(), end_frame = it:GetEnd(), source_in_frame = try(it.GetLeftOffset, it),
        clip_color = try(it.GetClipColor, it),
        source_path = mpi and try(mpi.GetClipProperty, mpi, "File Path") or nil}}
    end
    cur.track = track_type .. " " .. track_index
    cur.clip_count = #items
    cur.clips = clips
  end
  state.current_timeline = cur
end
print(string.format("[resolve-pilot] Project '%s', %d timeline(s), current: %s", state.project,
  #state.timelines, tl and tl:GetName() or "(none)"))
return state
"""


@_snippet
def snippet_import_timeline(
    path: str,
    timeline_name: str | None = None,
    import_source_clips: bool = True,
) -> str:
    """Import an FCPXML / EDL / AAF / DRT / OTIO file as a new timeline and open it."""
    opts: dict[str, Any] = {"importSourceClips": bool(import_source_clips)}
    if timeline_name:
        opts["timelineName"] = timeline_name
    path = os.path.expanduser(path)
    return f"""-- resolve-pilot: import_timeline
{_PROJECT_PREAMBLE}local path = {to_lua(path)}
local tl = project:GetMediaPool():ImportTimelineFromFile(path, {to_lua(opts)})
if not tl then
  print("[resolve-pilot] Import failed: " .. path)
  return {{ok = false, error = "Resolve could not import " .. path .. ". Check the file exists, is valid, " ..
    "and that no timeline with the same name already exists."}}
end
project:SetCurrentTimeline(tl)
local n = 0
for t = 1, tl:GetTrackCount("video") do n = n + #(tl:GetItemListInTrack("video", t) or {{}}) end
print(string.format("[resolve-pilot] Imported '%s' (%d video clips)", tl:GetName(), n))
return {{ok = true, timeline = tl:GetName(), video_clip_count = n,
  start_frame = tl:GetStartFrame(), end_frame = tl:GetEndFrame()}}
"""


EXPORT_SUBTYPES = ("NONE", "AAF_NEW", "AAF_EXISTING", "CDL", "SDL", "MISSING_CLIPS")


@_snippet
def snippet_export_timeline(
    out_path: str,
    export_type: str = "FCPXML_1_10",
    export_subtype: str | None = None,
) -> str:
    """Export the current timeline (FCPXML_1_10, FCPXML_1_9, AAF, EDL, OTIO, DRT, ...).

    export_type is the suffix of Resolve's `resolve.EXPORT_*` constant. AAF and EDL
    also need export_subtype (AAF: AAF_NEW / AAF_EXISTING; EDL: NONE / CDL / SDL /
    MISSING_CLIPS); it defaults to AAF_NEW and NONE.
    """
    if not re.fullmatch(r"[A-Z0-9_]+", export_type):
        raise ValueError(f"export_type must look like FCPXML_1_10, got {export_type!r}")
    if export_subtype is None:
        export_subtype = {"AAF": "AAF_NEW", "EDL": "NONE"}.get(export_type)
    if export_subtype is not None and export_subtype not in EXPORT_SUBTYPES:
        raise ValueError(f"export_subtype must be one of {', '.join(EXPORT_SUBTYPES)}")
    out_path = os.path.expanduser(out_path)
    sub = f", resolve.EXPORT_{export_subtype}" if export_subtype else ""
    return f"""-- resolve-pilot: export_timeline
{_TIMELINE_PREAMBLE}if resolve.EXPORT_{export_type} == nil then
  return {{ok = false, error = "Unknown export_type {export_type} in this Resolve version"}}
end
local out = {to_lua(out_path)}
local ok = timeline:Export(out, resolve.EXPORT_{export_type}{sub})
print(string.format("[resolve-pilot] Export %s -> %s", tostring(ok), out))
return {{ok = ok and true or false, path = out}}
"""


@_snippet
def snippet_set_clip_color(clip_color: str = "Orange", track: int = 1) -> str:
    """Color-code every clip on a video track. Useful for sectioning a long timeline."""
    c = validate_clip_color(clip_color)
    return f"""-- resolve-pilot: set_clip_color
{_TIMELINE_PREAMBLE}local items = timeline:GetItemListInTrack("video", {int(track)})
local n = 0
for _, item in ipairs(items or {{}}) do
  if item:SetClipColor({to_lua(c)}) then n = n + 1 end
end
print(string.format("[resolve-pilot] Colored %d clips on V%d", n, {int(track)}))
return {{ok = true, colored = n, track = {int(track)}, color = {to_lua(c)}}}
"""


@_snippet
def snippet_create_compound_from_selection(name: str = "Compound") -> str:
    """Wrap the timeline's current video item into a compound clip."""
    return f"""-- resolve-pilot: create_compound
{_TIMELINE_PREAMBLE}local items = timeline:GetCurrentVideoItem() and {{ timeline:GetCurrentVideoItem() }} or {{}}
local ok = timeline:CreateCompoundClip(items, {{startTimecode = "01:00:00:00", name = {to_lua(name)}}})
print("[resolve-pilot] Compound created: " .. tostring(ok))
return {{ok = ok and true or false}}
"""


@_snippet
def snippet_render_current_timeline(
    preset: str = "H.264 Master",
    target_dir: str = "~/Desktop/Renders",
    custom_name: str | None = None,
    start: bool = False,
) -> str:
    """Queue the current timeline for render with a preset; optionally start rendering."""
    settings: dict[str, Any] = {"TargetDir": os.path.expanduser(target_dir)}
    if custom_name:
        settings["CustomName"] = custom_name
    return f"""-- resolve-pilot: render_current_timeline
{_TIMELINE_PREAMBLE}local preset = {to_lua(preset)}
if not project:LoadRenderPreset(preset) then
  print("[resolve-pilot] Unknown render preset: " .. preset)
  return {{ok = false, error = "Unknown render preset: " .. preset,
    available_presets = project:GetRenderPresetList()}}
end
project:SetRenderSettings({to_lua(settings)})
local job = project:AddRenderJob()
if not job then
  return {{ok = false, error = "AddRenderJob failed. Check the Deliver page (the Free edition limits some formats)."}}
end
local start = {to_lua(bool(start))}
if start then project:StartRendering(job) end
print(string.format("[resolve-pilot] Queued job %s for '%s'%s", tostring(job), timeline:GetName(),
  start and " and started rendering" or ""))
return {{ok = true, job_id = job, timeline = timeline:GetName(), preset = preset, rendering = start}}
"""


@_snippet
def snippet_render_all_timelines(preset: str = "H.264 Master", out_dir: str = "~/Desktop/Renders") -> str:
    """Queue every timeline in the project for render with the given preset, then start."""
    out_dir = os.path.expanduser(out_dir)
    return f"""-- resolve-pilot: render_all_timelines
{_PROJECT_PREAMBLE}local n_tl = project:GetTimelineCount()
local queued = 0
for i = 1, n_tl do
  local tl = project:GetTimelineByIndex(i)
  project:SetCurrentTimeline(tl)
  project:LoadRenderPreset({to_lua(preset)})
  project:SetRenderSettings({{TargetDir = {to_lua(out_dir)}, CustomName = tl:GetName()}})
  local jid = project:AddRenderJob()
  if jid then queued = queued + 1 end
end
print(string.format("[resolve-pilot] Queued %d render jobs.", queued))
project:StartRendering()
return {{ok = true, queued = queued}}
"""


@_snippet
def snippet_dump_project_info() -> str:
    """Print a readable summary of the open project: name, FPS, resolution, timelines."""
    return """-- resolve-pilot: dump_project_info
local pm = resolve:GetProjectManager()
local p = pm:GetCurrentProject()
if not p then print("[resolve-pilot] No project open."); return end
print("Project: " .. p:GetName())
print("Timeline count: " .. p:GetTimelineCount())
print("FPS: " .. tostring(p:GetSetting("timelineFrameRate")))
print("Resolution: " .. tostring(p:GetSetting("timelineResolutionWidth")) .. "x" .. tostring(p:GetSetting("timelineResolutionHeight")))
local tl = p:GetCurrentTimeline()
if tl then
  print("Current timeline: " .. tl:GetName())
  print("  Video tracks: " .. tl:GetTrackCount("video"))
  print("  Audio tracks: " .. tl:GetTrackCount("audio"))
  print("  Duration: " .. tl:GetEndFrame() - tl:GetStartFrame() .. " frames")
end
"""


@_snippet
def snippet_apply_lut_to_clips(lut_path: str, track: int = 1, node_index: int = 1) -> str:
    """Apply a LUT to all clips on a video track at a specific color node index."""
    lut_path = os.path.expanduser(lut_path)
    return f"""-- resolve-pilot: apply_lut
{_TIMELINE_PREAMBLE}local items = timeline:GetItemListInTrack("video", {int(track)}) or {{}}
local applied = 0
for _, item in ipairs(items) do
  if item:SetLUT({int(node_index)}, {to_lua(lut_path)}) then applied = applied + 1 end
end
print(string.format("[resolve-pilot] Applied LUT to %d / %d clips at node %d", applied, #items, {int(node_index)}))
return {{ok = true, applied = applied, total = #items, node_index = {int(node_index)}}}
"""


@_snippet
def snippet_relink_media(folder: str) -> str:
    """Relink offline media in the current project by pointing Resolve at a folder.

    Walks the media pool recursively and re-points every clip at
    `<folder>/<bin-relative-path>/<clip-name>`. Assumes the filesystem layout
    under `folder` mirrors the bin/sub-bin structure.
    """
    folder = os.path.expanduser(folder)
    return f"""-- resolve-pilot: relink_media
{_PROJECT_PREAMBLE}local mp = project:GetMediaPool()
local root_path = {to_lua(folder)}
local relinked, attempted = 0, 0
local function walk(bin, rel)
  for _, clip in ipairs(bin:GetClipList() or {{}}) do
    if clip:GetClipProperty("Type") ~= "Folder" then
      attempted = attempted + 1
      local target = root_path .. rel .. "/" .. clip:GetName()
      if clip:ReplaceClip(target) then relinked = relinked + 1 end
    end
  end
  for _, sub in ipairs(bin:GetSubFolderList() or {{}}) do
    walk(sub, rel .. "/" .. sub:GetName())
  end
end
walk(mp:GetRootFolder(), "")
print(string.format("[resolve-pilot] Relinked %d / %d clips from %s",
  relinked, attempted, root_path))
return {{ok = true, relinked = relinked, attempted = attempted}}
"""


@_snippet
def snippet_proxy_generate(track: int = 1) -> str:
    """Touch media-pool items on a video track for proxy management."""
    return f"""-- resolve-pilot: proxy_generate
{_TIMELINE_PREAMBLE}local items = timeline:GetItemListInTrack("video", {int(track)})
local q = 0
for _, item in ipairs(items or {{}}) do
  local pi = item:GetMediaPoolItem()
  if pi then pi:LinkProxyMedia(""); q = q + 1 end
end
print(string.format("[resolve-pilot] Touched %d media-pool items for proxy management on V%d", q, {int(track)}))
return {{ok = true, touched = q}}
"""


@_snippet
def snippet_grade_from_drx(drx_path: str, track: int = 1, mode: int = 0) -> str:
    """Apply a saved grade (.drx) to all clips on a video track.

    mode: 0 = no keyframes, 1 = source timecode aligned, 2 = start frames aligned.
    """
    if int(mode) not in (0, 1, 2):
        raise ValueError("mode must be 0 (no keyframes), 1 (source timecode aligned) "
                         "or 2 (start frames aligned)")
    drx_path = os.path.expanduser(drx_path)
    return f"""-- resolve-pilot: grade_from_drx
{_TIMELINE_PREAMBLE}local items = timeline:GetItemListInTrack("video", {int(track)})
local ok = timeline:ApplyGradeFromDRX({to_lua(drx_path)}, {int(mode)}, items)
print("[resolve-pilot] Grade-from-DRX applied: " .. tostring(ok))
return {{ok = ok and true or false}}
"""


@_snippet
def snippet_open_page(page: str) -> str:
    """Switch Resolve to a page: media, cut, edit, fusion, color, fairlight, deliver."""
    p = _pick(page, PAGES, "page")
    return f"""-- resolve-pilot: open_page
local ok = resolve:OpenPage({to_lua(p)})
print("[resolve-pilot] OpenPage {p}: " .. tostring(ok))
return {{ok = ok and true or false, page = {to_lua(p)}}}
"""


# ────────────────────────────────────────────────────────────────────────────
# Introspection
# ────────────────────────────────────────────────────────────────────────────

def _describe(fn: Callable[..., str]) -> str:
    doc = inspect.getdoc(fn) or ""
    return doc.split("\n", 1)[0].strip()


def list_snippets() -> list[dict]:
    """Return name, description and parameters of every snippet."""
    out = []
    for name, fn in _SNIPPETS.items():
        params = []
        for p in inspect.signature(fn).parameters.values():
            entry: dict[str, Any] = {"name": p.name, "required": p.default is inspect.Parameter.empty}
            if p.default is not inspect.Parameter.empty:
                entry["default"] = p.default
            params.append(entry)
        out.append({"name": name, "description": _describe(fn), "params": params})
    return out


def render_snippet(name: str, args: dict[str, Any] | None = None) -> str:
    """Render snippet ``name`` with ``args``; raises ValueError with guidance on misuse."""
    key = name.strip()
    if key.startswith("snippet_"):
        key = key[len("snippet_"):]
    fn = _SNIPPETS.get(key)
    if fn is None:
        raise ValueError(f"Unknown snippet {name!r}. Available: {', '.join(_SNIPPETS)}")
    sig = inspect.signature(fn)
    try:
        bound = sig.bind(**(args or {}))
    except TypeError as e:
        raise ValueError(f"Bad arguments for snippet {key!r}: {e}. Signature: {key}{sig}") from e
    return fn(*bound.args, **bound.kwargs)
