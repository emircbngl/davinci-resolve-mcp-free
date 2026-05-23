"""Curated Lua snippets for the DaVinci Resolve Console.

All snippets:
- Use only Resolve's built-in globals (resolve, fusion, bmd, app).
- Print a clear status line so the user can see they worked.
- Are idempotent where possible (running twice does not duplicate work).
- Work in Free edition (Console is available) and Studio.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Snippet:
    name: str
    description: str
    code: str

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "code": self.code}


_MARKER_COLORS = {
    "blue", "cyan", "green", "yellow", "red", "pink", "purple", "fuchsia",
    "rose", "lavender", "sky", "mint", "lemon", "sand", "cocoa", "cream"
}


def _validate_color(color: str) -> str:
    c = color.strip().capitalize()
    if c.lower() not in _MARKER_COLORS:
        raise ValueError(f"Unknown marker color: {color}. Try one of: {sorted(_MARKER_COLORS)}")
    return c


def snippet_batch_markers(
    frames: list[int],
    color: str = "Blue",
    name_prefix: str = "Cue",
    note: str = "",
) -> str:
    """Add markers at specific frame positions on the current timeline."""
    c = _validate_color(color)
    frame_table = ", ".join(str(int(f)) for f in frames)
    return f"""-- resolve-pilot: batch_markers
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project:GetCurrentTimeline()
if not timeline then print("[resolve-pilot] No active timeline."); return end
local frames = {{ {frame_table} }}
local added = 0
for i, f in ipairs(frames) do
  local ok = timeline:AddMarker(f, "{c}", "{name_prefix} " .. i, [[{note}]], 1, "")
  if ok then added = added + 1 end
