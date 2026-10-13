-- Minimal stand-in for DaVinci Resolve's Lua scripting API, used by the tests to
-- run the real bridge and the real snippets under LuaJIT (Resolve embeds LuaJIT).
-- Only the calls resolve-pilot makes are modelled.

local function basename(path)
  return (path:match("([^/\\]+)$") or path):gsub("%.[^.]*$", "")
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

function Item.new(name, start, dur, path)
  return setmetatable({ name = name, start = start, dur = dur, path = path, color = "",
    props = default_props(), enabled = true }, Item)
end
function Item:GetName() return self.name end
function Item:GetStart() return self.start end
function Item:GetEnd() return self.start + self.dur end
function Item:GetDuration() return self.dur end
function Item:GetLeftOffset() return 0 end
function Item:GetClipColor() return self.color end
function Item:SetClipColor(c) self.color = c; return true end
function Item:SetLUT(node, path) self.lut = { node = node, path = path }; return true end
function Item:GetMediaPoolItem()
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
  local tl = setmetatable({ name = name, start = 86400, markers = {},
    tracks = { video = { {} }, audio = { {} }, subtitle = {} } }, Timeline)
  local pos = tl.start
  for _, c in ipairs(clips or {}) do
    table.insert(tl.tracks.video[1], Item.new(c[1], pos, c[2], c[3]))
    pos = pos + c[2]
  end
  return tl
end
function Timeline:GetName() return self.name end
function Timeline:GetStartFrame() return self.start end
function Timeline:GetEndFrame()
  local last = self.start
  for _, it in ipairs(self.tracks.video[1]) do last = math.max(last, it:GetEnd()) end
  return last
end
function Timeline:GetSetting(key) return "" end -- "" = follows the project settings
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

function Project.new(name)
  local p = setmetatable({ name = name, page = "edit", render_jobs = {},
    settings = { timelineFrameRate = "24", timelineResolutionWidth = "1920",
      timelineResolutionHeight = "1080" } }, Project)
  p.timelines = { Timeline.new("Main", { { "A", 48, "/media/a.mov" }, { "B", 72, "/media/b.mov" } }) }
  p.current = p.timelines[1]
  p.pool = setmetatable({ project = p }, MediaPool)
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
function Project:SetRenderSettings(s) self.render_settings = s; return true end
function Project:AddRenderJob()
  local id = "job-" .. (#self.render_jobs + 1)
  table.insert(self.render_jobs, id)
  return id
end
function Project:StartRendering() self.rendering = true; return true end
function Project:IsRenderingInProgress() return self.rendering == true end
function Project:GetRenderJobList()
  local out = {}
  for _, id in ipairs(self.render_jobs) do
    out[#out + 1] = { JobId = id, RenderJobName = id, TimelineName = "Main", TargetDir = "/out",
      OutputFilename = id .. ".mov" }
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
  -- Put a dissolve across the cut between the first two clips of the current timeline.
  add_transition = function()
    local tl = project:GetCurrentTimeline()
    local v1 = tl.tracks.video[1]
    table.insert(v1, 2, Transition.new(v1[1]:GetEnd() - 6, 12))
    return #v1
  end,
}
