"""Live bridge: the real bridge.lua under LuaJIT, driven through the Python client."""
from __future__ import annotations

import re
import subprocess
import time

import pytest

from resolve_pilot import bridge
from resolve_pilot.lua_snippets import library as lua
from tests.conftest import LUAJIT, needs_luajit, start_bridge, stop_bridge


def test_status_without_bridge_explains_how_to_start():
    st = bridge.status()
    assert st.running is False
    assert "Workspace > Scripts" in st.detail


def test_exec_without_bridge_raises_not_running():
    with pytest.raises(bridge.BridgeNotRunning):
        bridge.exec_lua("return 1")


def test_render_script_bakes_in_folder_and_escapes_it(tmp_path):
    weird = tmp_path / 'we"ird \\ dir'
    code = bridge.render_script(idle_minutes=5, directory=weird)
    assert lua.lua_string(str(weird)) in code
    assert "__RP_" not in code
    assert "local IDLE_LIMIT = 300" in code


def test_scripts_menu_dir_is_utility_folder(monkeypatch):
    monkeypatch.delenv("RESOLVE_PILOT_SCRIPTS_DIR")
    assert bridge.scripts_menu_dir().name == "Utility"
    with pytest.raises(ValueError):
        bridge.scripts_menu_dir("everyone")


@needs_luajit
def test_rendered_bridge_compiles(tmp_path):
    f = tmp_path / "b.lua"
    f.write_text(bridge.render_script())
    subprocess.run([LUAJIT, "-b", str(f), str(tmp_path / "b.out")], check=True)


@needs_luajit
def test_roundtrip_values_and_print_capture(live_bridge):
    r = bridge.exec_lua(
        'print("hi", 2, {a = 1})\n'
        'return {n = 1.5, i = 3, list = {1, 2, 3}, s = "q\\"\\n\\t\\1", t = true, empty = {}}'
    )
    assert r.ok, r.error
    assert r.output == 'hi\t2\t{"a":1}'
    assert r.result == {"n": 1.5, "i": 3, "list": [1, 2, 3], "s": 'q"\n\t\x01', "t": True, "empty": []}


@needs_luajit
def test_syntax_and_runtime_errors_are_reported(live_bridge):
    r = bridge.exec_lua("return 1 +")
    assert not r.ok and r.error.startswith("Lua syntax error")
    r = bridge.exec_lua('print("before") error("boom")')
    assert not r.ok and "boom" in r.error and "stack traceback" in r.error
    assert r.output == "before"
    # The bridge keeps serving after errors.
    assert bridge.exec_lua("return 42").result == 42


@needs_luajit
def test_rp_helpers(live_bridge):
    r = bridge.exec_lua("return {rp.project():GetName(), rp.timeline():GetName(), rp.fps()}")
    assert r.result == ["Test Project", "Main", 24]


