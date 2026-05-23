-- resolve-pilot bridge — file-polling, non-blocking, console stays responsive.
-- Paste once into Workspace > Console (Lua). It runs ~30 min, then auto-exits.
--
-- Protocol:
--   host writes Lua code     →  /tmp/resolve_bridge_in.lua
--   bridge consumes it, executes, captures prints/return value
--   bridge writes response   →  /tmp/resolve_bridge_out.txt
--   bridge keeps a heartbeat →  /tmp/resolve_bridge_lock  (epoch seconds)
--
-- Response format:
--   first line:  "OK"  |  "ERR"  |  "PARSE_ERR"
--   rest:        captured stdout + optional RETURN: <value>

local IN   = "/tmp/resolve_bridge_in.lua"
local OUT  = "/tmp/resolve_bridge_out.txt"
local LOCK = "/tmp/resolve_bridge_lock"
local MAX_TICKS = 9000          -- 30 min @ 0.2s/tick
local TICK_SEC  = 0.2

local function shell(cmd) os.execute(cmd) end
local function rm(path)   shell('/bin/rm -f "' .. path .. '" 2>/dev/null') end

local function file_exists(path)
  local f = io.open(path, "r")
  if f then f:close(); return true end
  return false
end

local function read_file(path)
  local f = io.open(path, "r"); if not f then return nil end
  local s = f:read("*a"); f:close(); return s
end

local function write_file(path, content)
  local f = io.open(path, "w"); if not f then return false end
  f:write(content); f:close(); return true
end

local function tostr(v)
  local t = type(v)
  if t == "table" then
    local parts = {}
    for k, val in pairs(v) do
      table.insert(parts, tostring(k) .. "=" .. tostring(val))
    end
    return "{" .. table.concat(parts, ", ") .. "}"
  end
  return tostring(v)
end

local function heartbeat() write_file(LOCK, tostring(os.time())) end

rm(IN); rm(OUT)
heartbeat()

print("[bridge] resolve-pilot bridge ready.")
print("[bridge]   IN:   " .. IN)
print("[bridge]   OUT:  " .. OUT)
print("[bridge]   LOCK: " .. LOCK)
print("[bridge] Will idle-poll for up to " .. (MAX_TICKS * TICK_SEC) .. " s.")

for tick = 1, MAX_TICKS do
  if tick % 50 == 0 then heartbeat() end

  if file_exists(IN) then
    local code = read_file(IN)
    rm(IN)

    local fn, perr = loadstring(code or "")
    local response
    if fn then
      local captured = {}
      local oldprint = print
      _G.print = function(...)
        local n = select("#", ...)
        local row = {}
        for i = 1, n do row[i] = tostr(select(i, ...)) end
        table.insert(captured, table.concat(row, "\t"))
      end
      local ok, value = pcall(fn)
      _G.print = oldprint
      if ok then
        local body = table.concat(captured, "\n")
        if value ~= nil then
          body = body .. (body == "" and "" or "\n") .. "RETURN: " .. tostr(value)
        end
        response = "OK\n" .. body
      else
        response = "ERR\n" .. tostr(value) .. "\n" .. table.concat(captured, "\n")
      end
    else
      response = "PARSE_ERR\n" .. tostring(perr)
    end

    -- Atomic write: tmp → rename, so the reader never sees a half file
    write_file(OUT .. ".tmp", response)
    shell('mv "' .. OUT .. '.tmp" "' .. OUT .. '"')
    print(string.format("[bridge] tick %d: dispatched %d bytes", tick, #response))
    heartbeat()
  end

  if bmd and bmd.wait then bmd.wait(TICK_SEC) else shell("sleep 0.2") end
end

rm(LOCK)
print("[bridge] Idle timeout reached. Bridge stopped.")