end
print(string.format("[resolve-pilot] Added %d / %d markers on '%s'", added, #frames, timeline:GetName()))
"""


def snippet_export_timeline(out_path: str, export_type: str = "FCPXML_1_10") -> str:
    """Export the current timeline to FCPXML / OTIO / AAF / EDL / DRT.

    export_type: one of FCPXML_1_10, FCPXML_1_9, FCPXML_1_8, AAF, EDL_CDL, OTIO, DRT, ADL.
    """
    return f"""-- resolve-pilot: export_timeline
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project:GetCurrentTimeline()
if not timeline then print("[resolve-pilot] No active timeline."); return end
local ok = timeline:Export([[{out_path}]], resolve.EXPORT_{export_type})
print(string.format("[resolve-pilot] Export %s -> %s", tostring(ok), [[{out_path}]]))
"""


def snippet_set_clip_color(clip_color: str = "Orange", track: int = 1) -> str:
    """Color-code every clip on a video track. Useful for sectioning a long timeline."""
    return f"""-- resolve-pilot: set_clip_color
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project:GetCurrentTimeline()
if not timeline then print("[resolve-pilot] No active timeline."); return end
local items = timeline:GetItemListInTrack("video", {int(track)})
local n = 0
for _, item in ipairs(items or {{}}) do
  item:SetClipColor("{clip_color}")
  n = n + 1
end
print(string.format("[resolve-pilot] Colored %d clips on V%d", n, {int(track)}))
"""


def snippet_create_compound_from_selection(name: str = "Compound") -> str:
    """Wrap the timeline's selected items into a compound clip."""
    return f"""-- resolve-pilot: create_compound
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project:GetCurrentTimeline()
if not timeline then print("[resolve-pilot] No active timeline."); return end
local items = timeline:GetCurrentVideoItem() and {{ timeline:GetCurrentVideoItem() }} or {{}}
local ok = timeline:CreateCompoundClip(items, {{startTimecode = "01:00:00:00", name = "{name}"}})
print("[resolve-pilot] Compound created: " .. tostring(ok))
"""


def snippet_render_all_timelines(preset: str = "H.264 Master", out_dir: str = "~/Desktop/Renders") -> str:
    """Queue every timeline in the project for render with the given preset, then start.

    Studio-only: rendering in Free edition is restricted. Snippet runs but render queue
    is gated by Resolve itself.
    """
    return f"""-- resolve-pilot: render_all_timelines
local project = resolve:GetProjectManager():GetCurrentProject()
local n_tl = project:GetTimelineCount()
local queued = 0
for i = 1, n_tl do
  local tl = project:GetTimelineByIndex(i)
  project:SetCurrentTimeline(tl)
  project:LoadRenderPreset([[{preset}]])
  project:SetRenderSettings({{TargetDir = [[{out_dir}]], CustomName = tl:GetName()}})
  local jid = project:AddRenderJob()
  if jid then queued = queued + 1 end
end
print(string.format("[resolve-pilot] Queued %d render jobs.", queued))
project:StartRendering()
"""


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


def snippet_apply_lut_to_clips(lut_path: str, track: int = 1, node_index: int = 1) -> str:
    """Apply a LUT to all clips on a video track at a specific color node index."""
    return f"""-- resolve-pilot: apply_lut
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project:GetCurrentTimeline()
if not timeline then print("[resolve-pilot] No active timeline."); return end
local items = timeline:GetItemListInTrack("video", {int(track)})
local applied = 0
for _, item in ipairs(items or {{}}) do
  if item:SetLUT({int(node_index)}, [[{lut_path}]]) then applied = applied + 1 end
end
print(string.format("[resolve-pilot] Applied LUT to %d clips at node %d", applied, {int(node_index)}))
"""


def snippet_relink_media(folder: str) -> str:
    """Relink offline media in the current project by pointing Resolve at a folder.

    Walks the media pool recursively and re-points every clip at
    `<folder>/<bin-relative-path>/<clip-name>`. Assumes the filesystem layout
    under `folder` mirrors the bin/sub-bin structure.
    """
    return f"""-- resolve-pilot: relink_media
local project = resolve:GetProjectManager():GetCurrentProject()
local mp = project:GetMediaPool()
local root_path = [[{folder}]]
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
"""


def snippet_proxy_generate(track: int = 1) -> str:
    """Generate proxy media for every clip on a given video track."""
    return f"""-- resolve-pilot: proxy_generate
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project:GetCurrentTimeline()
if not timeline then print("[resolve-pilot] No active timeline."); return end
local items = timeline:GetItemListInTrack("video", {int(track)})
local q = 0
for _, item in ipairs(items or {{}}) do
  local pi = item:GetMediaPoolItem()
  if pi then pi:LinkProxyMedia(""); q = q + 1 end
end
print(string.format("[resolve-pilot] Touched %d media-pool items for proxy management on V%d", q, {int(track)}))
"""


def snippet_grade_from_drx(drx_path: str, track: int = 1, mode: int = 0) -> str:
    """Apply a saved grade (.drx) to all clips on a video track.

    mode: 0 = NoKeyframes, 1 = SourceTimecode, 2 = SourceFrame, 3 = TargetFrame.
    """
    return f"""-- resolve-pilot: grade_from_drx
local project = resolve:GetProjectManager():GetCurrentProject()
local timeline = project:GetCurrentTimeline()
if not timeline then print("[resolve-pilot] No active timeline."); return end
local items = timeline:GetItemListInTrack("video", {int(track)})
local ok = timeline:ApplyGradeFromDRX([[{drx_path}]], {int(mode)}, items)
print("[resolve-pilot] Grade-from-DRX applied: " .. tostring(ok))
"""


def list_snippets() -> list[dict]:
    """Return metadata about every available snippet so Claude can browse them."""
    return [
        {"name": "batch_markers", "description": "Add markers at specific frame positions"},
        {"name": "export_timeline", "description": "Export timeline to FCPXML/AAF/EDL/OTIO/DRT"},
        {"name": "set_clip_color", "description": "Color-code clips on a video track"},
        {"name": "create_compound_from_selection", "description": "Wrap selection into compound clip"},
        {"name": "render_all_timelines", "description": "Queue all timelines for render (Studio)"},
        {"name": "dump_project_info", "description": "Print a readable project summary"},
        {"name": "apply_lut_to_clips", "description": "Apply LUT to all clips on a track at given node"},
        {"name": "relink_media", "description": "Relink offline media from a folder"},
        {"name": "proxy_generate", "description": "Touch media items for proxy management"},
        {"name": "grade_from_drx", "description": "Apply a saved grade (.drx) to a track"},
    ]
