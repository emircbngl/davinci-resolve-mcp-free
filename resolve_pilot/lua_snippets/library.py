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

# GetItemListInTrack also returns transitions (e.g. Cross Dissolves from an FCPXML) as
# items. They have no media pool item and none of the transform properties that clips,
# titles and generators carry, so `clip_items` drops them. Measured on Resolve 20.3.
_CLIP_ITEMS = """\
local function is_transition(it)
  if it:GetMediaPoolItem() then return false end
  local props = it:GetProperty()
  return type(props) ~= "table" or props.Opacity == nil
end
local function clip_items(tl, kind, index)
  local out, transitions = {}, 0
  for _, it in ipairs(tl:GetItemListInTrack(kind, index) or {}) do
    if is_transition(it) then transitions = transitions + 1 else out[#out + 1] = it end
  end
  return out, transitions
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

# Markers resolve-pilot may delete again carry customData starting with this.
OWNED_MARKER_PREFIX = "resolve-pilot:"


def validate_owned_prefix(prefix: str) -> str:
    """Only a specific resolve-pilot namespace may be replaced, never the user's markers."""
    if not prefix.startswith(OWNED_MARKER_PREFIX) or len(prefix) <= len(OWNED_MARKER_PREFIX):
        raise ValueError(f"replace prefix must start with {OWNED_MARKER_PREFIX!r} and name a namespace")
    return prefix


def normalize_markers(markers: list[dict]) -> list[dict]:
    """Validate marker dicts and fill defaults; shared by the Lua and Studio paths.

    Optional keys: ``duration_seconds`` (converted with the timeline fps inside Resolve,
    overrides ``duration``) and ``custom_data`` (Resolve's per-marker customData).
    """
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
            if not math.isfinite(row["seconds"]) or row["seconds"] < 0:
                raise ValueError(f"marker #{i} has an invalid time {m['seconds']!r}")
        if m.get("duration_seconds") is not None:
            ds = float(m["duration_seconds"])
            if not math.isfinite(ds) or ds <= 0:
                raise ValueError(f"marker #{i} has an invalid duration_seconds {ds!r}")
            row["duration_seconds"] = ds
        if m.get("custom_data"):
            row["custom_data"] = str(m["custom_data"])
        rows.append(row)
    return rows


# Frame index -> timecode string, drop-frame aware (29.97 / 59.94 DF skip frame numbers
# 0-1 / 0-3 at every minute except each tenth).
_TIMECODE_FN = """\
local function frames_to_tc(total, fps, df)
  local base = math.floor(fps + 0.5)
  if df then
    local drop = math.floor(base / 15)
    local per10, per1 = base * 600 - drop * 9, base * 60 - drop
    local tens, rem = math.floor(total / per10), total % per10
    total = total + drop * 9 * tens
    if rem > drop then total = total + drop * math.floor((rem - drop) / per1) end
  end
  local s = math.floor(total / base)
  return string.format("%02d:%02d:%02d%s%02d", math.floor(s / 3600), math.floor(s / 60) % 60,
    s % 60, df and ";" or ":", total % base)
end
"""

_PLAYHEAD_PAGES = ("cut", "edit", "color", "fairlight", "deliver")


@_snippet
def snippet_export_timeline_stills(positions: list[dict], out_dir: str) -> str:
    """Export the current timeline's frames (as rendered: grades, titles, effects) as PNGs.

    positions: [{"seconds": 1.5} or {"frame": 36}, ...], offsets from the timeline start.
    Moves the playhead for each still, then puts the playhead and page back.
    """
    rows = []
    for p in positions:
        if p.get("frame") is not None:
            rows.append({"frame": int(p["frame"])})
        elif p.get("seconds") is not None and math.isfinite(float(p["seconds"])):
            rows.append({"seconds": float(p["seconds"])})
        else:
            raise ValueError("each position needs a finite seconds or frame value")
    out_dir = os.path.expanduser(out_dir)
    return f"""-- resolve-pilot: export_timeline_stills
{_TIMELINE_PREAMBLE}{_TIMELINE_FPS}{_TIMECODE_FN}local positions, out_dir, sep = {to_lua(rows)}, {to_lua(out_dir)}, {to_lua(os.sep)}
local df = tostring(timeline:GetSetting("timelineDropFrameTimecode")) == "1"
local start, last = timeline:GetStartFrame(), timeline:GetEndFrame() - 1
local page = resolve:GetCurrentPage()
local playhead_pages = {to_lua(list(_PLAYHEAD_PAGES))}
local on_playhead_page = false
for _, p in ipairs(playhead_pages) do if p == page then on_playhead_page = true end end
if not on_playhead_page then resolve:OpenPage("edit") end
local before = timeline:GetCurrentTimecode()
local stills = {{}}
for i, pos in ipairs(positions) do
  local offset = pos.frame or math.floor(pos.seconds * fps + 0.5)
  local row = {{index = i - 1, frame = offset, seconds = offset / fps}}
  if offset < 0 or start + offset > last then
    row.ok = false
    row.error = "outside the timeline (0-" .. (last - start) .. " frames)"
  else
    row.timecode = frames_to_tc(start + offset, fps, df)
    if not timeline:SetCurrentTimecode(row.timecode) then
      row.ok = false
      row.error = "Resolve refused to move the playhead to " .. row.timecode
    else
      row.path = out_dir .. sep .. string.format("still_%03d.png", i - 1)
      row.ok = project:ExportCurrentFrameAsStill(row.path) and true or false
      if not row.ok then row.error = "ExportCurrentFrameAsStill failed" end
    end
  end
  stills[#stills + 1] = row
end
if before then timeline:SetCurrentTimecode(before) end
if not on_playhead_page and page then resolve:OpenPage(page) end
return {{ok = true, timeline = timeline:GetName(), fps = fps, drop_frame = df, stills = stills}}
"""


@_snippet
def snippet_render_status() -> str:
    """List render jobs with status and progress, and whether Resolve is rendering."""
    return f"""-- resolve-pilot: render_status
{_PROJECT_PREAMBLE}local jobs = {{}}
for _, job in ipairs(project:GetRenderJobList() or {{}}) do
  local st = project:GetRenderJobStatus(job.JobId) or {{}}
  local ms = st.EstimatedTimeRemainingInMs
  jobs[#jobs + 1] = {{job_id = job.JobId, name = job.RenderJobName, timeline = job.TimelineName,
    output = (job.TargetDir or "") .. {to_lua(os.sep)} .. (job.OutputFilename or ""),
    status = st.JobStatus, percent = st.CompletionPercentage,
    seconds_left = (ms and ms > 0) and ms / 1000 or nil, error = st.Error}}
end
return {{ok = true, rendering = project:IsRenderingInProgress() and true or false, jobs = jobs}}
"""


@_snippet
def snippet_add_subtitles(srt_path: str, replace: bool = False) -> str:
    """Put an SRT on subtitle track 1 of the current timeline, at the SRT's own times."""
    srt_path = os.path.expanduser(srt_path)
    return f"""-- resolve-pilot: add_subtitles
{_TIMELINE_PREAMBLE}{_TIMELINE_FPS}local path, replace = {to_lua(srt_path)}, {to_lua(bool(replace))}
local mp = project:GetMediaPool()
local item
local function scan(folder)
  for _, c in ipairs(folder:GetClipList() or {{}}) do
    if not item and c:GetClipProperty("File Path") == path then item = c end
  end
  for _, f in ipairs(folder:GetSubFolderList() or {{}}) do if not item then scan(f) end end
end
scan(mp:GetRootFolder())
local imported = false
if not item then
  local got = mp:ImportMedia({{path}})
  item = got and got[1]
  imported = item ~= nil
end
if not item then return {{ok = false, error = "Resolve could not import " .. path}} end
local kind = item:GetClipProperty("Type")
if kind ~= "Subtitle" then
  return {{ok = false, error = "Resolve did not read " .. path .. " as subtitles (Type: " .. tostring(kind) .. ")"}}
end
-- Without a subtitle track, AppendToTimeline silently adds nothing.
if (timeline:GetTrackCount("subtitle") or 0) == 0 then timeline:AddTrack("subtitle") end
local existing = timeline:GetItemListInTrack("subtitle", 1) or {{}}
local removed = 0
if #existing > 0 then
  if not replace then
    return {{ok = false, error = "Subtitle track 1 already has " .. #existing .. " captions. Pass " ..
      "replace=true to swap them for this file."}}
  end
  local before = #existing
  local page = resolve:GetCurrentPage()
  if page ~= "edit" then resolve:OpenPage("edit") end
  timeline:DeleteClips(existing, false)
  if page and page ~= "edit" then resolve:OpenPage(page) end
  removed = before - #(timeline:GetItemListInTrack("subtitle", 1) or {{}})
end
-- Subtitles are appended at the PLAYHEAD (measured on 20.3), so park it on the first
-- frame and put it back afterwards. Only mediaPoolItem: adding startFrame/endFrame/
-- mediaType for a subtitle item crashes Resolve 20.3 (measured twice).
local playhead = timeline:GetCurrentTimecode()
timeline:SetCurrentTimecode(timeline:GetStartTimecode())
mp:AppendToTimeline({{{{mediaPoolItem = item}}}})
if playhead then timeline:SetCurrentTimecode(playhead) end
local start = timeline:GetStartFrame()
local cues = {{}}
for _, it in ipairs(timeline:GetItemListInTrack("subtitle", 1) or {{}}) do
  cues[#cues + 1] = {{start_seconds = (it:GetStart() - start) / fps, end_seconds = (it:GetEnd() - start) / fps,
    text = it:GetName()}}
end
return {{ok = #cues > 0, timeline = timeline:GetName(), fps = fps, imported = imported,
  media_pool_item = item:GetName(), removed = removed, cues = cues}}
"""


