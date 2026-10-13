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
