"""Shared fixtures: an isolated state dir, generated media, and a live bridge.

The live bridge fixture runs the real ``bridge.lua`` under LuaJIT (the Lua that
DaVinci Resolve embeds) against ``tests/lua/mock_resolve.lua``, so the whole
request → Lua → JSON → Python path is exercised without Resolve installed.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

LUAJIT = shutil.which("luajit")
FFMPEG = shutil.which("ffmpeg")
MOCK_RESOLVE = Path(__file__).parent / "lua" / "mock_resolve.lua"

needs_luajit = pytest.mark.skipif(not LUAJIT, reason="luajit not installed")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and shutil.which("ffprobe")), reason="ffmpeg not installed")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep plans, transcripts and bridge files out of the real ~/.resolve-pilot."""
    home = tmp_path / "rp-home"
    monkeypatch.setenv("RESOLVE_PILOT_HOME", str(home))
    monkeypatch.delenv("RESOLVE_PILOT_BRIDGE_DIR", raising=False)
    return home


# Resolve 21.1+ Free: Console Lua without io, package, require, debug or os.execute
# (reported by other bridges on 21.1.0.14). os.remove/os.rename are dropped too, as
# the worst case; ffi stays, as it is a global inside Resolve.
_SANDBOX_21_1 = """\
local safe_os = {time = os.time, clock = os.clock, date = os.date, getenv = os.getenv,
  difftime = os.difftime}
local env = setmetatable({io = false, package = false, require = false, debug = false,
  os = safe_os, ffi = require("ffi")}, {__index = _G})
setmetatable(env, {__index = function(_, k)
  local v = rawget(env, k)
  if v == false then return nil end
  return _G[k]
end})
"""


def start_bridge(workdir: Path, idle_minutes: int = 1, sandbox: bool = False) -> subprocess.Popen:
    """Render the bridge for the current RESOLVE_PILOT_HOME and run it under LuaJIT."""
    from resolve_pilot import bridge
    from resolve_pilot.lua_snippets.library import lua_string

    script = workdir / "bridge.lua"
    script.write_text(bridge.render_script(idle_minutes=idle_minutes), encoding="utf-8")
    runner = workdir / "runner.lua"
    runner.write_text(
        # Like Resolve/Fusion: the script runs in its own environment that falls back to
        # the globals, while chunks it compiles with loadstring get the real globals.
        f"dofile({lua_string(str(MOCK_RESOLVE))})\n"
        f"local chunk = assert(loadfile({lua_string(str(script))}))\n"
        + (_SANDBOX_21_1 + "setfenv(chunk, env)\n" if sandbox
           else "setfenv(chunk, setmetatable({}, {__index = _G}))\n")
        + "chunk()\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen([LUAJIT, str(runner)], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if bridge.status().running:
            return proc
        if proc.poll() is not None:
            raise RuntimeError("bridge exited early:\n" + proc.stdout.read())
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("bridge did not start")


def stop_bridge(proc: subprocess.Popen) -> str:
    from resolve_pilot import bridge

    bridge.stop()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return proc.stdout.read() if proc.stdout else ""


@pytest.fixture
def live_bridge(tmp_path, request):
    if not LUAJIT:
        pytest.skip("luajit not installed")
    proc = start_bridge(tmp_path, sandbox=getattr(request, "param", None) == "21.1")
    yield proc
    if proc.poll() is None:
        stop_bridge(proc)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """A 6 s, 24 fps clip with a hard cut at 3 s, and a 6 s click track at 120 BPM."""
    if not (FFMPEG and shutil.which("ffprobe")):
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("media")
    clip = d / "two scenes.mp4"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=3",
        "-f", "lavfi", "-i", "color=c=red:size=320x240:rate=24:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(clip),
    ], check=True)
    song = d / "song.wav"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        "aevalsrc='if(lt(mod(t,0.5),0.04),sin(2*PI*1000*t),0)':d=6:s=22050",
        str(song),
    ], check=True)
    return {"clip": clip, "song": song}