@_snippet
def snippet_add_title(
    text: str,
    start_seconds: float,
    duration_seconds: float,
    track: int | None = None,
    size: float | None = None,
    position: tuple[float, float] | None = None,
    color: tuple[float, float, float] | None = None,
    font: str | None = None,
) -> str:
    """Overlay a Text+ title on the current timeline without touching other clips.

    InsertFusionTitleIntoTimeline is an INSERT edit on V1 (it splits and ripples the
    edit, measured on 20.3), so the title is built on a scratch timeline, wrapped in a
    compound clip and appended onto a free upper video track at start_seconds.
    """
    if not text:
        raise ValueError("text must not be empty")
    if not (math.isfinite(start_seconds) and start_seconds >= 0):
        raise ValueError("start_seconds must be >= 0")
    if not (math.isfinite(duration_seconds) and 0 < duration_seconds <= 600):
        raise ValueError("duration_seconds must be > 0 and at most 600")
    if track is not None and int(track) < 2:
        raise ValueError("track must be 2 or higher; V1 holds the edit")
    inputs: dict[str, Any] = {"StyledText": text}
    if size is not None:
        if not 0 < size <= 1:
            raise ValueError("size is a fraction of the frame height, 0-1 (Text+ default 0.08)")
        inputs["Size"] = float(size)
    if color is not None:
        if len(color) != 3 or any(not 0 <= c <= 1 for c in color):
            raise ValueError("color is [r, g, b], each 0-1")
        inputs.update(Red1=float(color[0]), Green1=float(color[1]), Blue1=float(color[2]))
    if font:
        inputs["Font"] = str(font)
    center = None
    if position is not None:
        if len(position) != 2 or any(not 0 <= v <= 1 for v in position):
            raise ValueError("position is [x, y] in 0-1, origin bottom-left (0.5, 0.5 = centre)")
        center = [float(position[0]), float(position[1])]
    return f"""-- resolve-pilot: add_title
{_TIMELINE_PREAMBLE}{_TIMELINE_FPS}{_TIMECODE_FN}local text, start_s, dur_s = {to_lua(text)}, {to_lua(float(start_seconds))}, {to_lua(float(duration_seconds))}
local want_track, inputs, center = {to_lua(int(track) if track else None)}, {to_lua(inputs)}, {to_lua(center)}
local mp = project:GetMediaPool()
local target = timeline
local tstart = target:GetStartFrame()
local rec = tstart + math.floor(start_s * fps + 0.5)
local page = resolve:GetCurrentPage()
if page ~= "edit" and page ~= "cut" then resolve:OpenPage("edit") end
local function finish(result)
  project:SetCurrentTimeline(target)
  if page and page ~= "edit" and page ~= "cut" then resolve:OpenPage(page) end
  return result
end
local name = "Title: " .. text:gsub("%s+", " "):sub(1, 40)
local scratch = mp:CreateEmptyTimeline("resolve-pilot title " .. tostring({{}}):match("0x(%x+)"))
if not scratch then return finish({{ok = false, error = "Resolve could not create a scratch timeline"}}) end
project:SetCurrentTimeline(scratch)
local sfps = tonumber(scratch:GetSetting("timelineFrameRate")) or fps
local sdf = tostring(scratch:GetSetting("timelineDropFrameTimecode")) == "1"
local need = math.max(1, math.floor(dur_s * sfps + 0.5))
local items, total, set_inputs = {{}}, 0, {{}}
scratch:SetCurrentTimecode(scratch:GetStartTimecode())
while total < need and #items < 120 do
  local it = scratch:InsertFusionTitleIntoTimeline("Text+")
  if not it then break end
  local comp = it:GetFusionCompByIndex(1)
  local tool = comp and (comp:FindTool("Template") or comp:FindToolByID("TextPlus"))
  if tool then
    for k, v in pairs(inputs) do tool:SetInput(k, v) end
    if center then tool:SetInput("Center", center) end
    if #items == 0 then
      for k in pairs(inputs) do set_inputs[k] = tool:GetInput(k) end
      if center then
        local c = tool:GetInput("Center")
        set_inputs.Center = type(c) == "table" and {{c[1], c[2]}} or c
      end
    end
  end
  items[#items + 1] = it
  total = total + it:GetDuration()
  scratch:SetCurrentTimecode(frames_to_tc(scratch:GetEndFrame(), sfps, sdf))
end
if #items == 0 then
  mp:DeleteTimelines({{scratch}})
  return finish({{ok = false, error = "Resolve did not insert a Text+ title (is the Fusion title available?)"}})
end
local cc = scratch:CreateCompoundClip(items, {{name = name}})
local cmpi = cc and cc:GetMediaPoolItem()
project:SetCurrentTimeline(target)
if not cmpi then
  mp:DeleteTimelines({{scratch}})
  return finish({{ok = false, error = "Resolve could not wrap the title in a compound clip"}})
end
-- keep the editable title in a "Titles" bin
local root, bin = mp:GetRootFolder(), nil
for _, f in ipairs(root:GetSubFolderList() or {{}}) do if f:GetName() == "Titles" then bin = f end end
bin = bin or mp:AddSubFolder(root, "Titles")
if bin then mp:MoveClips({{cmpi}}, bin) end
-- a free video track above V1 over [rec, rec + length)
local len = math.floor(need / sfps * fps + 0.5)
local function free(t)
  for _, it in ipairs(target:GetItemListInTrack("video", t) or {{}}) do
    if it:GetStart() < rec + len and it:GetEnd() > rec then return false end
  end
  return true
end
local track = want_track
if track then
  while target:GetTrackCount("video") < track do target:AddTrack("video") end
  if not free(track) then
    mp:DeleteTimelines({{scratch}})
    return finish({{ok = false, error = "V" .. track .. " already has a clip there; pick another track or time"}})
  end
else
  for t = 2, target:GetTrackCount("video") do if not track and free(t) then track = t end end
  if not track then target:AddTrack("video"); track = target:GetTrackCount("video") end
end
local got = mp:AppendToTimeline({{{{mediaPoolItem = cmpi, startFrame = 0, endFrame = need, trackIndex = track,
  recordFrame = rec}}}})
mp:DeleteTimelines({{scratch}})
local placed
for _, it in ipairs(target:GetItemListInTrack("video", track) or {{}}) do
  if it:GetStart() == rec then placed = it end
end
if not placed then
  return finish({{ok = false, error = "Resolve did not place the title on V" .. track, appended = got and #got or 0}})
end
return finish({{ok = true, timeline = target:GetName(), track = track, name = placed:GetName(),
  start_seconds = (placed:GetStart() - tstart) / fps, end_seconds = (placed:GetEnd() - tstart) / fps,
  media_pool_item = cmpi:GetName(), text_pieces = #items, inputs = set_inputs}})
"""


CHECKPOINT_MARK = " · checkpoint "