@needs_luajit
def test_snippets_run_against_mock_resolve(live_bridge, tmp_path):
    state = bridge.exec_lua(lua.snippet_get_state(include_clips=True, include_markers=True)).result
    assert state["project"] == "Test Project"
    tl = state["current_timeline"]
    assert tl["fps"] == 24 and tl["clip_count"] == 2
    assert tl["clips"][1]["start_seconds"] == 2 and tl["clips"][1]["source_path"] == "/media/b.mov"

    res = bridge.exec_lua(lua.snippet_add_markers([
        {"seconds": 1.5, "color": "red", "name": 'Say "hi"'},
        {"frame": 10},
        {"frame": 10},
    ])).result
    assert res["added"] == 2 and res["skipped_frames"] == [10]
    markers = bridge.exec_lua(lua.snippet_get_state(include_markers=True)).result
    assert [m["frame"] for m in markers["current_timeline"]["markers"]] == [10, 36]
    assert markers["current_timeline"]["markers"][1]["name"] == 'Say "hi"'

    assert bridge.exec_lua(lua.snippet_set_clip_color("teal", 1)).result["colored"] == 2
    assert bridge.exec_lua(lua.snippet_apply_lut_to_clips("/luts/a.cube")).result["applied"] == 2

    bad = bridge.exec_lua(lua.snippet_render_current_timeline(preset="Nope")).result
    assert bad["ok"] is False
    assert bad["available_presets"] == ["H.264 Master", "YouTube - 1080p"]  # a list, no "__flags"
    job = bridge.exec_lua(lua.snippet_render_current_timeline(start=True)).result
    assert job["ok"] and job["job_id"] == "job-1" and job["rendering"] is True

    fcpxml = tmp_path / "cut [v2].fcpxml"
    fcpxml.write_text("<fcpxml/>")
    imp = bridge.exec_lua(lua.snippet_import_timeline(str(fcpxml), "My Cut")).result
    assert imp == {"ok": True, "timeline": "My Cut", "video_clip_count": 1,
                   "start_frame": 86400, "end_frame": 86448}
    missing = bridge.exec_lua(lua.snippet_import_timeline(str(tmp_path / "nope.fcpxml"))).result
    assert missing["ok"] is False

    assert bridge.exec_lua(lua.snippet_open_page("color")).result == {"ok": True, "page": "color"}


_SNIPPET_SAMPLES = {
    "add_markers": {"markers": [{"frame": 1}]},
    "batch_markers": {"frames": [1, 2]},
    "import_timeline": {"path": "/x.fcpxml"},
    "export_timeline": {"out_path": "/x.fcpxml"},
    "apply_lut_to_clips": {"lut_path": "/x.cube"},
    "relink_media": {"folder": "/x"},
    "grade_from_drx": {"drx_path": "/x.drx"},
    "open_page": {"page": "edit"},
    "export_timeline_stills": {"positions": [{"seconds": 1}, {"frame": 3}], "out_dir": "/x"},
    "add_subtitles": {"srt_path": "/x.srt"},
    "checkpoint": {"action": "save", "label": "before cut"},
    "add_title": {"text": "Hi", "start_seconds": 1, "duration_seconds": 7, "position": (0.5, 0.2),
                  "color": (1, 1, 0), "size": 0.1, "font": "Open Sans"},
    "import_media": {"paths": ["/x.mov"], "bin": "B-roll/Day 1"},
    "append_clips": {"clips": [{"path": "/x.mov", "duration_seconds": 2, "track": 2, "record_seconds": 1},
                               {"media_pool_id": "id", "media": "audio"}],
                     "new_timeline": "Cut", "new_timeline_fps": 25},
    "remove_clips": {"indices": [0, 2], "names": ["a", "b"], "ripple": True},
    "set_clip_properties": {"indices": [0], "properties": {"Scaling": "fill", "Pan": -200.0,
                                                           "FlipX": True}, "enabled": False},
    "set_tracks": {"changes": [{"track_type": "video", "index": 1, "name": "A", "locked": True}],
                   "add": [{"track_type": "audio", "sub_type": "stereo"}]},
}


@needs_luajit
def test_transitions_are_not_counted_or_touched_as_clips(live_bridge):
    assert bridge.exec_lua("return mock.add_transition()").result == 3
    tl = bridge.exec_lua(lua.snippet_get_state(include_clips=True)).result["current_timeline"]
    assert tl["clip_count"] == 2 and tl["transition_count"] == 1
    assert [c["name"] for c in tl["clips"]] == ["A", "B"]
    assert bridge.exec_lua(lua.snippet_set_clip_color("teal", 1)).result["colored"] == 2
    lut = bridge.exec_lua(lua.snippet_apply_lut_to_clips("/luts/a.cube")).result
    assert lut == {"ok": True, "applied": 2, "total": 2, "node_index": 1}
    assert bridge.exec_lua("return mock.project.luts_refreshed").result is True


