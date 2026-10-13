-- Minimal stand-in for DaVinci Resolve's Lua scripting API, used by the tests to
-- run the real bridge and the real snippets under LuaJIT (Resolve embeds LuaJIT).
-- Only the calls resolve-pilot makes are modelled.

local function basename(path)
  return (path:match("([^/\\]+)$") or path):gsub("%.[^.]*$", "")
end

-- TimelineItem ---------------------------------------------------------------
local Item = {}
Item.__index = Item

function Item.new(name, start, dur, path)
  return setmetatable({ name = name, start = start, dur = dur, path = path, color = "" }, Item)
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
function Timeline:GetItemListInTrack(kind, index) return (self.tracks[kind] or {})[index] or {} end
function Timeline:AddMarker(frame, color, name, note, duration, data)
  if self.markers[frame] then return false end
  self.markers[frame] = { color = color, name = name, note = note, duration = duration, customData = data }
  return true
end
function Timeline:GetMarkers() return self.markers end
function Timeline:GetStartTimecode() return "01:00:00:00" end
function Timeline:GetCurrentTimecode() return "01:00:00:00" end
function Timeline:Export(path, kind) return true end
function Timeline:GetCurrentVideoItem() return self.tracks.video[1][1] end

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
function Project:GetRenderPresetList() return { "H.264 Master", "YouTube - 1080p" } end
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

-- Resolve ------------------------------------------------------------------------
local project = Project.new("Test Project")
local pm = { GetCurrentProject = function() return project end }

resolve = {
  page = "edit",
  GetProjectManager = function() return pm end,
  GetProductName = function() return "DaVinci Resolve" end,
  GetVersionString = function() return "21.1.1" end,
  GetCurrentPage = function(self) return self.page end,
  OpenPage = function(self, page) self.page = page; return true end,
}

-- Test hook: lets tests reach the mock's internals through the bridge.
mock = { project = project }