@_snippet
def snippet_checkpoint(action: str, label: str | None = None, name: str | None = None,
                       backup_path: str | None = None) -> str:
    """Timeline checkpoints (duplicate / list / restore) and .drp project backups."""
    action = _pick(action, ("save", "list", "restore", "export_project"), "action")
    if action == "save" and not label:
        raise ValueError("save needs a label")
    if action == "restore" and not name:
        raise ValueError("restore needs the checkpoint name (see action='list')")
    if action == "export_project" and not backup_path:
        raise ValueError("export_project needs backup_path")
    return f"""-- resolve-pilot: checkpoint
{_PROJECT_PREAMBLE}local action, label, name, backup = {to_lua(action)}, {to_lua(label)}, {to_lua(name)}, {to_lua(backup_path)}
local MARK = {to_lua(CHECKPOINT_MARK)}
local pm, mp = resolve:GetProjectManager(), project:GetMediaPool()
local function timelines()
  local out = {{}}
  for i = 1, project:GetTimelineCount() do out[#out + 1] = project:GetTimelineByIndex(i) end
  return out
end
local function find(n) for _, t in ipairs(timelines()) do if t:GetName() == n then return t end end end
local function bin()
  local root = mp:GetRootFolder()
  for _, f in ipairs(root:GetSubFolderList() or {{}}) do if f:GetName() == "Checkpoints" then return f end end
  return mp:AddSubFolder(root, "Checkpoints")
end
if action == "export_project" then
  pm:SaveProject()
  local ok = pm:ExportProject(project:GetName(), backup, false)
  return {{ok = ok and true or false, project = project:GetName(), path = backup}}
end
if action == "list" then
  local list = {{}}
  for _, t in ipairs(timelines()) do
    local n = t:GetName()
    local s, e = n:find(MARK, 1, true)
    if s then list[#list + 1] = {{name = n, of = n:sub(1, s - 1), label = n:sub(e + 1)}} end
  end
  local cur = project:GetCurrentTimeline()
  return {{ok = true, current = cur and cur:GetName(), checkpoints = list}}
end
local tl = project:GetCurrentTimeline()
if action == "save" then
  if not tl then return {{ok = false, error = "No active timeline to checkpoint."}} end
  pm:SaveProject()
  local base = tl:GetName():gsub(MARK:gsub("%p", "%%%0") .. ".*$", "")
  local copy_name = base .. MARK .. label
  if find(copy_name) then return {{ok = false, error = "A checkpoint named '" .. copy_name .. "' exists; use another label."}} end
  local copy = tl:DuplicateTimeline(copy_name)
  -- DuplicateTimeline makes the copy current (measured on 20.3): go back to the work.
  project:SetCurrentTimeline(tl)
  if not copy then return {{ok = false, error = "Resolve could not duplicate the timeline."}} end
  local item = copy.GetMediaPoolItem and copy:GetMediaPoolItem()
  local b = item and bin()
  if b then mp:MoveClips({{item}}, b) end
  return {{ok = true, checkpoint = copy:GetName(), current = project:GetCurrentTimeline():GetName(),
    in_bin = b and "Checkpoints" or nil}}
end
-- restore: work on a fresh copy of the checkpoint so the checkpoint itself stays intact
local cp = find(name)
if not cp then return {{ok = false, error = "No timeline named '" .. name .. "'. See action='list'."}} end
local s = name:find(MARK, 1, true)
local base = s and name:sub(1, s - 1) or name
local n, work_name = 1, base .. " (restored)"
while find(work_name) do n = n + 1; work_name = base .. " (restored " .. n .. ")" end
local work = cp:DuplicateTimeline(work_name)
if not work then return {{ok = false, error = "Resolve could not copy the checkpoint."}} end
project:SetCurrentTimeline(work)
return {{ok = true, restored_from = name, current = project:GetCurrentTimeline():GetName(),
  note = "The edit before the restore is still there as '" .. (tl and tl:GetName() or "?") .. "'."}}
"""


def _triple(name: str, v: Any, lo: float, hi: float) -> str:
    if v is None:
        return ""
    vals = [float(v)] * 3 if isinstance(v, (int, float)) else [float(x) for x in v]
    if len(vals) != 3 or any(not (math.isfinite(x) and lo <= x <= hi) for x in vals):
        raise ValueError(f"{name} must be one number or [r, g, b], each {lo}..{hi}")
    return " ".join(f"{x:.6g}" for x in vals)


@_snippet
def snippet_grade_clips(
    track: int = 1,
    indices: list[int] | None = None,
    slope: Any = None,
    offset: Any = None,
    power: Any = None,
    saturation: float | None = None,
    node: int = 1,
    copy_from: int | None = None,
    still_dir: str | None = None,
) -> str:
    """Primary grade by numbers (ASC CDL) or copy one clip's grade, with before/after stills.

    Resolve has no GetCDL, so when still_dir is given the clip's middle frame is exported
    before and after and the caller compares them.
    """
    cdl = None
    if copy_from is None:
        cdl = {"NodeIndex": str(int(node)),
               "Slope": _triple("slope", slope, 0, 4) or "1 1 1",
               "Offset": _triple("offset", offset, -1, 1) or "0 0 0",
               "Power": _triple("power", power, 0.1, 4) or "1 1 1"}
        if saturation is not None:
            if not (math.isfinite(saturation) and 0 <= saturation <= 4):
                raise ValueError("saturation must be 0..4 (1 = unchanged)")
            cdl["Saturation"] = f"{float(saturation):.6g}"
        else:
            cdl["Saturation"] = "1"
    idx = None if indices is None else [int(i) for i in indices]
    return f"""-- resolve-pilot: grade_clips
{_TIMELINE_PREAMBLE}{_TIMELINE_FPS}{_TIMECODE_FN}{_CLIP_ITEMS}local cdl, indices, copy_from, still_dir = {to_lua(cdl)}, {to_lua(idx)}, {to_lua(copy_from)}, {to_lua(os.path.expanduser(still_dir) if still_dir else None)}
local items = clip_items(timeline, "video", {int(track)})
local df = tostring(timeline:GetSetting("timelineDropFrameTimecode")) == "1"
local targets = {{}}
if indices then
  for _, i in ipairs(indices) do
    if not items[i + 1] then return {{ok = false, error = "V{int(track)} has " .. #items .. " clips; no index " .. i}} end
    targets[#targets + 1] = i
  end
else
  for i = 1, #items do targets[#targets + 1] = i - 1 end
end
local page, playhead = resolve:GetCurrentPage(), timeline:GetCurrentTimecode()
local on_page = page == "cut" or page == "edit" or page == "color" or page == "fairlight" or page == "deliver"
if not on_page then resolve:OpenPage("edit") end
local function still(it, tag, i)
  if not still_dir then return nil end
  local mid = math.floor((it:GetStart() + it:GetEnd()) / 2)
  if not timeline:SetCurrentTimecode(frames_to_tc(mid, fps, df)) then return nil end
  local path = still_dir .. {to_lua(os.sep)} .. string.format("%s_%03d.png", tag, i)
  return project:ExportCurrentFrameAsStill(path) and path or nil
end
local rows = {{}}
local source = copy_from and items[copy_from + 1]
if copy_from and not source then return {{ok = false, error = "no clip " .. copy_from .. " to copy from"}} end
for _, i in ipairs(targets) do
  local it = items[i + 1]
  local row = {{index = i, name = it:GetName(), before = still(it, "before", i)}}
  if source then
    row.applied = (it ~= source) and (source:CopyGrades({{it}}) and true or false) or true
  else
    row.applied = it:SetCDL(cdl) and true or false
  end
  row.after = still(it, "after", i)
  rows[#rows + 1] = row
end
if playhead then timeline:SetCurrentTimecode(playhead) end
if not on_page and page then resolve:OpenPage(page) end
return {{ok = true, timeline = timeline:GetName(), track = {int(track)}, cdl = cdl, copied_from = copy_from, clips = rows}}
"""


@_snippet
def snippet_add_markers(markers: list[dict], replace_owned_prefix: str | None = None) -> str:
    """Add markers to the current timeline; positions in frames or seconds.

    markers: list of {frame | seconds, color="Blue", name="", note="", duration=1,
    duration_seconds?, custom_data?}. `frame` is an offset from the timeline start;
    `seconds` and `duration_seconds` are converted inside Resolve using the timeline's
    real frame rate. `duration` is in frames.
    replace_owned_prefix: first delete the markers whose customData starts with this
    (a "resolve-pilot:..." namespace), so re-running a tool replaces its own markers.
    """
    rows = normalize_markers(markers)
    prefix = validate_owned_prefix(replace_owned_prefix) if replace_owned_prefix else None
    body = ",\n  ".join(to_lua(r) for r in rows)
    return f"""-- resolve-pilot: add_markers
{_TIMELINE_PREAMBLE}{_TIMELINE_FPS}local markers = {{
  {body}
}}
local replace_prefix = {to_lua(prefix)}
local removed = 0
if replace_prefix then
  local owned = {{}}
  for _, m in pairs(timeline:GetMarkers() or {{}}) do
    local data = m.customData
    if type(data) == "string" and data:sub(1, #replace_prefix) == replace_prefix then
      owned[#owned + 1] = data
    end
  end
  for _, data in ipairs(owned) do
    if timeline:DeleteMarkerByCustomData(data) then removed = removed + 1 end
  end
end
local added, skipped = 0, {{}}
for _, m in ipairs(markers) do
  local frame = m.frame or math.floor(m.seconds * fps + 0.5)
  local duration = m.duration
  if m.duration_seconds then duration = math.max(1, math.floor(m.duration_seconds * fps + 0.5)) end
  if timeline:AddMarker(frame, m.color, m.name, m.note, duration, m.custom_data or "") then
    added = added + 1
  else
    skipped[#skipped + 1] = frame
  end
end
print(string.format("[resolve-pilot] Added %d / %d markers on '%s'", added, #markers, timeline:GetName()))
if removed > 0 then
  print(string.format("[resolve-pilot] Replaced %d earlier resolve-pilot markers", removed))
end
if #skipped > 0 then
  print("[resolve-pilot] Skipped frames (marker already there or out of range): " .. table.concat(skipped, ", "))
end
return {{ok = true, added = added, requested = #markers, skipped_frames = skipped,
  removed_previous = removed, timeline = timeline:GetName(), timeline_fps = fps}}
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
{_CLIP_ITEMS}local function try(f, ...)
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
    local items, transitions = clip_items(tl, track_type, track_index)
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
    cur.transition_count = transitions
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
{_PROJECT_PREAMBLE}{_CLIP_ITEMS}local path = {to_lua(path)}
local tl = project:GetMediaPool():ImportTimelineFromFile(path, {to_lua(opts)})
if not tl then
  print("[resolve-pilot] Import failed: " .. path)
  return {{ok = false, error = "Resolve could not import " .. path .. ". Check the file exists, is valid, " ..
    "and that no timeline with the same name already exists."}}
end
project:SetCurrentTimeline(tl)
local n = 0
for t = 1, tl:GetTrackCount("video") do n = n + #clip_items(tl, "video", t) end
print(string.format("[resolve-pilot] Imported '%s' (%d video clips)", tl:GetName(), n))
return {{ok = true, timeline = tl:GetName(), video_clip_count = n,
  start_frame = tl:GetStartFrame(), end_frame = tl:GetEndFrame()}}
"""