@needs_luajit
def test_apply_lut_reports_failure_when_resolve_rejects_every_clip(live_bridge):
    bridge.exec_lua("for _, it in ipairs(mock.project:GetCurrentTimeline().tracks.video[1]) do "
                    "it.SetLUT = function() return false end end")
    res = bridge.exec_lua(lua.snippet_apply_lut_to_clips("/elsewhere/a.cube")).result
    assert res["ok"] is False and res["applied"] == 0 and "LUT folder" in res["error"]


@needs_luajit
def test_every_snippet_compiles(tmp_path):
    for s in lua.list_snippets():
        code = lua.render_snippet(s["name"], _SNIPPET_SAMPLES.get(s["name"], {}))
        f = tmp_path / f"{s['name']}.lua"
        f.write_text(code)
        subprocess.run([LUAJIT, "-b", str(f), str(tmp_path / "out")], check=True)


# Globals Resolve's LuaJIT provides; anything else a script reads is a typo or a
# variable that was deleted while a reference to it survived (a bridge once died
# on startup reading a removed MAX_TICKS), which only fails at runtime.
_LUA_BUILTINS = {
    "assert", "collectgarbage", "debug", "dofile", "error", "getmetatable", "io", "ipairs",
    "load", "loadstring", "math", "next", "os", "package", "pairs", "pcall", "print", "rawget",
    "rawset", "require", "select", "setmetatable", "string", "table", "tonumber", "tostring",
    "type", "unpack", "xpcall", "_G", "getfenv", "setfenv",
}
_RESOLVE_GLOBALS = {"resolve", "Resolve", "fusion", "bmd", "app", "rp", "ffi"}


def _globals(luajit_listing: str) -> tuple[set[str], set[str]]:
    reads = set(re.findall(r'\bGGET\b.*?"([^"]+)"', luajit_listing))
    writes = set(re.findall(r'\bGSET\b.*?"([^"]+)"', luajit_listing))
    return reads, writes


def _listing(tmp_path, name: str, code: str) -> str:
    f = tmp_path / f"{name}.lua"
    f.write_text(code)
    return subprocess.run([LUAJIT, "-bl", str(f)], check=True, capture_output=True, text=True).stdout


@needs_luajit
def test_bridge_reads_no_undefined_globals(tmp_path):
    reads, writes = _globals(_listing(tmp_path, "bridge", bridge.render_script()))
    assert reads, "luajit -bl output format changed; the check would pass vacuously"
    assert reads - _LUA_BUILTINS - _RESOLVE_GLOBALS == set()
    # It deliberately sets only these: `print` is swapped while capturing output.
    assert writes <= {"print", "resolve", "rp"}


@needs_luajit
def test_snippets_read_no_undefined_globals_and_leak_none(tmp_path):
    for s in lua.list_snippets():
        code = lua.render_snippet(s["name"], _SNIPPET_SAMPLES.get(s["name"], {}))
        reads, writes = _globals(_listing(tmp_path, s["name"], code))
        assert reads - _LUA_BUILTINS - _RESOLVE_GLOBALS == set(), s["name"]
        assert writes == set(), s["name"]


@needs_luajit
def test_stale_stop_file_does_not_kill_a_fresh_bridge(tmp_path):
    # A stop request left behind (the previous bridge exited before reading it)
    # must not end the next bridge on its first stop check (every 10 ticks).
    (bridge.bridge_dir() / "stop").write_text("stop", encoding="utf-8")
    proc = start_bridge(tmp_path)
    try:
        time.sleep(3.0)
        assert proc.poll() is None, proc.stdout.read()
        assert bridge.exec_lua("return 7", timeout=10).result == 7
    finally:
        out = stop_bridge(proc)
    assert "stopped (stop requested)" in out


