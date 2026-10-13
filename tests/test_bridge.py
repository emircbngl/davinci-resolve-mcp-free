"""Live bridge: the real bridge.lua under LuaJIT, driven through the Python client."""
from __future__ import annotations

import subprocess

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


def test_scripts_menu_dir_is_utility_folder():
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
    assert bad["ok"] is False and "H.264 Master" in bad["available_presets"]
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


@needs_luajit
def test_every_snippet_compiles(tmp_path):
    samples = {
        "add_markers": {"markers": [{"frame": 1}]},
        "batch_markers": {"frames": [1, 2]},
        "import_timeline": {"path": "/x.fcpxml"},
        "export_timeline": {"out_path": "/x.fcpxml"},
        "apply_lut_to_clips": {"lut_path": "/x.cube"},
        "relink_media": {"folder": "/x"},
        "grade_from_drx": {"drx_path": "/x.drx"},
        "open_page": {"page": "edit"},
    }
    for s in lua.list_snippets():
        code = lua.render_snippet(s["name"], samples.get(s["name"], {}))
        f = tmp_path / f"{s['name']}.lua"
        f.write_text(code)
        subprocess.run([LUAJIT, "-b", str(f), str(tmp_path / "out")], check=True)


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
    monkeypatch.setattr(client, "home", lambda: tmp_path / "Emirç" / ".resolve-pilot")
    monkeypatch.setenv("PUBLIC", str(tmp_path / "Public"))
    d = client.bridge_dir()
    assert str(d).isascii() and d == tmp_path / "Public" / "resolve-pilot" / "bridge"