EXPORT_SUBTYPES = ("NONE", "AAF_NEW", "AAF_EXISTING", "CDL", "SDL", "MISSING_CLIPS")

# export_timeline formats -> (resolve.EXPORT_<type>, resolve.EXPORT_<subtype> or None, extension).
# Types and subtypes from Resolve's scripting README; AAF and EDL require a subtype.
EXPORT_FORMATS: dict[str, tuple[str, str | None, str]] = {
    "fcpxml": ("FCPXML_1_10", None, ".fcpxml"),
    "fcpxml_1_10": ("FCPXML_1_10", None, ".fcpxml"),
    "fcpxml_1_9": ("FCPXML_1_9", None, ".fcpxml"),
    "fcpxml_1_8": ("FCPXML_1_8", None, ".fcpxml"),
    "fcp7_xml": ("FCP_7_XML", None, ".xml"),
    "edl": ("EDL", "NONE", ".edl"),
    "edl_cdl": ("EDL", "CDL", ".edl"),
    "edl_sdl": ("EDL", "SDL", ".edl"),
    "edl_missing_clips": ("EDL", "MISSING_CLIPS", ".edl"),
    "aaf": ("AAF", "AAF_NEW", ".aaf"),
    "aaf_existing": ("AAF", "AAF_EXISTING", ".aaf"),
    "otio": ("OTIO", None, ".otio"),
    "drt": ("DRT", None, ".drt"),
    "csv": ("TEXT_CSV", None, ".csv"),
    "tab": ("TEXT_TAB", None, ".txt"),
    "ale": ("ALE", None, ".ale"),
    "ale_cdl": ("ALE_CDL", None, ".ale"),
}


@_snippet
def snippet_export_timeline(
    out_path: str | None = None,
    export_type: str = "FCPXML_1_10",
    export_subtype: str | None = None,
    timeline_name: str | None = None,
    out_dir: str | None = None,
    stamp: str | None = None,
) -> str:
    """Export a timeline (FCPXML_1_10, FCPXML_1_9, AAF, EDL, OTIO, DRT, TEXT_CSV, ...).

    export_type is the suffix of Resolve's `resolve.EXPORT_*` constant. AAF and EDL
    also need export_subtype (AAF: AAF_NEW / AAF_EXISTING; EDL: NONE / CDL / SDL /
    MISSING_CLIPS); it defaults to AAF_NEW and NONE.
    timeline_name: export that timeline instead of the current one.
    Without out_path the file goes to out_dir as <timeline name>_<stamp><extension>.
    """
    if not re.fullmatch(r"[A-Z0-9_]+", export_type):
        raise ValueError(f"export_type must look like FCPXML_1_10, got {export_type!r}")
    if export_subtype is None:
        export_subtype = {"AAF": "AAF_NEW", "EDL": "NONE"}.get(export_type)
    if export_subtype is not None and export_subtype not in EXPORT_SUBTYPES:
        raise ValueError(f"export_subtype must be one of {', '.join(EXPORT_SUBTYPES)}")
    if out_path is None and out_dir is None:
        raise ValueError("Pass out_path, or out_dir for an automatic file name.")
    ext = next((e for t, _, e in EXPORT_FORMATS.values() if t == export_type), ".txt")
    out_path = os.path.expanduser(out_path) if out_path else None
    out_dir = os.path.expanduser(out_dir) if out_dir else None
    sub = f", resolve.EXPORT_{export_subtype}" if export_subtype else ""
    sub_check = (f'\nif resolve.EXPORT_{export_subtype} == nil then\n  return {{ok = false, error = '
                 f'"This Resolve has no resolve.EXPORT_{export_subtype}"}}\nend' if export_subtype else "")
    return f"""-- resolve-pilot: export_timeline
{_PROJECT_PREAMBLE}if resolve.EXPORT_{export_type} == nil then
  return {{ok = false, error = "Unknown export_type {export_type} in this Resolve version"}}
end{sub_check}
local wanted, out = {to_lua(timeline_name)}, {to_lua(out_path)}
local current = project:GetCurrentTimeline()
local timeline = current
if wanted then
  timeline = nil
  local names = {{}}
  for i = 1, (project:GetTimelineCount() or 0) do
    local t = project:GetTimelineByIndex(i)
    if t and t:GetName() == wanted then timeline = t end
    if t then names[#names + 1] = t:GetName() end
  end
  if not timeline then
    return {{ok = false, error = "No timeline named '" .. wanted .. "'.", timelines = names}}
  end
end
if not timeline then
  return {{ok = false, error = "No active timeline. Open a timeline on the Edit page first."}}
end
local name = timeline:GetName()
if not out then
  local slug = name:gsub("[^%w%-]+", "_"):gsub("^_+", ""):gsub("_+$", "")
  if slug == "" then slug = "timeline" end
  out = {to_lua(out_dir)} .. {to_lua(os.sep)} .. slug .. "_" .. {to_lua(stamp or "export")} .. {to_lua(ext)}
end
local function export() return timeline:Export(out, resolve.EXPORT_{export_type}{sub}) end
local done, ok = pcall(export)
local switched = false
-- Export may only work on the current timeline; retry with it current, then switch back.
if not (done and ok) and current and name ~= current:GetName() then
  switched = project:SetCurrentTimeline(timeline) and true or false
  if switched then
    done, ok = pcall(export)
    project:SetCurrentTimeline(current)
  end
end
print(string.format("[resolve-pilot] Export %s -> %s", tostring(done and ok), out))
local result = {{ok = (done and ok) and true or false, path = out, timeline = name,
  export_type = {to_lua(export_type)}, export_subtype = {to_lua(export_subtype)}, switched_timeline = switched}}
if not done then result.error = tostring(ok)
elseif not ok then result.error = "Resolve's Timeline:Export returned " .. tostring(ok) end
return result
"""


@_snippet
def snippet_set_clip_color(clip_color: str = "Orange", track: int = 1) -> str:
    """Color-code every clip on a video track. Useful for sectioning a long timeline."""
    c = validate_clip_color(clip_color)
    return f"""-- resolve-pilot: set_clip_color
{_TIMELINE_PREAMBLE}{_CLIP_ITEMS}local items = clip_items(timeline, "video", {int(track)})
local n = 0
for _, item in ipairs(items or {{}}) do
  if item:SetClipColor({to_lua(c)}) then n = n + 1 end
end
print(string.format("[resolve-pilot] Colored %d clips on V%d", n, {int(track)}))
return {{ok = true, colored = n, track = {int(track)}, color = {to_lua(c)}}}
"""


# ────────────────────────────────────────────────────────────────────────────
# Inspector properties (TimelineItem:SetProperty) and tracks
# ────────────────────────────────────────────────────────────────────────────