@needs_luajit
def test_newest_bridge_wins(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = start_bridge(tmp_path / "a")
    second = start_bridge(tmp_path / "b")
    try:
        first.wait(timeout=10)
        assert "another bridge started" in first.stdout.read()
        assert bridge.exec_lua("return 7").result == 7
    finally:
        if first.poll() is None:
            first.kill()
        stop_bridge(second)


@needs_luajit
def test_stop_request_ends_bridge(live_bridge):
    out = stop_bridge(live_bridge)
    assert "stopped (stop requested)" in out
    assert bridge.status().running is False


# ── Regressions from review ────────────────────────────────────────────────


@needs_luajit
def test_cyclic_and_huge_results_do_not_hang_the_bridge(live_bridge):
    r = bridge.exec_lua("local t = {} t.a = t t.b = t return t", timeout=10)
    assert r.ok and r.result == {"a": "<cycle>", "b": "<cycle>"}
    r = bridge.exec_lua("return _G", timeout=10)
    assert r.ok and r.result["_G"] == "<cycle>"
    r = bridge.exec_lua("print(_G) return 1", timeout=10)
    assert r.ok and r.result == 1 and '"_G":"<cycle>"' in r.output
    assert bridge.exec_lua("return 'still alive'").result == "still alive"


@needs_luajit
def test_unencodable_result_is_reported_not_fatal(live_bridge):
    r = bridge.exec_lua(
        'local k = setmetatable({}, {__tostring = function() error("bad") end}) return {[k] = 1}')
    assert not r.ok and "could not be encoded" in r.error
    assert bridge.exec_lua("return 2").result == 2


@needs_luajit
def test_error_line_numbers_match_the_callers_code(live_bridge):
    r = bridge.exec_lua("local a = 1\nlocal b = nil\nreturn b.x")
    assert "request:3:" in r.error
    r = bridge.exec_lua("local a = 1\nreturn a +")
    assert "request:2:" in r.error


@needs_luajit
def test_output_is_capped(live_bridge):
    r = bridge.exec_lua('for i = 1, 30000 do print(i, "some clip name here") end return 1')
    assert r.ok and r.result == 1 and r.output_truncated
    assert len(r.output) <= 256 * 1024
    assert "output_truncated" not in bridge.exec_lua("print(1)").to_dict()


@needs_luajit
def test_long_request_reports_busy_instead_of_not_running(live_bridge):
    with pytest.raises(bridge.BridgeError, match="still running"):
        bridge.exec_lua('os.execute("sleep 14") return 1', timeout=1)
    import time
    time.sleep(9)  # past the normal heartbeat staleness window, request still running
    st = bridge.status()
    assert st.running and st.busy and st.busy_seconds >= 8
    with pytest.raises(bridge.BridgeBusy):
        bridge.exec_lua("return 1")
    deadline = time.monotonic() + 15
    while bridge.status().busy and time.monotonic() < deadline:
        time.sleep(0.2)
    assert bridge.exec_lua("return 3").result == 3


@needs_luajit
def test_a_busy_older_bridge_does_not_evict_a_newer_one(tmp_path):
    import time
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    old = start_bridge(tmp_path / "a")
    try:
        with pytest.raises(bridge.BridgeError):
            bridge.exec_lua('os.execute("sleep 3") return 1', timeout=0.5)
        new = start_bridge(tmp_path / "b")
        old.wait(timeout=15)
        assert "another bridge started" in old.stdout.read()
        time.sleep(2.5)
        assert new.poll() is None
        assert bridge.exec_lua("return 'new'").result == "new"
    finally:
        if old.poll() is None:
            old.kill()
    stop_bridge(new)


@needs_luajit
def test_two_server_processes_share_one_bridge(live_bridge, tmp_path):
    import os
    import sys
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from resolve_pilot import bridge\n"
        "ok = sum(bridge.exec_lua(f'return {i}', timeout=20).result == i for i in range(10))\n"
        "print(ok)\n"
    )
    procs = [subprocess.Popen([sys.executable, str(worker)], stdout=subprocess.PIPE, text=True,
                              env=os.environ.copy()) for _ in range(2)]
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    assert outs == ["10", "10"]


