-- Minimal stand-in for DaVinci Resolve's Lua scripting API, used by the tests to
-- run the real bridge and the real snippets under LuaJIT (Resolve embeds LuaJIT).
-- Only the calls resolve-pilot makes are modelled.

local function basename(path)
  return (path:match("([^/\\]+)$") or path):gsub("%.[^.]*$", "")
end

-- MediaPoolItem ----------------------------------------------------------------
-- Clip properties come back as strings, like the real API. Files listed in
-- mock.media_info get that fps / length; anything else is a 25 fps, 250-frame clip
-- (deliberately not the 24 fps timeline rate, so source-vs-timeline fps mix-ups show).
local Mpi = {}
Mpi.__index = Mpi
local next_id = 0

function Mpi.new(path)
  next_id = next_id + 1
  local info = (mock and mock.media_info or {})[path] or {}
  local m = setmetatable({ id = string.format("mpi-%04d", next_id), path = path, name = (path:match("([^/\\]+)$") or path),
    fps = info.fps or 25, frames = info.frames or 250, audio = info.audio ~= false,
    video = info.video ~= false }, Mpi)
  if path:lower():match("%.srt$") then
    -- Subtitle items: the cues come from the file, like Resolve reads them.
    m.subtitle, m.audio, m.video, m.cues = true, false, false, {}
    local f = io.open(path, "rb")
    local text = f and f:read("*a") or ""
    if f then f:close() end
    local function sec(h, mi, s, ms) return h * 3600 + mi * 60 + s + ms / 1000 end
    for a1, a2, a3, a4, b1, b2, b3, b4, body in text:gmatch(
        "(%d+):(%d+):(%d+),(%d+)%s*%-%->%s*(%d+):(%d+):(%d+),(%d+)\r?\n(.-)\r?\n\r?\n") do
      m.cues[#m.cues + 1] = { sec(a1, a2, a3, a4), sec(b1, b2, b3, b4), body }
    end
  end
  return m
end
function Mpi:GetName() return self.name end
function Mpi:GetUniqueId() return self.id end
function Mpi:GetClipProperty(key)
  if key == "File Path" then return self.path end
  if key == "Type" then
    if self.compound then return "Compound" end
    return self.subtitle and "Subtitle" or (self.video and self.audio and "Video + Audio")
      or (self.video and "Video" or "Audio")
  end
  if key == "FPS" then return self.video and tostring(self.fps) or nil end
  if key == "Frames" then return tostring(self.frames) end
  if key == nil then return { ["File Path"] = self.path, FPS = tostring(self.fps), Frames = tostring(self.frames) } end
  return nil
end

-- TimelineItem ---------------------------------------------------------------
local Item = {}
Item.__index = Item

-- Inspector defaults as a fresh clip reports them. Enum values are the resolve.*
-- constants below, deliberately not 0..n, so a snippet that assumes ordinals fails.
local function default_props()
  return { Pan = 0, Tilt = 0, ZoomX = 1, ZoomY = 1, ZoomGang = true, RotationAngle = 0,
    AnchorPointX = 0, AnchorPointY = 0, Pitch = 0, Yaw = 0, FlipX = false, FlipY = false,
    CropLeft = 0, CropRight = 0, CropTop = 0, CropBottom = 0, CropSoftness = 0, CropRetain = false,
    Distortion = 0, Opacity = 100, CompositeMode = 100, DynamicZoomEase = 200, RetimeProcess = 300,
    MotionEstimation = 400, Scaling = 500, ResizeFilter = 600 }
end

function Item.new(name, start, dur, path, mpi)
  return setmetatable({ name = name, start = start, dur = dur, path = path, color = "", mpi = mpi,
    left = 0, props = default_props(), enabled = true }, Item)
end
function Item:GetName() return self.name end
function Item:GetStart() return self.start end
function Item:GetEnd() return self.start + self.dur end
function Item:GetDuration() return self.dur end
function Item:GetLeftOffset() return self.left end
function Item:GetClipColor() return self.color end
function Item:SetClipColor(c) self.color = c; return true end
function Item:SetLUT(node, path) self.lut = { node = node, path = path }; return true end
function Item:GetTrackTypeAndIndex() return { self.track_type, self.track_index } end
function Item:GetMediaPoolItem()
  if self.mpi then return self.mpi end
  local path = self.path
  return { GetClipProperty = function(_, key) if key == "File Path" then return path end end }
end
-- GetProperty() returns every property; GetProperty(key) one value (nil if unknown).
function Item:GetProperty(key)
  if key == nil then
    local copy = {}
    for k, v in pairs(self.props) do copy[k] = v end
    return copy
  end
  return self.props[key]
end
-- Unknown keys and wrong value types are refused, like Resolve does (it returns false).
-- Fractions come back slightly off, as float storage does, so read-back needs a tolerance.
function Item:SetProperty(key, value)
  local cur = self.props[key]
  if cur == nil or type(cur) ~= type(value) then return false end
  if type(value) == "number" and value ~= math.floor(value) then value = value + 1e-7 end
  self.props[key] = value
  return true
end
function Item:GetClipEnabled() return self.enabled end
function Item:SetClipEnabled(on)
  if type(on) ~= "boolean" then return false end
  self.enabled = on
  return true
end

-- Transitions come back from GetItemListInTrack too (Resolve 20.3): no media pool
-- item and no transform properties.
local Transition = setmetatable({}, { __index = Item })
Transition.__index = Transition
function Transition.new(start, dur)
  return setmetatable({ name = "Cross Dissolve", start = start, dur = dur, color = "" }, Transition)
end
function Transition:GetMediaPoolItem() return nil end
function Transition:GetProperty() return {} end

-- Timeline -------------------------------------------------------------------
local Timeline = {}
Timeline.__index = Timeline

function Timeline.new(name, clips)
  local tl = setmetatable({ name = name, start = 86400, markers = {}, settings = {},
    tracks = { video = { {} }, audio = { {} }, subtitle = {} } }, Timeline)
  local pos = tl.start
  for _, c in ipairs(clips or {}) do
    local it = Item.new(c[1], pos, c[2], c[3])
    it.track_type, it.track_index = "video", 1
    table.insert(tl.tracks.video[1], it)
    pos = pos + c[2]
  end
  return tl
end
function Timeline:GetName() return self.name end
function Timeline:GetStartFrame() return self.start end
function Timeline:GetEndFrame()
  local last = self.start
  for _, kind in ipairs({ "video", "audio", "subtitle" }) do
    for _, track in ipairs(self.tracks[kind]) do
      for _, it in ipairs(track) do last = math.max(last, it:GetEnd()) end
    end
  end
  return last
end
-- "" = follows the project settings. The frame rate can only change while empty.
function Timeline:GetSetting(key) return self.settings[key] or "" end
function Timeline:SetSetting(key, value)
  if key == "timelineFrameRate" and self:GetEndFrame() > self.start then return false end
  self.settings[key] = value
  return true
end
function Timeline:AddTrack(kind)
  if not self.tracks[kind] then return false end
  table.insert(self.tracks[kind], {})
  return true
end
-- Deletes the given items; ripple pulls later items on the same track left.
function Timeline:DeleteClips(items, ripple)
  if resolve.page ~= "edit" then return false end -- Edit page only (measured on 21.0.4)
  local doomed = {}
  for _, it in ipairs(items or {}) do doomed[it] = true end
  for _, kind in ipairs({ "video", "audio", "subtitle" }) do
    for _, track in ipairs(self.tracks[kind]) do
      local i = 1
      while i <= #track do
        local it = track[i]
        if doomed[it] then
          table.remove(track, i)
          if ripple then
            for j = i, #track do track[j].start = track[j].start - it.dur end
          end
        else
          i = i + 1
        end
      end
    end
  end
  return true
end
function Timeline:GetTrackCount(kind) return #self.tracks[kind] end
-- Track name / enable / lock, kept per track; out-of-range indices are refused.
local TRACK_PREFIX = { video = "Video", audio = "Audio", subtitle = "Subtitle" }
function Timeline:_track(kind, i)
  if not (self.tracks[kind] and self.tracks[kind][i]) then return nil end
  self.track_meta = self.track_meta or {}
  local key = kind .. i
  self.track_meta[key] = self.track_meta[key] or { name = TRACK_PREFIX[kind] .. " " .. i,
    enabled = true, locked = false }
  return self.track_meta[key]
end
function Timeline:GetTrackName(kind, i) local t = self:_track(kind, i); return t and t.name end
function Timeline:GetIsTrackEnabled(kind, i) local t = self:_track(kind, i); return t and t.enabled end
function Timeline:GetIsTrackLocked(kind, i) local t = self:_track(kind, i); return t and t.locked end
function Timeline:SetTrackName(kind, i, name)
  local t = self:_track(kind, i)
  if not t or type(name) ~= "string" then return false end
  t.name = name
  return true
end
function Timeline:SetTrackEnable(kind, i, on)
  local t = self:_track(kind, i)
  if not t or type(on) ~= "boolean" then return false end
  t.enabled = on
  return true
end
function Timeline:SetTrackLock(kind, i, on)
  local t = self:_track(kind, i)
  if not t or type(on) ~= "boolean" then return false end
  t.locked = on
  return true
end
local AUDIO_SUBTYPES = { mono = true, stereo = true, ["5.1"] = true, ["7.1"] = true }
function Timeline:AddTrack(kind, sub)
  if not self.tracks[kind] then return false end
  if kind == "audio" and sub ~= nil and not AUDIO_SUBTYPES[sub] then return false end
  table.insert(self.tracks[kind], {})
  return true
end
function Timeline:GetItemListInTrack(kind, index) return (self.tracks[kind] or {})[index] or {} end
function Timeline:AddMarker(frame, color, name, note, duration, data)
  if self.markers[frame] then return false end
  self.markers[frame] = { color = color, name = name, note = note, duration = duration, customData = data }
  return true
end
function Timeline:GetMarkers() return self.markers end
function Timeline:DeleteMarkerByCustomData(data)
  for frame, m in pairs(self.markers) do
    if m.customData == data then self.markers[frame] = nil; return true end
  end
  return false
end
function Timeline:GetStartTimecode() return "01:00:00:00" end
-- Writes a small file naming the format. AAF and EDL need a subtype, as in Resolve.
-- mock.export_lies: return true and write nothing; mock.export_needs_current: only the
-- current timeline exports.
function Timeline:Export(path, kind, sub)
  if type(kind) ~= "number" then return false end
  if (kind == resolve.EXPORT_AAF or kind == resolve.EXPORT_EDL) and sub == nil then return false end
  if mock.export_lies then return true end
  if mock.export_needs_current and mock.project:GetCurrentTimeline() ~= self then return false end
  local f = io.open(path, "wb")
  if not f then return false end
  f:write("timeline ", self.name, " kind ", tostring(kind), " sub ", tostring(sub), "\n")
  f:close()
  self.exported = (self.exported or 0) + 1
  return true
end
function Timeline:GetCurrentVideoItem() return self.tracks.video[1][1] end
function Timeline:GetCurrentTimecode() return self.playhead or "01:00:00:00" end
function Timeline:SetCurrentTimecode(tc) self.playhead = tc; return true end

-- MediaPool / Project ------------------------------------------------------------
local Project = {}
Project.__index = Project

local MediaPool = {}
MediaPool.__index = MediaPool

function MediaPool:ImportTimelineFromFile(path, opts)
  local f = io.open(path, "r")
  if not f then return nil end
  f:close()
  local tl = Timeline.new((opts or {}).timelineName or basename(path), { { "imported", 48, path } })
  table.insert(self.project.timelines, tl)
  return tl
end

-- Folders ------------------------------------------------------------------------
local Folder = {}
Folder.__index = Folder
function Folder.new(name) return setmetatable({ name = name, clips = {}, subs = {} }, Folder) end
function Folder:GetName() return self.name end
function Folder:GetClipList() local out = {} for i, c in ipairs(self.clips) do out[i] = c end return out end
function Folder:GetSubFolderList() local out = {} for i, f in ipairs(self.subs) do out[i] = f end return out end

function MediaPool:GetRootFolder() return self.root end
function MediaPool:GetCurrentFolder() return self.current end
function MediaPool:SetCurrentFolder(f) if not f then return false end self.current = f; return true end
function MediaPool:AddSubFolder(parent, name)
  local f = Folder.new(name)
  table.insert(parent.subs, f)
  return f
end
-- Plain path strings only: a {FilePath = ...} table imports nothing (measured on 21.0.4).
-- Missing files are skipped. Always imports into the current folder.
function MediaPool:ImportMedia(list)
  local out = {}
  for _, path in ipairs(list or {}) do
    if type(path) == "string" then
      local f = io.open(path, "rb")
      if f then
        f:close()
        local mpi = Mpi.new(path)
        table.insert(self.current.clips, mpi)
        out[#out + 1] = mpi
      end
    end
  end
  self.project.imports = (self.project.imports or 0) + #out
  return out
end
function MediaPool:CreateEmptyTimeline(name)
  for _, t in ipairs(self.project.timelines) do if t.name == name then return nil end end
  local tl = Timeline.new(name)
  table.insert(self.project.timelines, tl)
  return tl -- does not make it current: callers must not rely on that
end
-- clipInfo: startFrame / endFrame are source frames, endFrame EXCLUSIVE (measured on
-- 21.0.4); recordFrame is an absolute timeline frame; omitted = after the track's last
-- item. Items land at the timeline fps, keeping their real-time length.
function MediaPool:AppendToTimeline(infos)
  local tl = self.project.current
  if not tl then return {} end
  local fps = tonumber(tl:GetSetting("timelineFrameRate")) or tonumber(self.project.settings.timelineFrameRate)
  local out = {}
  for _, info in ipairs(infos or {}) do
    local mpi = info.mediaPoolItem
    if type(mpi) ~= "table" or getmetatable(mpi) ~= Mpi then return {} end
    if mpi.subtitle then
      -- Resolve 20.3 crashes when a subtitle item comes with a frame range or media type.
      if info.startFrame or info.endFrame or info.mediaType or info.recordFrame then
        error("MOCK: Resolve would crash here (subtitle item with a range)")
      end
      local track = tl.tracks.subtitle[1]
      if not track then return {} end -- no subtitle track: nothing is added
      -- Captions land at the playhead (measured on 20.3), not at the timeline start.
      local at = tl.start
      local h, mi, sec, ff = (tl.playhead or ""):match("^(%d+):(%d+):(%d+)[:;](%d+)$")
      if h then at = ((h * 60 + mi) * 60 + sec) * math.floor(fps + 0.5) + ff end
      for _, cue in ipairs(mpi.cues) do
        local it = Item.new(cue[3], at + math.floor(cue[1] * fps + 0.5),
          math.floor((cue[2] - cue[1]) * fps + 0.5), mpi.path, mpi)
        it.track_type, it.track_index = "subtitle", 1
        table.insert(track, it)
      end
      out[#out + 1] = mpi
      goto continue
    end
    local s, e = info.startFrame or 0, info.endFrame or mpi.frames
    if s < 0 or e > mpi.frames or e <= s then return {} end
    local dur = math.floor((e - s) / mpi.fps * fps + 0.5)
    local kinds = {}
    if info.mediaType ~= 2 and mpi.video then kinds[#kinds + 1] = "video" end
    if info.mediaType ~= 1 and mpi.audio then kinds[#kinds + 1] = "audio" end
    local index = info.trackIndex or 1
    for _, kind in ipairs(kinds) do
      local track = tl.tracks[kind][index]
      if not track then return out end
      local rec = info.recordFrame
      if not rec then
        rec = tl.start
        for _, it in ipairs(track) do rec = math.max(rec, it:GetEnd()) end
      end
      local it = Item.new(mpi.name, rec, dur, mpi.path, mpi)
      it.left, it.track_type, it.track_index = s, kind, index
      table.insert(track, it)
      table.sort(track, function(a, b) return a.start < b.start end)
      out[#out + 1] = it
    end
    ::continue::
  end
  return out
end

function Project.new(name)
  local p = setmetatable({ name = name, page = "edit", render_jobs = {},
    settings = { timelineFrameRate = "24", timelineResolutionWidth = "1920",
      timelineResolutionHeight = "1080" } }, Project)
  p.timelines = { Timeline.new("Main", { { "A", 48, "/media/a.mov" }, { "B", 72, "/media/b.mov" } }) }
  p.current = p.timelines[1]
  p.pool = setmetatable({ project = p, root = Folder.new("Master") }, MediaPool)
  p.pool.current = p.pool.root
  return p
end
function Project:GetName() return self.name end
function Project:GetSetting(key) return self.settings[key] end
function Project:GetTimelineCount() return #self.timelines end
function Project:GetTimelineByIndex(i) return self.timelines[i] end
function Project:GetCurrentTimeline() return self.current end
function Project:SetCurrentTimeline(tl) self.current = tl; return true end
function Project:GetMediaPool() return self.pool end
-- Tables from the real API carry an internal "__flags" key next to the list items.
function Project:GetRenderPresetList() return { "H.264 Master", "YouTube - 1080p", __flags = 4194304 } end
function Project:RefreshLUTList() self.luts_refreshed = true; return true end
function Project:LoadRenderPreset(name)
  for _, p in ipairs(self:GetRenderPresetList()) do
    if p == name then self.preset = name; return true end
  end
  return false
end
function Project:SetRenderSettings(s)
  self.render_settings = self.render_settings or {}
  for k, v in pairs(s) do self.render_settings[k] = v end
  return true
end
-- Render formats map display name -> extension; codecs map name -> code (Resolve 20.3).
function Project:GetRenderFormats() return { MP4 = "mp4", QuickTime = "mov", __flags = 1 } end
function Project:GetRenderCodecs(ext)
  if ext == "mp4" then return { ["H.264"] = "H264", ["H.265"] = "H265", __flags = 1 } end
  if ext == "mov" then return { ["H.264"] = "H264", ["Apple ProRes 422 HQ"] = "ProRes422HQ" } end
  return {}
end
function Project:SetCurrentRenderFormatAndCodec(f, c)
  local ok = false
  for _, code in pairs(self:GetRenderCodecs(f)) do if code == c then ok = true end end
  if ok then self.render_fc = { format = f, codec = c } end
  return ok
end
function Project:AddRenderJob()
  local id = "job-" .. (#self.render_jobs + 1)
  table.insert(self.render_jobs, id)
  local s = self.render_settings or {}
  local fc = self.render_fc or { format = "mov", codec = "H264" }
  local tl = self.current
  self.job_info = self.job_info or {}
  self.job_info[id] = { VideoFormat = fc.format:upper(), VideoCodec = fc.codec, FormatWidth = s.FormatWidth or 1920,
    FormatHeight = s.FormatHeight or 1080, FrameRate = "24", IsExportAudio = s.ExportAudio ~= false,
    MarkIn = s.SelectAllFrames == false and s.MarkIn or tl.start, MarkOut = s.SelectAllFrames == false and s.MarkOut or tl:GetEndFrame() - 1,
    TargetDir = s.TargetDir or "/out", OutputFilename = (s.CustomName or id) .. "." .. fc.format }
  return id
end
function Project:StartRendering() self.rendering = true; return true end
function Project:IsRenderingInProgress() return self.rendering == true end
function Project:GetRenderJobList()
  local out = {}
  for _, id in ipairs(self.render_jobs) do
    local j = { JobId = id, RenderJobName = id, TimelineName = "Main" }
    for k, v in pairs((self.job_info or {})[id] or { TargetDir = "/out", OutputFilename = id .. ".mov" }) do j[k] = v end
    out[#out + 1] = j
  end
  return out
end
function Project:GetRenderJobStatus(id)
  return { JobStatus = self.rendering and "Rendering" or "Ready", CompletionPercentage = self.rendering and 40 or 0,
    EstimatedTimeRemainingInMs = self.rendering and 12000 or nil }
end
-- Copies mock.png_path (a real PNG set by the test) and records the playhead it saw.
function Project:ExportCurrentFrameAsStill(path)
  local src = mock.png_path and io.open(mock.png_path, "rb")
  local f = io.open(path, "wb")
  if not (src and f) then return false end
  f:write(src:read("*a"))
  src:close()
  f:close()
  self.stills = self.stills or {}
  table.insert(self.stills, self.current:GetCurrentTimecode())
  return true
end

-- Resolve ------------------------------------------------------------------------
local project = Project.new("Test Project")
local pm = { GetCurrentProject = function() return project end }

resolve = {
  page = "edit",
  SCALE_USE_PROJECT = 500, SCALE_CROP = 501, SCALE_FIT = 502, SCALE_FILL = 503, SCALE_STRETCH = 504,
  COMPOSITE_NORMAL = 100, COMPOSITE_SCREEN = 105, COMPOSITE_MULTIPLY = 104,
  DYNAMIC_ZOOM_EASE_LINEAR = 200, DYNAMIC_ZOOM_EASE_IN_AND_OUT = 203,
  RETIME_USE_PROJECT = 300, RETIME_NEAREST = 301, RETIME_FRAME_BLEND = 302, RETIME_OPTICAL_FLOW = 303,
  EXPORT_AAF = 1, EXPORT_DRT = 2, EXPORT_EDL = 3, EXPORT_FCP_7_XML = 4, EXPORT_FCPXML_1_8 = 5,
  EXPORT_FCPXML_1_9 = 6, EXPORT_FCPXML_1_10 = 7, EXPORT_TEXT_CSV = 8, EXPORT_TEXT_TAB = 9,
  EXPORT_OTIO = 10, EXPORT_ALE = 11, EXPORT_ALE_CDL = 12,
  EXPORT_NONE = 20, EXPORT_AAF_NEW = 21, EXPORT_AAF_EXISTING = 22, EXPORT_CDL = 23, EXPORT_SDL = 24,
  EXPORT_MISSING_CLIPS = 25,
  MOTION_EST_USE_PROJECT = 400, RESIZE_FILTER_USE_PROJECT = 600, RESIZE_FILTER_LANCZOS = 610,
  GetProjectManager = function() return pm end,
  GetProductName = function() return "DaVinci Resolve" end,
  GetVersionString = function() return "21.1.1" end,
  GetCurrentPage = function(self) return self.page end,
  OpenPage = function(self, page) self.page = page; return true end,
}

-- Test hook: lets tests reach the mock's internals through the bridge.
mock = {
  project = project,
  media_info = {}, -- [path] = {fps = 30, frames = 90, audio = false, video = false}
  -- Put a dissolve across the cut between the first two clips of the current timeline.
  add_transition = function()
    local tl = project:GetCurrentTimeline()
    local v1 = tl.tracks.video[1]
    table.insert(v1, 2, Transition.new(v1[1]:GetEnd() - 6, 12))
    return #v1
  end,
}

-- Fusion titles ------------------------------------------------------------------
-- InsertFusionTitleIntoTimeline is an INSERT edit on V1 at the playhead (measured on
-- 20.3): it splits the clip under the playhead and pushes everything after it right.
-- A locked V1 gets nothing.
local TITLE_SECONDS = 5

local function playhead_frame(tl)
  local fps = tonumber(tl:GetSetting("timelineFrameRate")) or tonumber(project.settings.timelineFrameRate)
  local h, mi, s, ff = (tl.playhead or ""):match("^(%d+):(%d+):(%d+)[:;](%d+)$")
  if not h then return tl.start, fps end
  return ((h * 60 + mi) * 60 + s) * math.floor(fps + 0.5) + ff, fps
end

function Timeline:InsertFusionTitleIntoTimeline(name)
  local v1 = self.tracks.video[1]
  if not v1 or self:GetIsTrackLocked("video", 1) then return nil end
  local at, fps = playhead_frame(self)
  local len = TITLE_SECONDS * math.floor(fps + 0.5)
  local out = {}
  for _, it in ipairs(v1) do
    if it.start < at and it:GetEnd() > at then -- split
      local tail = Item.new(it.name, at, it:GetEnd() - at, it.path, it.mpi)
      tail.left = it.left + (at - it.start)
      it.dur = at - it.start
      out[#out + 1] = it
      out[#out + 1] = tail
    else
      out[#out + 1] = it
    end
  end
  for _, it in ipairs(out) do if it.start >= at then it.start = it.start + len end end
  local tool = { inputs = { StyledText = "Title", Size = 0.08, Center = { 0.5, 0.5 } } }
  function tool:SetInput(k, v) self.inputs[k] = v; return true end
  function tool:GetInput(k) return self.inputs[k] end
  local comp = { FindTool = function(_, n) if n == "Template" then return tool end end,
    FindToolByID = function() return nil end }
  local title = Item.new(name, at, len)
  title.comp, title.track_type, title.track_index = comp, "video", 1
  out[#out + 1] = title
  table.sort(out, function(a, b) return a.start < b.start end)
  for i = #v1, 1, -1 do v1[i] = nil end
  for i, it in ipairs(out) do v1[i] = it end
  return title
end
function Item:GetFusionCompByIndex(i) return i == 1 and self.comp or nil end

-- Wraps items into one compound clip item (and media pool item) in place.
function Timeline:CreateCompoundClip(items, opts)
  if not items or #items == 0 then return nil end
  local first, last = items[1].start, items[1]:GetEnd()
  for _, it in ipairs(items) do first = math.min(first, it.start); last = math.max(last, it:GetEnd()) end
  local fps = tonumber(self:GetSetting("timelineFrameRate")) or tonumber(project.settings.timelineFrameRate)
  local mpi = Mpi.new((opts or {}).name or "Compound Clip")
  mpi.name, mpi.compound, mpi.audio, mpi.video, mpi.fps, mpi.frames = (opts or {}).name or "Compound Clip",
    true, false, true, fps, last - first
  table.insert(project.pool.current.clips, mpi)
  local doomed = {}
  for _, it in ipairs(items) do doomed[it] = true end
  local v1 = self.tracks.video[1]
  for i = #v1, 1, -1 do if doomed[v1[i]] then table.remove(v1, i) end end
  local cc = Item.new(mpi.name, first, last - first, nil, mpi)
  table.insert(v1, cc)
  return cc
end

function MediaPool:DeleteTimelines(list)
  local doomed = {}
  for _, t in ipairs(list or {}) do doomed[t] = true end
  local tls = self.project.timelines
  for i = #tls, 1, -1 do if doomed[tls[i]] then table.remove(tls, i) end end
  return true
end

function MediaPool:MoveClips(clips, folder)
  local moving = {}
  for _, c in ipairs(clips or {}) do moving[c] = true end
  local function strip(f)
    for i = #f.clips, 1, -1 do if moving[f.clips[i]] then table.remove(f.clips, i) end end
    for _, s in ipairs(f.subs) do strip(s) end
  end
  strip(self.root)
  for _, c in ipairs(clips or {}) do table.insert(folder.clips, c) end
  return true
end

-- Checkpoints ------------------------------------------------------------------------
-- DuplicateTimeline makes the copy current (measured on 20.3).
function Timeline:DuplicateTimeline(name)
  local copy = Timeline.new(name)
  copy.start, copy.settings = self.start, {}
  for k, v in pairs(self.settings) do copy.settings[k] = v end
  for kind, tracks in pairs(self.tracks) do
    copy.tracks[kind] = {}
    for i, track in ipairs(tracks) do
      copy.tracks[kind][i] = {}
      for j, it in ipairs(track) do
        local c = Item.new(it.name, it.start, it.dur, it.path, it.mpi)
        c.left, c.track_type, c.track_index = it.left, it.track_type, it.track_index
        copy.tracks[kind][i][j] = c
      end
    end
  end
  table.insert(project.timelines, copy)
  project.current = copy
  return copy
end
function Timeline:GetMediaPoolItem()
  self.pool_item = self.pool_item or setmetatable({ name = self.name, timeline = true }, Mpi)
  return self.pool_item
end
pm.SaveProject = function() project.saved = (project.saved or 0) + 1; return true end
pm.ExportProject = function(_, name, path, with_stills)
  local f = io.open(path, "wb")
  if not f then return false end
  f:write("DRP " .. name)
  f:close()
  return true
end

-- Grading: SetCDL has no getter in Resolve; the mock records it so tests can look.
function Item:SetCDL(cdl)
  if type(cdl) ~= "table" or not cdl.NodeIndex then return false end
  self.cdl = cdl
  return true
end
function Item:CopyGrades(targets)
  for _, t in ipairs(targets or {}) do t.cdl = self.cdl end
  return true
end