# Keys TimelineItem:SetProperty accepts, from Resolve's scripting README, in the order they
# are written (Scaling before zoom, ZoomGang before ZoomX/ZoomY). Value kinds:
#   ("number", min, max)  min/max None = only finite; Pan/Tilt/AnchorPoint are documented
#                         as +-4x the timeline width/height, crops as 0..width/height
#   ("bool",)
#   ("enum", prefix, names)  a name resolves to the resolve.<prefix><NAME> constant inside
#                         Resolve (its integer value is not assumed); an int is passed as is
CLIP_PROPERTIES: dict[str, tuple] = {
    "Scaling": ("enum", "SCALE_", ("USE_PROJECT", "CROP", "FIT", "FILL", "STRETCH")),
    "ResizeFilter": ("enum", "RESIZE_FILTER_", (
        "USE_PROJECT", "SHARPER", "SMOOTHER", "BICUBIC", "BILINEAR", "BESSEL", "BOX",
        "CATMULL_ROM", "CUBIC", "GAUSSIAN", "LANCZOS", "MITCHELL", "NEAREST_NEIGHBOR",
        "QUADRATIC", "SINC", "LINEAR")),
    "ZoomGang": ("bool",),
    "ZoomX": ("number", 0.0, 100.0),
    "ZoomY": ("number", 0.0, 100.0),
    "Pan": ("number", None, None),
    "Tilt": ("number", None, None),
    "RotationAngle": ("number", -360.0, 360.0),
    "AnchorPointX": ("number", None, None),
    "AnchorPointY": ("number", None, None),
    "Pitch": ("number", -1.5, 1.5),
    "Yaw": ("number", -1.5, 1.5),
    "FlipX": ("bool",),
    "FlipY": ("bool",),
    "CropLeft": ("number", 0.0, None),
    "CropRight": ("number", 0.0, None),
    "CropTop": ("number", 0.0, None),
    "CropBottom": ("number", 0.0, None),
    "CropSoftness": ("number", -100.0, 100.0),
    "CropRetain": ("bool",),
    "Distortion": ("number", -1.0, 1.0),
    "Opacity": ("number", 0.0, 100.0),
    "CompositeMode": ("enum", "COMPOSITE_", (
        "NORMAL", "ADD", "SUBTRACT", "DIFF", "MULTIPLY", "SCREEN", "OVERLAY", "HARDLIGHT",
        "SOFTLIGHT", "DARKEN", "LIGHTEN", "COLOR_DODGE", "COLOR_BURN", "EXCLUSION", "HUE",
        "SATURATE", "COLORIZE", "LUMA_MASK", "DIVIDE", "LINEAR_DODGE", "LINEAR_BURN",
        "LINEAR_LIGHT", "VIVID_LIGHT", "PIN_LIGHT", "HARD_MIX", "LIGHTER_COLOR",
        "DARKER_COLOR", "FOREGROUND", "ALPHA", "INVERTED_ALPHA", "LUM", "INVERTED_LUM")),
    "DynamicZoomEase": ("enum", "DYNAMIC_ZOOM_EASE_", ("LINEAR", "IN", "OUT", "IN_AND_OUT")),
    "RetimeProcess": ("enum", "RETIME_", ("USE_PROJECT", "NEAREST", "FRAME_BLEND", "OPTICAL_FLOW")),
    "MotionEstimation": ("enum", "MOTION_EST_", (
        "USE_PROJECT", "STANDARD_FASTER", "STANDARD_BETTER", "ENHANCED_FASTER",
        "ENHANCED_BETTER", "SPEED_WARP_BETTER", "SPEED_WARP_FASTER")),
}
TRACK_TYPES = ("video", "audio", "subtitle")
# Timeline:AddTrack(trackType, subTrackType): audio channel layouts from the scripting README.
AUDIO_TRACK_SUBTYPES = ("mono", "stereo", "5.1", "5.1film", "7.1", "7.1film",
                        *(f"adaptive{n}" for n in range(1, 25)))