def test_windows_non_ascii_profile_falls_back_to_ascii_folder(monkeypatch, tmp_path):
    from resolve_pilot.bridge import client
    monkeypatch.setattr(client.platform, "system", lambda: "Windows")
    monkeypatch.setattr(client, "home", lambda: tmp_path / "José" / ".resolve-pilot")
    monkeypatch.setenv("PUBLIC", str(tmp_path / "Public"))
    d = client.bridge_dir()
    assert str(d).isascii() and d == tmp_path / "Public" / "resolve-pilot" / "bridge"


@needs_luajit
def test_requests_see_rp_and_have_print_captured_in_a_script_environment(live_bridge):
    # Resolve runs the bridge script in its own environment; request chunks must still
    # see the rp helpers and have print() captured (measured on Resolve 20.3).
    r = bridge.exec_lua('print("hi")\nreturn rp.timeline():GetName()')
    assert r.result == "Main" and r.output == "hi"


@needs_luajit
@pytest.mark.parametrize("live_bridge", ["21.1"], indirect=True)
def test_bridge_works_without_io_os_execute_package_or_debug(live_bridge, tmp_path):
    """Resolve 21.1+ Free sandbox: file access falls back to ffi, errors still report."""
    r = bridge.exec_lua('print("hi ş")\nreturn {access = rp.file_access, tl = rp.timeline():GetName()}')
    assert r.result == {"access": "ffi", "tl": "Main"} and r.output == "hi ş"
    state = bridge.exec_lua(lua.snippet_get_state(include_clips=True)).result
    assert state["current_timeline"]["clip_count"] == 2
    r = bridge.exec_lua('error("boom")')
    assert not r.ok and "boom" in r.error  # no debug.traceback here, the message still comes back
    assert bridge.exec_lua("return 42").result == 42


def test_install_gives_a_console_line_and_flags_older_bridge_scripts(tmp_path, monkeypatch):
    from resolve_pilot import mcp_server
    folder = bridge.scripts_menu_dir()
    assert str(folder).startswith(str(tmp_path))  # the autouse fixture keeps tests out of Resolve
    folder.mkdir(parents=True)
    (folder / "resolve_pilot_bridge.lua").write_text("-- old")
    (folder / "AutoCut.lua").write_text("-- unrelated")
    out = mcp_server.setup_bridge("install")
    assert out["installed"] == str(folder / "ResolvePilotBridge.lua")
    assert out["console_command"].startswith("dofile(") and "ResolvePilotBridge.lua" in out["console_command"]
    assert out["other_bridge_scripts"] == ["resolve_pilot_bridge.lua"]


@needs_luajit
def test_timeline_frames_export_stills_and_restore_the_playhead(live_bridge, tmp_path):
    from tests.conftest import FFMPEG
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    import json as _json
    from resolve_pilot import mcp_server
    png = tmp_path / "still.png"
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "color=red:s=64x36",
                    "-frames:v", "1", "-y", str(png)], check=True)
    bridge.exec_lua(f"mock.png_path = {lua.lua_string(str(png))}; resolve.page = 'media'")
    pos = mcp_server.TimelinePosition
    result = mcp_server.timeline_frames([pos(seconds=1), pos(frame=30), pos(seconds=99)])
    header = _json.loads(result.content[0].text)
    assert header["channel"] == "bridge" and header["returned"] == 2
    assert [d["index"] for d in header["dropped"]] == [2] and "outside" in header["dropped"][0]["reason"]
    labels = [c.text for c in result.content[1:] if c.type == "text"]
    assert labels[0].startswith("01:00:01:00") and labels[1].startswith("01:00:01:06")
    assert sum(c.type == "image" for c in result.content) == 2
    seen = bridge.exec_lua("return {mock.project.stills, mock.project.current:GetCurrentTimecode(), "
                           "resolve.page}").result
    assert seen == [["01:00:01:00", "01:00:01:06"], "01:00:00:00", "media"]  # restored


