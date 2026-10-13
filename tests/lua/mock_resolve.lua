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

function Item.new(name, start, dur, path, mpi)
  return setmetatable({ name = name, start = start, dur = dur, path = path, color = "", mpi = mpi,
    left = 0 }, Item)
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
function Item:GetProperty() return { Opacity = 100, ZoomX = 1, ZoomY = 1 } end

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
function Timeline:Export(path, kind) return true end
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
      for _, cue in ipairs(mpi.cues) do
        local it = Item.new(cue[3], tl.start + math.floor(cue[1] * fps + 0.5),
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