def normalize_clip_properties(properties: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Validate Inspector properties against CLIP_PROPERTIES; shared by Lua and Studio.

    Returns rows in write order: {key, kind, requested, value | const}. ``const`` is the
    name of a resolve.* enum constant to look up inside Resolve.
    """
    by_lower = {k.lower(): k for k in CLIP_PROPERTIES}
    picked: dict[str, Any] = {}
    for raw_key, v in (properties or {}).items():
        key = by_lower.get(str(raw_key).lower())
        if key is None:
            raise ValueError(f"Unknown or unsupported clip property {raw_key!r}. "
                             f"Use one of: {', '.join(CLIP_PROPERTIES)}")
        picked[key] = v
    rows = []
    for key, spec in CLIP_PROPERTIES.items():
        if key not in picked:
            continue
        v = picked[key]
        row: dict[str, Any] = {"key": key, "kind": spec[0], "requested": v}
        if spec[0] == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"{key} takes true or false, got {v!r}")
            row["value"] = v
        elif spec[0] == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"{key} takes a number, got {v!r}")
            lo, hi = spec[1], spec[2]
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                raise ValueError(f"{key} must be between {lo if lo is not None else '-inf'} "
                                 f"and {hi if hi is not None else 'inf'}, got {v!r}")
            row["value"] = float(v)
        else:
            prefix, names = spec[1], spec[2]
            if isinstance(v, int) and not isinstance(v, bool):
                row["value"] = v
            elif isinstance(v, str):
                name = v.strip().upper().replace(" ", "_").replace("-", "_")
                name = name.removeprefix(prefix)
                if name not in names:
                    raise ValueError(f"{key} takes one of {', '.join(n.lower() for n in names)} "
                                     f"(or the integer value), got {v!r}")
                row["const"] = prefix + name
            else:
                raise ValueError(f"{key} takes a name or an integer, got {v!r}")
        rows.append(row)
    return rows


# Read-back comparison, the same rule as values_match() in Python: Resolve can report
# booleans as 0/1 and stores numbers as floats, so allow a small relative tolerance.
_SAME_VALUE_FN = """\
local function same_value(kind, want, got)
  if got == nil then return false end
  if kind == "bool" then
    if type(got) == "number" then got = got ~= 0 end
    if type(got) == "string" then got = (got == "true" or got == "1") end
    return got == want
  end
  local g, w = tonumber(got), tonumber(want)
  return g ~= nil and w ~= nil and math.abs(g - w) <= 1e-3 * math.max(1, math.abs(w))
end
"""


def values_match(kind: str, want: Any, got: Any) -> bool:
    """Python twin of the Lua ``same_value`` read-back check."""
    if got is None:
        return False
    if kind == "bool":
        if isinstance(got, (int, float)) and not isinstance(got, bool):
            got = got != 0
        elif isinstance(got, str):
            got = got in ("true", "1")
        return got is want or got == want
    try:
        g, w = float(got), float(want)
    except (TypeError, ValueError):
        return False
    return abs(g - w) <= 1e-3 * max(1.0, abs(w))


@_snippet
def snippet_set_clip_properties(
    track_type: str = "video",
    track: int = 1,
    indices: list[int] | None = None,
    properties: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> str:
    """Set Inspector properties (zoom, pan, crop, opacity, ...) and enable/disable clips, with read-back.

    indices: 0-based clip indices as get_state(include_clips=true) lists them (transitions
    skipped); None = every clip on the track. Every write is read back with GetProperty /
    GetClipEnabled, and `ok` comes from the read-back, not from the setter's return value.
    """
    track_type = _pick(track_type, ("video", "audio"), "track type")
    rows = normalize_clip_properties(properties)
    if not rows and enabled is None:
        raise ValueError("Nothing to change: pass properties and/or enabled.")
    idx = None if indices is None else [int(i) for i in indices]
    if idx is not None and (not idx or min(idx) < 0):
        raise ValueError("indices must be a non-empty list of 0-based clip indices (or None for all)")
    return f"""-- resolve-pilot: set_clip_properties
{_TIMELINE_PREAMBLE}{_CLIP_ITEMS}{_SAME_VALUE_FN}local track_type, track = {to_lua(track_type)}, {int(track)}
local indices, enabled = {to_lua(idx)}, {to_lua(enabled)}
local props = {to_lua(rows)}
local resolved = {{}}
for _, p in ipairs(props) do
  if p.const then
    p.value = resolve[p.const]
    if p.value == nil then
      return {{ok = false, error = "This Resolve has no resolve." .. p.const ..
        " constant; pass the integer value for " .. p.key .. " instead."}}
    end
    resolved[p.key] = p.value
  end
end
local items = clip_items(timeline, track_type, track)
local targets, missing = {{}}, {{}}
if indices then
  for _, i in ipairs(indices) do
    if items[i + 1] then targets[#targets + 1] = {{i, items[i + 1]}} else missing[#missing + 1] = i end
  end
else
  for i, it in ipairs(items) do targets[#targets + 1] = {{i - 1, it}} end
end
local function call(it, method, ...)
  if type(it[method]) ~= "function" then return false, method .. " is not available" end
  return pcall(it[method], it, ...)
end
local clips, changed = {{}}, 0
for _, t in ipairs(targets) do
  local index, it = t[1], t[2]
  local row = {{index = index, name = it:GetName(), requested = {{}}, actual = {{}}, mismatched = {{}},
    contradicted = {{}}, ok = true}}
  for _, p in ipairs(props) do
    row.requested[p.key] = p.requested
    local done, said = call(it, "SetProperty", p.key, p.value)
    local read, got = call(it, "GetProperty", p.key)
    if read then row.actual[p.key] = got end
    if not (done and read and same_value(p.kind, p.value, got)) then
      row.ok = false
      row.mismatched[#row.mismatched + 1] = p.key
      if not done then row.error = tostring(said)
      elseif said == true then row.contradicted[#row.contradicted + 1] = p.key end
    end
  end
  if enabled ~= nil then
    row.requested.enabled = enabled
    local done, said = call(it, "SetClipEnabled", enabled)
    local read, got = call(it, "GetClipEnabled")
    if read then row.actual.enabled = got end
    if not (done and read and same_value("bool", enabled, got)) then
      row.ok = false
      row.mismatched[#row.mismatched + 1] = "enabled"
      if not done then row.error = tostring(said)
      elseif said == true then row.contradicted[#row.contradicted + 1] = "enabled" end
    end
  end
  if row.ok then changed = changed + 1 end
  clips[#clips + 1] = row
end
local out = {{ok = #targets > 0 and changed == #targets and #missing == 0, track_type = track_type,
  track = track, clip_count = #items, changed = changed, clips = clips, missing_indices = missing,
  resolved_values = resolved, timeline = timeline:GetName()}}
if #targets == 0 then
  out.error = "No clips to change on " .. track_type .. " track " .. track .. " (" .. #items .. " clips there)."
elseif not out.ok then
  out.error = "Some values did not stick: Resolve's read-back differs from what was requested " ..
    "(see clips[].mismatched), or indices are missing."
end
print(string.format("[resolve-pilot] Changed %d / %d clips on %s %d", changed, #targets, track_type, track))
return out
"""


def normalize_track_changes(changes: list[dict] | None, add: list[dict] | None
                            ) -> tuple[list[dict], list[dict]]:
    """Validate set_tracks input; shared by the Lua and Studio paths."""
    out_changes, out_add = [], []
    for i, a in enumerate(add or []):
        kind = _pick(str(a.get("track_type") or ""), TRACK_TYPES, "track type")
        row: dict[str, Any] = {"track_type": kind}
        if a.get("sub_type"):
            if kind != "audio":
                raise ValueError(f"add #{i}: sub_type is only for audio tracks")
            row["sub_type"] = _pick(str(a["sub_type"]), AUDIO_TRACK_SUBTYPES, "audio track sub_type")
        out_add.append(row)
    for i, c in enumerate(changes or []):
        kind = _pick(str(c.get("track_type") or ""), TRACK_TYPES, "track type")
        index = c.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise ValueError(f"change #{i}: index must be a track number >= 1")
        row = {"track_type": kind, "index": index}
        if c.get("name") is not None:
            row["name"] = str(c["name"])
        for key in ("enabled", "locked"):
            if c.get(key) is not None:
                if not isinstance(c[key], bool):
                    raise ValueError(f"change #{i}: {key} must be true or false")
                row[key] = c[key]
        if len(row) == 2:
            raise ValueError(f"change #{i}: give name, enabled and/or locked")
        out_changes.append(row)
    if not out_changes and not out_add:
        raise ValueError("Nothing to do: pass changes and/or add.")
    return out_changes, out_add


@_snippet
def snippet_set_tracks(changes: list[dict] | None = None, add: list[dict] | None = None) -> str:
    """Add tracks, and rename / enable / disable / lock / unlock tracks, with read-back.

    add: [{track_type, sub_type?}] runs first (audio sub_type: mono, stereo, 5.1, ...).
    changes: [{track_type, index, name?, enabled?, locked?}]; each reports
    {requested, actual, ok} read back with GetTrackName / GetIsTrackEnabled / GetIsTrackLocked.
    """
    rows, adds = normalize_track_changes(changes, add)
    return f"""-- resolve-pilot: set_tracks
{_TIMELINE_PREAMBLE}{_SAME_VALUE_FN}local adds, changes = {to_lua(adds)}, {to_lua(rows)}
local function call(method, ...)
  if type(timeline[method]) ~= "function" then return false, method .. " is not available" end
  return pcall(timeline[method], timeline, ...)
end
local function count(kind)
  local done, n = call("GetTrackCount", kind)
  return done and tonumber(n) or 0
end
local added, all_ok = {{}}, true
for _, a in ipairs(adds) do
  local before = count(a.track_type)
  local done, said
  if a.sub_type then done, said = call("AddTrack", a.track_type, a.sub_type)
  else done, said = call("AddTrack", a.track_type) end
  local after = count(a.track_type)
  local row = {{track_type = a.track_type, sub_type = a.sub_type, before = before, after = after,
    ok = after == before + 1}}
  if row.ok then row.index = after end
  if not done then row.error = tostring(said)
  elseif not row.ok then row.error = "AddTrack returned " .. tostring(said) .. " but the track count did not grow" end
  all_ok = all_ok and row.ok
  added[#added + 1] = row
end
local fields = {{
  {{"name", "SetTrackName", "GetTrackName", "string"}},
  {{"enabled", "SetTrackEnable", "GetIsTrackEnabled", "bool"}},
  {{"locked", "SetTrackLock", "GetIsTrackLocked", "bool"}},
}}
local results = {{}}
for _, c in ipairs(changes) do
  local row = {{track_type = c.track_type, index = c.index, requested = {{}}, actual = {{}},
    mismatched = {{}}, contradicted = {{}}, ok = true}}
  local n = count(c.track_type)
  if c.index > n then
    row.ok = false
    row.error = "There is no " .. c.track_type .. " track " .. c.index .. " (" .. n .. " tracks)."
  else
    for _, f in ipairs(fields) do
      local key, setter, getter, kind = f[1], f[2], f[3], f[4]
      if c[key] ~= nil then
        row.requested[key] = c[key]
        local done, said = call(setter, c.track_type, c.index, c[key])
        local read, got = call(getter, c.track_type, c.index)
        if read then row.actual[key] = got end
        local same = read and (kind == "string" and got == c[key] or kind == "bool" and same_value("bool", c[key], got))
        if not (done and same) then
          row.ok = false
          row.mismatched[#row.mismatched + 1] = key
          if not done then row.error = tostring(said)
          elseif said == true then row.contradicted[#row.contradicted + 1] = key end
        end
      end
    end
  end
  all_ok = all_ok and row.ok
  results[#results + 1] = row
end
local out = {{ok = all_ok, added = added, changes = results, timeline = timeline:GetName(),
  track_counts = {{video = count("video"), audio = count("audio"), subtitle = count("subtitle")}}}}
if not all_ok then
  out.error = "Some track changes did not stick (see added[].error and changes[].mismatched)."
end
print(string.format("[resolve-pilot] Tracks: %d added, %d changed, ok=%s", #added, #results, tostring(all_ok)))
return out
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
{_TIMELINE_PREAMBLE}{_CLIP_ITEMS}local items = clip_items(timeline, "video", {int(track)})
-- Resolve only sees LUTs added to its LUT folder after a refresh.
if type(project.RefreshLUTList) == "function" then project:RefreshLUTList() end
local applied = 0
for _, item in ipairs(items) do
  if item:SetLUT({int(node_index)}, {to_lua(lut_path)}) then applied = applied + 1 end
end
print(string.format("[resolve-pilot] Applied LUT to %d / %d clips at node %d", applied, #items, {int(node_index)}))
if applied == 0 and #items > 0 then
  return {{ok = false, applied = 0, total = #items, node_index = {int(node_index)},
    error = "Resolve rejected the LUT for every clip. It must sit inside Resolve's LUT folder, " ..
      "and the node index must exist on each clip."}}
end
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
{_TIMELINE_PREAMBLE}{_CLIP_ITEMS}local items = clip_items(timeline, "video", {int(track)})
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
{_TIMELINE_PREAMBLE}{_CLIP_ITEMS}local items = clip_items(timeline, "video", {int(track)})
local ok = timeline:ApplyGradeFromDRX({to_lua(drx_path)}, {int(mode)}, items)
print("[resolve-pilot] Grade-from-DRX applied: " .. tostring(ok))
return {{ok = ok and true or false}}
"""


# ────────────────────────────────────────────────────────────────────────────
# Live timeline building: media pool imports, appends, deletes
# ────────────────────────────────────────────────────────────────────────────
#
# Behaviour these rely on was measured by another project with Python on Resolve
# 21.0.4 Free and is not yet confirmed from Lua: ImportMedia wants plain path strings
# (a {FilePath = ...} table returns an empty list), AppendToTimeline's endFrame is
# exclusive and counts frames of the source clip at its own fps. Every snippet reads
# back what actually landed and reports a mismatch instead of trusting the call.

MEDIA_KINDS = ("both", "video", "audio")

# Needs `project`. Defines mp, try, clip_path, same_path, find_item, open_bin,
# describe_mpi, import_into.
_MEDIA_POOL_FNS = """\
local mp = project:GetMediaPool()
local function try(obj, method, ...)
  local f = obj and obj[method]
  if type(f) ~= "function" then return nil end
  local ok, v = pcall(f, obj, ...)
  if ok then return v end
  return nil
end
local function clip_path(mpi) return try(mpi, "GetClipProperty", "File Path") end
local function same_path(a, b)
  if type(a) ~= "string" or type(b) ~= "string" or a == "" then return false end
  return a == b or a:gsub("\\\\", "/") == b:gsub("\\\\", "/")
end
local function find_item(folder, pred, recursive)
  for _, mpi in ipairs(try(folder, "GetClipList") or {}) do
    if pred(mpi) then return mpi end
  end
  if recursive then
    for _, sub in ipairs(try(folder, "GetSubFolderList") or {}) do
      local hit = find_item(sub, pred, true)
      if hit then return hit end
    end
  end
  return nil
end
-- "B-roll/Day 1" below the root bin; missing bins are created.
local function open_bin(path)
  local folder, created = mp:GetRootFolder(), {}
  for name in path:gmatch("[^/]+") do
    local nxt
    for _, sub in ipairs(try(folder, "GetSubFolderList") or {}) do
      if try(sub, "GetName") == name then nxt = sub; break end
    end
    if not nxt then
      nxt = mp:AddSubFolder(folder, name)
      if not nxt then return nil, "Resolve could not create the bin '" .. name .. "'", created end
      created[#created + 1] = name
    end
    folder = nxt
  end
  return folder, nil, created
end
local function num(v) return tonumber(type(v) == "string" and v:match("[%d%.]+") or v) end
local function describe_mpi(mpi)
  return {name = try(mpi, "GetName"), media_pool_id = try(mpi, "GetUniqueId"), path = clip_path(mpi),
    fps = num(try(mpi, "GetClipProperty", "FPS")), frames = num(try(mpi, "GetClipProperty", "Frames"))}
end
-- Import `path` into `folder` (made current for the import) unless it is already there.
local function import_into(path, folder)
  local hit = find_item(folder, function(m) return same_path(clip_path(m), path) end, false)
  if hit then return hit, true end
  if folder then mp:SetCurrentFolder(folder) end
  local got = mp:ImportMedia({path})
  if type(got) ~= "table" or not got[1] then
    return nil, false, "Resolve imported nothing from " .. path .. " (unsupported or unreadable file?)"
  end
  return got[1], false
end
"""

# Needs `try`, `fps` (timeline) and `start` (timeline start frame).
_DESCRIBE_ITEM = """\
local function describe_item(it)
  local tt = try(it, "GetTrackTypeAndIndex")
  local s, e = it:GetStart(), it:GetEnd()
  local out = {name = try(it, "GetName"), start_frame = s, end_frame = e, duration_frames = e - s,
    start_seconds = (s - start) / fps, end_seconds = (e - start) / fps, duration_seconds = (e - s) / fps,
    source_in_frame = try(it, "GetLeftOffset")}
  if type(tt) == "table" and tt[1] then
    out.track_type, out.track_index = tt[1], tt[2]
    out.track = tostring(tt[1]) .. " " .. tostring(tt[2])
  end
  return out
end
"""


def normalize_bin(bin_path: str | None) -> str | None:
    """'B-roll/Day 1' -> validated bin path below the root bin, or None."""
    if bin_path is None:
        return None
    parts = [p.strip() for p in str(bin_path).replace("\\", "/").split("/") if p.strip()]
    if not parts:
        raise ValueError("bin must name a bin, e.g. 'B-roll' or 'B-roll/Day 1'")
    return "/".join(parts)


def normalize_clips(clips: list[dict]) -> list[dict]:
    """Validate clip specs for append_clips; shared by the Lua and Studio paths.

    Each clip: ``path`` (absolute) or ``media_pool_id``; ``in_seconds`` (default 0);
    ``out_seconds`` or ``duration_seconds`` (source seconds; omit both for "to the end");
    ``track`` (1-based); ``media`` both|video|audio; ``record_seconds`` (timeline offset).
    """
    rows = []
    for i, c in enumerate(clips):
        path, mid = c.get("path"), c.get("media_pool_id")
        if bool(path) == bool(mid):
            raise ValueError(f"clip #{i} needs exactly one of path or media_pool_id")
        row: dict[str, Any] = {"index": i, "media": _pick(c.get("media") or "both", MEDIA_KINDS, "media")}
        if path:
            row["path"] = os.path.expanduser(str(path))
            if row["path"].lower().endswith((".srt", ".vtt", ".scc", ".stl", ".ttml")):
                raise ValueError(f"clip #{i} is a subtitle file; use add_subtitles for captions "
                                 "(appending subtitles with a range can crash Resolve)")
        else:
            row["media_pool_id"] = str(mid)
        start = float(c.get("in_seconds") or 0.0)
        if not math.isfinite(start) or start < 0:
            raise ValueError(f"clip #{i} has an invalid in_seconds {c.get('in_seconds')!r}")
        row["in_seconds"] = start
        out, dur = c.get("out_seconds"), c.get("duration_seconds")
        if out is not None and dur is not None:
            raise ValueError(f"clip #{i}: give out_seconds or duration_seconds, not both")
        if dur is not None:
            out = start + float(dur)
        if out is not None:
            out = float(out)
            if not math.isfinite(out) or out <= start:
                raise ValueError(f"clip #{i}: the out point must be after in_seconds ({start})")
            row["out_seconds"] = out
        if c.get("track") is not None:
            if int(c["track"]) < 1:
                raise ValueError(f"clip #{i}: track numbers start at 1")
            row["track"] = int(c["track"])
        if c.get("record_seconds") is not None:
            rec = float(c["record_seconds"])
            if not math.isfinite(rec) or rec < 0:
                raise ValueError(f"clip #{i} has an invalid record_seconds {rec!r}")
            row["record_seconds"] = rec
        rows.append(row)
    return rows


@_snippet
def snippet_import_media(paths: list[str], bin: str | None = None) -> str:
    """Import media files into the media pool (optionally into a bin), skipping files already there.

    paths: absolute file paths. bin: "B-roll" or "B-roll/Day 1" below the root bin, created
    when missing; omit for the current bin. Files whose File Path is already in that bin
    are reused, not imported twice.
    """
    if not paths:
        raise ValueError("paths is empty")
    full = [os.path.expanduser(str(p)) for p in paths]
    return f"""-- resolve-pilot: import_media
{_PROJECT_PREAMBLE}{_MEDIA_POOL_FNS}local paths, bin_path = {to_lua(full)}, {to_lua(normalize_bin(bin))}
local before = mp:GetCurrentFolder()
local folder, err, created = before, nil, {{}}
if bin_path then
  folder, err, created = open_bin(bin_path)
  if not folder then return {{ok = false, error = err, bins_created = created}} end
end
local items, imported, reused, failed = {{}}, 0, 0, 0
for i, path in ipairs(paths) do
  local mpi, already, e = import_into(path, folder)
  local row = {{index = i - 1, path = path, ok = mpi ~= nil}}
  if mpi then
    for k, v in pairs(describe_mpi(mpi)) do row[k] = v end
    row.path = path
    row.pool_path = clip_path(mpi)
    row.already_in_pool = already
    row.in_bin = find_item(folder, function(m) return try(m, "GetUniqueId") == row.media_pool_id
      and same_path(clip_path(m), row.pool_path) end, false) ~= nil
    if already then reused = reused + 1 else imported = imported + 1 end
  else
    row.error = e
    failed = failed + 1
  end
  items[#items + 1] = row
end
if before and folder ~= before then mp:SetCurrentFolder(before) end
print(string.format("[resolve-pilot] Imported %d, reused %d, failed %d into bin '%s'", imported, reused,
  failed, tostring(try(folder, "GetName"))))
return {{ok = failed == 0, bin = try(folder, "GetName"), bins_created = created, imported = imported,
  reused = reused, failed = failed, items = items}}
"""


@_snippet
def snippet_append_clips(
    clips: list[dict],
    timeline: str | None = None,
    new_timeline: str | None = None,
    new_timeline_fps: float | None = None,
) -> str:
    """Append source ranges to a timeline (the current one, a named one, or a new empty one).

    clips: [{path | media_pool_id, in_seconds=0, out_seconds | duration_seconds,
    track?, media="both"|"video"|"audio", record_seconds?}, ...]. Source seconds are
    converted with the source clip's own fps; record_seconds (offset from the timeline
    start) with the timeline fps. Files not in the media pool are imported into the
    current bin. Returns the timeline items that actually landed.
    """
    rows = normalize_clips(clips)
    if timeline and new_timeline:
        raise ValueError("give timeline (an existing one) or new_timeline, not both")
    if not rows and not new_timeline:
        raise ValueError("clips is empty")
    if new_timeline_fps is not None and not new_timeline:
        raise ValueError("new_timeline_fps only applies with new_timeline")
    return f"""-- resolve-pilot: append_clips
{_PROJECT_PREAMBLE}{_MEDIA_POOL_FNS}local clips = {to_lua(rows)}
local want_name, new_name, new_fps = {to_lua(timeline)}, {to_lua(new_timeline)}, {to_lua(new_timeline_fps)}
local function round(x) return math.floor(x + 0.5) end
local function find_timeline(name)
  for i = 1, (project:GetTimelineCount() or 0) do
    local t = project:GetTimelineByIndex(i)
    if t and t:GetName() == name then return t end
  end
  return nil
end
local timeline, created, fps_applied
if new_name then
  if find_timeline(new_name) then
    return {{ok = false, error = "A timeline named '" .. new_name .. "' already exists. Pick another name, " ..
      "or append to it with timeline='" .. new_name .. "'."}}
  end
  timeline = mp:CreateEmptyTimeline(new_name)
  if not timeline then
    return {{ok = false, error = "Resolve did not create the timeline '" .. new_name .. "' (CreateEmptyTimeline returned nil)."}}
  end
  created = true
  if new_fps then
    -- A timeline's own frame rate can only be set while it is empty.
    try(timeline, "SetSetting", "useCustomSettings", "1")
    fps_applied = try(timeline, "SetSetting", "timelineFrameRate", tostring(new_fps)) and true or false
  end
elseif want_name then
  timeline = find_timeline(want_name)
  if not timeline then return {{ok = false, error = "No timeline named '" .. want_name .. "' in this project."}} end
else
  timeline = project:GetCurrentTimeline()
  if not timeline then return {{ok = false, error = "No active timeline. Pass timeline or new_timeline."}} end
end
-- AppendToTimeline always targets the current timeline.
project:SetCurrentTimeline(timeline)
local cur = project:GetCurrentTimeline()
if not cur or cur:GetName() ~= timeline:GetName() then
  return {{ok = false, error = "Resolve did not switch to timeline '" .. timeline:GetName() .. "'; nothing was appended."}}
end
local fps = tonumber(timeline:GetSetting("timelineFrameRate")) or tonumber(project:GetSetting("timelineFrameRate")) or 24
local start = timeline:GetStartFrame()
{_DESCRIBE_ITEM}local root = mp:GetRootFolder()
local results, appended, failed, tracks_added = {{}}, 0, 0, {{}}
local function ensure_track(kind, index)
  local n = timeline:GetTrackCount(kind) or 0
  while n < index do
    if not timeline:AddTrack(kind) then return false end
    local m = timeline:GetTrackCount(kind) or 0
    if m <= n then return false end
    n = m
    tracks_added[#tracks_added + 1] = kind .. " " .. n
  end
  return true
end
for _, c in ipairs(clips) do
  local row, warnings = {{index = c.index, ok = false}}, {{}}
  local mpi, e
  if c.media_pool_id then
    mpi = find_item(root, function(m) return try(m, "GetUniqueId") == c.media_pool_id end, true)
    if not mpi then e = "No media pool item with id " .. c.media_pool_id end
  else
    mpi = find_item(root, function(m) return same_path(clip_path(m), c.path) end, true)
    if not mpi then
      local imported
      mpi, imported, e = import_into(c.path, mp:GetCurrentFolder())
      row.imported = mpi ~= nil and not imported
    end
  end
  if mpi then
    local src = describe_mpi(mpi)
    row.source = src
    if mpi:GetClipProperty("Type") == "Subtitle" then
      -- A subtitle item with a frame range crashes Resolve 20.3 (measured twice).
      e = "this is a subtitle file; use add_subtitles for captions (appending it here can crash Resolve)"
    end
    local sfps = src.fps
    if not sfps or sfps <= 0 then
      sfps = fps
      warnings[#warnings + 1] = "Resolve reported no FPS for the source; used the timeline fps " .. fps
    end
    local s = round(c.in_seconds * sfps)
    local en = c.out_seconds and round(c.out_seconds * sfps) or src.frames
    if not en then
      e = "Resolve did not report the clip length; give out_seconds or duration_seconds"
    elseif src.frames and en > src.frames then
      warnings[#warnings + 1] = string.format("out point %d is past the end of the source (%d frames); used the end", en, src.frames)
      en = src.frames
    end
    if not e and en <= s then e = string.format("empty range: source frames %d-%d", s, en) end
    local kinds = c.media == "video" and {{"video"}} or c.media == "audio" and {{"audio"}} or {{"video", "audio"}}
    if not e and c.track then
      for _, k in ipairs(kinds) do
        if not ensure_track(k, c.track) then e = "Resolve could not add " .. k .. " track " .. c.track end
      end
    end
    if not e then
      local info = {{mediaPoolItem = mpi, startFrame = s, endFrame = en}}
      if c.media == "video" then info.mediaType = 1 elseif c.media == "audio" then info.mediaType = 2 end
      if c.track then info.trackIndex = c.track end
      if c.record_seconds then info.recordFrame = start + round(c.record_seconds * fps) end
      local expected = round((en - s) / sfps * fps)
      row.requested = {{source_start_frame = s, source_end_frame = en, source_fps = sfps, track = c.track,
        media = c.media, record_frame = info.recordFrame, expected_duration_frames = expected}}
      local got = mp:AppendToTimeline({{info}})
      if type(got) ~= "table" or not got[1] then
        e = "Resolve appended nothing (AppendToTimeline returned " .. tostring(got) .. ")"
      else
        row.items = {{}}
        for _, it in ipairs(got) do
          local d = describe_item(it)
          row.items[#row.items + 1] = d
          local where = d.track or "item"
          if d.duration_frames ~= expected then
            warnings[#warnings + 1] = string.format("%s landed with %d frames, expected %d", where, d.duration_frames, expected)
          end
          if info.recordFrame and d.start_frame ~= info.recordFrame then
            warnings[#warnings + 1] = string.format("%s starts at frame %d, asked for %d", where, d.start_frame, info.recordFrame)
          end
          if c.track and d.track_index and d.track_index ~= c.track then
            warnings[#warnings + 1] = string.format("%s landed on track %d, asked for %d", where, d.track_index, c.track)
          end
        end
        row.ok = true
      end
    end
  end
  if e then row.error = e end
  if #warnings > 0 then row.warnings = warnings end
  if row.ok then appended = appended + 1 else failed = failed + 1 end
  results[#results + 1] = row
end
local tl_info = {{name = timeline:GetName(), created = created or false, fps = fps, start_frame = start,
  end_frame = timeline:GetEndFrame(), duration_seconds = (timeline:GetEndFrame() - start) / fps,
  video_tracks = timeline:GetTrackCount("video"), audio_tracks = timeline:GetTrackCount("audio")}}
if new_fps then tl_info.requested_fps, tl_info.fps_applied = new_fps, fps_applied end
print(string.format("[resolve-pilot] Appended %d / %d clips to '%s'", appended, #clips, timeline:GetName()))
return {{ok = failed == 0, timeline = tl_info, appended = appended, failed = failed,
  tracks_added = tracks_added, clips = results}}
"""


@_snippet
def snippet_remove_clips(
    indices: list[int],
    track_type: str = "video",
    track_index: int = 1,
    names: list[str] | None = None,
    ripple: bool = False,
) -> str:
    """Delete clips from a track of the current timeline by index (as get_state lists them).

    indices are 0-based positions among the track's clips (transitions not counted).
    names, when given, must match the clips at those indices or nothing is deleted.
    ripple closes the gaps. Destructive.
    """
    track_type = _pick(track_type, ("video", "audio"), "track type")
    idx = [int(i) for i in indices]
    if not idx:
        raise ValueError("indices is empty")
    if len(set(idx)) != len(idx) or min(idx) < 0:
        raise ValueError("indices must be distinct and >= 0")
    if names is not None and len(names) != len(idx):
        raise ValueError("names must have one entry per index")
    return f"""-- resolve-pilot: remove_clips
{_TIMELINE_PREAMBLE}{_CLIP_ITEMS}{_TIMELINE_FPS}local indices, names = {to_lua(idx)}, {to_lua(names)}
local track_type, track_index, ripple = {to_lua(track_type)}, {int(track_index)}, {to_lua(bool(ripple))}
local start = timeline:GetStartFrame()
local function try(obj, method, ...)
  local f = obj and obj[method]
  if type(f) ~= "function" then return nil end
  local ok, v = pcall(f, obj, ...)
  if ok then return v end
  return nil
end
local items = clip_items(timeline, track_type, track_index)
local targets, removed = {{}}, {{}}
for n, i in ipairs(indices) do
  local it = items[i + 1]
  if not it then
    return {{ok = false, error = string.format("%s %d has %d clips (indices 0-%d); index %d does not exist. " ..
      "Nothing was deleted.", track_type, track_index, #items, #items - 1, i)}}
  end
  if names and names[n] ~= it:GetName() then
    return {{ok = false, error = string.format("clip %d is '%s', not '%s'; the timeline changed. Read it again " ..
      "with get_resolve_state. Nothing was deleted.", i, tostring(it:GetName()), names[n])}}
  end
  targets[#targets + 1] = it
  removed[#removed + 1] = {{index = i, name = it:GetName(), start_seconds = (it:GetStart() - start) / fps,
    end_seconds = (it:GetEnd() - start) / fps}}
end
-- DeleteClips only works on the Edit page (measured on 21.0.4).
local page = try(resolve, "GetCurrentPage")
if page ~= "edit" then resolve:OpenPage("edit") end
local ok = timeline:DeleteClips(targets, ripple)
if page and page ~= "edit" then resolve:OpenPage(page) end
local after = clip_items(timeline, track_type, track_index)
local deleted = #items - #after
print(string.format("[resolve-pilot] Deleted %d / %d clips from %s %d", deleted, #targets, track_type, track_index))
local out = {{ok = (ok and deleted == #targets) and true or false, deleted = deleted, requested = #targets,
  removed = removed, ripple = ripple, track = track_type .. " " .. track_index, clips_before = #items,
  clips_after = #after, timeline = timeline:GetName(),
  timeline_duration_seconds = (timeline:GetEndFrame() - start) / fps}}
if not out.ok then
  out.error = "DeleteClips returned " .. tostring(ok) .. " and " .. deleted .. " of " .. #targets ..
    " clips are gone. Check the timeline with get_resolve_state."
end
return out
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