@needs_luajit
def test_render_status_reports_jobs_and_progress(live_bridge):
    from resolve_pilot import mcp_server
    assert mcp_server.render_status()["jobs"] == []
    bridge.exec_lua(lua.snippet_render_current_timeline(start=True))
    out = mcp_server.render_status()
    assert out["rendering"] is True and out["done"] is False
    assert out["jobs"][0]["status"] == "Rendering" and out["jobs"][0]["percent"] == 40
    assert out["jobs"][0]["seconds_left"] == 12


@needs_luajit
def test_failures_explain_a_bridge_started_without_a_project(live_bridge):
    bridge.exec_lua("mock_saved = resolve.GetProjectManager; resolve.GetProjectManager = nil")
    r = bridge.exec_lua(lua.snippet_get_state())
    assert not r.ok and "started while no project was open" in r.error
    bridge.exec_lua("resolve.GetProjectManager = mock_saved")
    assert bridge.exec_lua(lua.snippet_get_state()).ok


_SRT = ("1\n00:00:00,500 --> 00:00:02,000\nMerhaba dünya\n\n"
        "2\n00:00:02,500 --> 00:00:04,000\nİkinci satır\n\n")


@needs_luajit
@pytest.mark.parametrize("live_bridge", [None, "21.1"], indirect=True)
def test_add_subtitles_lands_captions_at_srt_times(live_bridge, tmp_path):
    from resolve_pilot import mcp_server
    srt = tmp_path / "konuşma.srt"
    srt.write_text(_SRT, encoding="utf-8")
    out = mcp_server.add_subtitles(srt_path=str(srt))
    assert out["ok"] and out["channel"] == "bridge" and out["imported"] is True
    assert out["cue_count"] == 2 and out["warnings"] == []
    assert out["first"] == {"start_seconds": 0.5, "end_seconds": 2.0, "text": "Merhaba dünya"}
    # Track 1 now has captions: a second run is refused unless replace=true, and the same
    # file is reused from the media pool instead of being imported again.
    refused = mcp_server.add_subtitles(srt_path=str(srt))
    assert refused["ok"] is False and "already has 2 captions" in refused["error"]
    again = mcp_server.add_subtitles(srt_path=str(srt), replace=True)
    assert again["removed"] == 2 and again["cue_count"] == 2 and again["imported"] is False


@needs_luajit
def test_mock_crashes_like_resolve_on_ranged_subtitle_append(live_bridge, tmp_path):
    # Guards the guard: the mock reproduces the crash, so a snippet that passes a range
    # for a subtitle item fails the tests instead of crashing a user's Resolve.
    srt = tmp_path / "a.srt"
    srt.write_text(_SRT, encoding="utf-8")
    r = bridge.exec_lua(f"local mp = rp.media_pool(); rp.timeline():AddTrack('subtitle'); "
                        f"local it = mp:ImportMedia({{{lua.lua_string(str(srt))}}})[1]; "
                        f"return mp:AppendToTimeline({{{{mediaPoolItem = it, startFrame = 0, endFrame = 10}}}})")
    assert not r.ok and "would crash" in r.error


def test_append_clips_refuses_subtitle_files_before_reaching_resolve():
    with pytest.raises(ValueError, match="use add_subtitles"):
        lua.snippet_append_clips([{"path": "/x/captions.SRT", "in_seconds": 0, "duration_seconds": 2}])


@needs_luajit
def test_add_subtitles_ignores_where_the_playhead_is(live_bridge, tmp_path):
    # Resolve appends subtitles at the playhead; the tool parks it on frame 0 and back.
    from resolve_pilot import mcp_server
    srt = tmp_path / "a.srt"
    srt.write_text(_SRT, encoding="utf-8")
    bridge.exec_lua('rp.timeline():SetCurrentTimecode("01:00:05:00")')
    out = mcp_server.add_subtitles(srt_path=str(srt))
    assert out["first"]["start_seconds"] == 0.5 and out["warnings"] == []
    assert bridge.exec_lua("return rp.timeline():GetCurrentTimecode()").result == "01:00:05:00"


