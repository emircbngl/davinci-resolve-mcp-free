---
name: resolve-live-control
description: Control a running DaVinci Resolve directly — Free edition included — through the resolve-pilot Lua bridge. Covers starting the bridge, the read → act → verify loop, writing run_lua scripts, and a cheat sheet of the Resolve Lua API. Use whenever a request needs something the dedicated tools don't cover, or before writing any run_lua code.
---

# resolve-live-control

Resolve Free has no external scripting API, but Lua typed into its Console (or started from Workspace › Scripts) has full access to the project. The resolve-pilot **bridge** is a small Lua loop the user starts once; afterwards you can read and change the open project yourself.

## 1. Get connected

1. `resolve_pilot_status` → `channel`.
   - `studio`: Resolve Studio's API is reachable; dedicated tools already work. `run_lua` still needs the bridge.
   - `bridge`: connected.
   - `manual`: not connected.
2. To connect: `setup_bridge(action="install")`, then tell the user:
   > In Resolve choose **Workspace › Scripts › ResolvePilotBridge** (restart Resolve if it is not listed yet). Keep Resolve open.

   Alternative without installing: `setup_bridge(action="show_script")` and ask the user to paste it into **Workspace › Console** (Lua tab) and press Enter.
3. Confirm with `setup_bridge(action="status")` → `running: true`.

The bridge exits after 60 idle minutes (configurable), when a newer bridge starts, or on `setup_bridge(action="stop")`. If calls fail with "bridge is not running", ask the user to start it again.

## 2. Work in a read → act → verify loop

1. **Read**: `get_resolve_state(include_clips=true, include_markers=true)`.
2. **Act** with a dedicated tool when one exists — `import_media`, `append_clips`, `remove_clips`, `import_timeline`, `add_markers`, `set_clip_color`, `apply_lut`, `queue_render`, `open_page`, `set_clip_properties` (Inspector values, clip enable), `set_tracks` (add / rename / mute / lock tracks), `export_timeline` — they return structured results on every channel.
3. **Verify**: read the state again and compare with what you intended. Never report success you have not seen. On macOS, `screenshot_resolve` shows the Resolve window itself (dialogs, the viewer, which page is open).

### Building and changing the timeline live

Each `import_timeline` creates a *new* timeline. To change the open one:

- **Add shots**: `append_clips(clips=[{"source": "/abs/drone.mov", "in_seconds": 12, "out_seconds": 17, "track": 2, "media": "video", "record_seconds": 30}])`. Ranges are in *source* seconds (Resolve converts them with the clip's own fps; the out point is exclusive). Omit `record_seconds` to append after the track's last clip; omit `track` for track 1; `media` picks video, audio or both. Files not in the media pool are imported on the fly.
- **New timeline**: `append_clips(new_timeline="Cut 2", clips=[...])` (clips may be empty; an existing name is refused). `timeline="Cut 2"` appends to an existing one and makes it current.
- **Organize media first** (optional): `import_media(paths=[...], bin="B-roll/Day 1")` returns each file's `media_pool_id`, usable in `append_clips` instead of `source`. Files already in that bin are reused.
- **Remove**: read the track with `get_resolve_state(include_clips=true, track_type=..., track_index=...)`, ask the user, then `remove_clips(indices=[3], names=["the name shown at 3"], ripple=false)`. Mismatched names delete nothing. Check linked audio tracks afterwards.
- **Check the result**: every appended clip comes back with the items that landed (`track`, `start_seconds`, `end_seconds`, `duration_frames`) and `warnings` where Resolve did something other than asked (length off by a frame, another position or track). Then `timeline_frames` to look at it.

## 3. run_lua

```
run_lua(code="""
local tl = rp.timeline()
local out = {}
for i, item in ipairs(tl:GetItemListInTrack("video", 1)) do
  out[#out + 1] = {i = i, name = item:GetName(), frames = item:GetDuration()}
end
return out
""")
```
- `return` a value: tables come back as JSON in `result`. `print()` output is in `output`.
- Helpers: `rp.project()`, `rp.timeline()`, `rp.media_pool()`, `rp.fps()` (they raise clear errors when nothing is open), `rp.json(value)`.
- Globals you assign (without `local`) are kept for later requests in the bridge's own table; they never replace Resolve's real globals. Prefer `local`.
- Output is capped at 256 KB (`output_truncated`) and the encoded result at about 4 MB (`result_truncated`); return summaries, not whole media pools.
- Library snippets: `run_lua(snippet="relink_media", args={"folder": "/Volumes/Footage"})` (see `list_lua_snippets`).
- Errors come back as `ok: false` with a Lua traceback; fix and retry.
- Long jobs (big imports, renders): raise `timeout_seconds`. While one request runs, the bridge reports *busy*; other calls wait their turn within their own timeout and then fail with "still running a previous request" — wait and retry, don't restart the bridge.

**Safety.** `run_lua` can change or delete anything in the project. Read first, keep scripts small, and before destructive steps (DeleteClips, DeleteTimelines, DeleteMarkersByColor, replacing grades) ask the user and suggest saving: `rp.resolve():GetProjectManager():SaveProject()`.

## 4. Lua essentials for Resolve

- Methods use a colon: `tl:GetName()`. Tables are 1-based: `for i, x in ipairs(list) do … end`, length `#list`.
- `..` concatenates, `~=` is not-equal, `nil` is none, `local` for variables.
- Seconds → frames: `math.floor(seconds * rp.fps() + 0.5)`.
- **Timeline items** report absolute frames (`GetStart()` includes the start frame, e.g. 86400 = 01:00:00:00 at 24 fps). **Markers** use offsets from the timeline start.
- A method that returns `nil`/`false` may not exist in this Resolve version: `print(type(tl.SomeMethod))`.

## 5. API cheat sheet (most-used calls)

**Resolve** (`resolve` or `rp.resolve()`)
- `GetProjectManager()`, `OpenPage("media"|"cut"|"edit"|"fusion"|"color"|"fairlight"|"deliver")`, `GetCurrentPage()`, `GetProductName()`, `GetVersionString()`, `GetMediaStorage()`

**ProjectManager**
- `GetCurrentProject()`, `SaveProject()`, `CreateProject(name)`, `LoadProject(name)`, `GetProjectListInCurrentFolder()`

**Project** (`rp.project()`)
- `GetName()`, `GetSetting(key)`, `SetSetting(key, value)` — keys like `timelineFrameRate`, `timelineResolutionWidth`, `timelineResolutionHeight`
- `GetMediaPool()`, `GetTimelineCount()`, `GetTimelineByIndex(i)` (1-based), `GetCurrentTimeline()`, `SetCurrentTimeline(tl)`
- Render: `GetRenderPresetList()`, `LoadRenderPreset(name)`, `SetRenderSettings({TargetDir = "/path", CustomName = "name"})`, `AddRenderJob()`, `StartRendering(jobId)`, `IsRenderingInProgress()`, `GetRenderJobStatus(jobId)`

**MediaPool** (`rp.media_pool()`)
- `GetRootFolder()`, `GetCurrentFolder()`, `SetCurrentFolder(folder)`, `AddSubFolder(parent, name)`
- `ImportMedia({"/abs/a.mov", "/abs/b.wav"})` → list of MediaPoolItems
- `CreateEmptyTimeline(name)`, `CreateTimelineFromClips(name, {item1, item2})`
- `AppendToTimeline({{mediaPoolItem = item, startFrame = 0, endFrame = 48, trackIndex = 1, mediaType = 1, recordFrame = 86400}})` — frames of the source clip at its own fps, `endFrame` exclusive, appended to the current timeline; `mediaType` 1 = video only, 2 = audio only; `recordFrame` is a timeline frame. Returns the new TimelineItems. Prefer the `append_clips` tool
- `ImportMedia` takes plain path strings; a `{FilePath = ...}` table imports nothing
- `ImportTimelineFromFile(path, {timelineName = "Cut", importSourceClips = true})`
- `DeleteClips({items})`, `DeleteTimelines({tl})` — destructive

**Folder**: `GetClipList()`, `GetSubFolderList()`, `GetName()`

**MediaPoolItem**: `GetName()`, `GetClipProperty("File Path"|"FPS"|"Frames"|"Duration"|"Resolution")`, `SetClipColor(color)`, `AddMarker(frame, color, name, note, duration)`, `GetMarkers()`

**Timeline** (`rp.timeline()`)
- `GetName()`, `SetName(name)`, `GetStartFrame()`, `GetEndFrame()`, `GetStartTimecode()`, `GetCurrentTimecode()`, `SetCurrentTimecode("01:00:10:00")`, `GetSetting(key)`
- `GetTrackCount("video"|"audio"|"subtitle")`, `GetItemListInTrack("video", 1)`, `AddTrack("video")`, `SetTrackName("video", 2, "B-roll")`, `SetTrackEnable("audio", 1, false)`
- `AddMarker(offset, color, name, note, duration, customData)`, `GetMarkers()`, `DeleteMarkerAtFrame(offset)`, `DeleteMarkersByColor("Red"|"All")`
- `DeleteClips({items}, ripple)`, `DuplicateTimeline(name)`, `Export(path, resolve.EXPORT_FCPXML_1_10)`, `GetCurrentVideoItem()`

**TimelineItem**
- `GetName()`, `GetStart()`, `GetEnd()`, `GetDuration()`, `GetLeftOffset()` (source in, frames), `GetMediaPoolItem()`
- `SetClipColor(color)`, `GetClipColor()`, `ClearClipColor()`, `AddMarker(offsetInClip, color, name, note, duration)`
- `SetLUT(nodeIndex, path)`, `SetCDL({NodeIndex = "1", Slope = "1 1 1", Offset = "0 0 0", Power = "1 1 1", Saturation = "1"})`
- `GetProperty(key)` / `SetProperty(key, value)` — `ZoomX`, `ZoomY`, `Pan`, `Tilt`, `RotationAngle`, `CropLeft`, `CropRight`, `CropTop`, `CropBottom`, `Opacity`
- `SetClipEnabled(false)`

Colors — markers: Blue, Cyan, Green, Yellow, Red, Pink, Purple, Fuchsia, Rose, Lavender, Sky, Mint, Lemon, Sand, Cocoa, Cream. Clips: Orange, Apricot, Yellow, Lime, Olive, Green, Teal, Navy, Blue, Purple, Violet, Pink, Tan, Beige, Brown, Chocolate.

## 6. Recipes

Append source ranges to the current timeline (the `append_clips` tool does this with read-back; use Lua only for variations it does not cover). `startFrame`/`endFrame` count frames of the *source clip*, at the clip's own frame rate (not the timeline's); `endFrame` is exclusive:
```lua
local mp = rp.media_pool()
local items = mp:ImportMedia({"/abs/interview.mov"})
local sfps = tonumber(items[1]:GetClipProperty("FPS")) or rp.fps()
local ok = mp:AppendToTimeline({
  {mediaPoolItem = items[1], startFrame = math.floor(12.0 * sfps), endFrame = math.floor(18.5 * sfps)},
})
return ok and #ok or 0
```

Give every source file its own clip color on V1:
```lua
local palette = {"Orange", "Teal", "Violet", "Lime", "Navy", "Tan"}
local colors, n = {}, 0
for _, it in ipairs(rp.timeline():GetItemListInTrack("video", 1)) do
  local mpi = it:GetMediaPoolItem()
  local path = mpi and mpi:GetClipProperty("File Path") or "?"
  if not colors[path] then n = n + 1; colors[path] = palette[(n - 1) % #palette + 1] end
  it:SetClipColor(colors[path])
end
return colors
```

## Failure modes

- "bridge is not running" → the user stopped it, Resolve was restarted, or 60 idle minutes passed; ask them to start it again.
- "took the request but did not answer" → the Lua is still running (a long import/render). Wait, then `get_resolve_state`.
- The Mac App Store build of Resolve is sandboxed and cannot reach the bridge folder; use the download from blackmagicdesign.com.