@needs_luajit
@pytest.mark.parametrize("live_bridge", [None, "21.1"], indirect=True)
def test_add_title_overlays_without_touching_the_edit(live_bridge):
    from resolve_pilot import mcp_server
    before = bridge.exec_lua(lua.snippet_get_state(include_clips=True)).result["current_timeline"]["clips"]
    bridge.exec_lua('rp.timeline():SetCurrentTimecode("01:00:02:00")')
    out = mcp_server.add_title("Gün 1", start_seconds=0.5, duration_seconds=7, position=[0.5, 0.15],
                               color=[1, 0.8, 0], size=0.07)
    assert out["ok"] and out["track"] == 2 and out["text_pieces"] == 2  # 7 s > one 5 s Text+
    assert out["start_seconds"] == 0.5 and out["end_seconds"] == 7.5
    assert out["inputs"]["StyledText"] == "Gün 1" and out["inputs"]["Center"] == [0.5, 0.15]
    after = bridge.exec_lua(lua.snippet_get_state(include_clips=True)).result["current_timeline"]["clips"]
    assert after == before  # V1 untouched, though Resolve's own insert would split and ripple it
    state = bridge.exec_lua("local p = rp.project(); local names = {} "
                            "for i = 1, p:GetTimelineCount() do names[i] = p:GetTimelineByIndex(i):GetName() end "
                            "local titles = {} for _, f in ipairs(rp.media_pool():GetRootFolder():GetSubFolderList()) do "
                            "if f:GetName() == 'Titles' then for _, c in ipairs(f:GetClipList()) do "
                            "titles[#titles + 1] = c:GetName() end end end "
                            "return {timelines = names, titles = titles, current = rp.timeline():GetName()}").result
    assert state == {"timelines": ["Main"], "titles": ["Title: Gün 1"], "current": "Main"}  # scratch deleted
    # A second title over the same time goes to the next free track.
    assert mcp_server.add_title("İkinci", start_seconds=1, duration_seconds=2)["track"] == 3


def test_add_title_rejects_v1_and_bad_values():
    for kwargs, msg in (({"track": 1}, "track must be 2"), ({"position": (2, 0)}, "position"),
                        ({"color": (1, 1)}, "color"), ({"size": 2}, "size")):
        with pytest.raises(ValueError, match=msg):
            lua.snippet_add_title("x", 0, 1, **kwargs)


@needs_luajit
@pytest.mark.parametrize("live_bridge", [None, "21.1"], indirect=True)
def test_checkpoint_save_list_restore_and_backup(live_bridge):
    from resolve_pilot import mcp_server
    saved = mcp_server.checkpoint("save", label="before cut")
    assert saved["ok"] and saved["checkpoint"] == "Main · checkpoint before cut"
    assert saved["current"] == "Main" and saved["in_bin"] == "Checkpoints"  # copy did not stay current
    assert mcp_server.checkpoint("save", label="before cut")["ok"] is False  # no silent duplicates
    listed = mcp_server.checkpoint("list")
    assert listed["checkpoints"] == [{"name": "Main · checkpoint before cut", "of": "Main", "label": "before cut"}]
    # Damage the edit, then restore: a fresh copy of the checkpoint becomes current.
    bridge.exec_lua("table.remove(rp.timeline().tracks.video[1], 1)")
    restored = mcp_server.checkpoint("restore", name="Main · checkpoint before cut")
    assert restored["ok"] and restored["current"] == "Main (restored)"
    clips = bridge.exec_lua(lua.snippet_get_state(include_clips=True)).result["current_timeline"]["clips"]
    assert [c["name"] for c in clips] == ["A", "B"]
    # A checkpoint of the restored copy is named after the original timeline, not nested.
    assert mcp_server.checkpoint("save", label="v2")["checkpoint"] == "Main (restored) · checkpoint v2"
    backup = mcp_server.checkpoint("export_project")
    assert backup["ok"] and backup["size_bytes"] > 0 and backup["path"].endswith(".drp")
