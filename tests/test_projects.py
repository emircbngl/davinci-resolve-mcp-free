"""manage_projects: list, open and create projects without losing the open one's work."""
from __future__ import annotations

import pytest

from resolve_pilot import control
from resolve_pilot.lua_snippets import library as lua
from resolve_pilot.bridge import client as bridge
from tests.conftest import needs_luajit


@pytest.fixture
def no_studio(monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))


def _saves() -> int:
    return bridge.exec_lua("return mock.project.saved or 0").result


def test_the_snippet_refuses_what_it_cannot_do():
    with pytest.raises(ValueError, match="action"):
        lua.snippet_manage_projects("delete", "x")
    for action in ("open", "create"):
        with pytest.raises(ValueError, match=f"{action} needs the project name"):
            lua.snippet_manage_projects(action, "  ")
    assert "LoadProject" in lua.snippet_manage_projects("open", 'My "Cut"')


@needs_luajit
def test_list_create_and_open(live_bridge, no_studio):
    from resolve_pilot import mcp_server
    assert mcp_server.manage_projects() == {"channel": "bridge", "ok": True, "current": "Test Project",
                                            "projects": ["Test Project"]}
    made = mcp_server.manage_projects("create", "Scratch")
    assert made == {"channel": "bridge", "ok": True, "project": "Scratch", "created": True,
                    "previous": "Test Project", "saved_previous": True, "timeline_count": 0}
    assert mcp_server.manage_projects()["projects"] == ["Test Project", "Scratch"]
    assert _saves() == 1                 # the new project is saved at once: Resolve lists it only then
    assert mcp_server.manage_projects()["current"] == "Scratch"
    assert mcp_server.get_resolve_state()["project"] == "Scratch"       # the other tools follow

    back = mcp_server.manage_projects("open", "Test Project")
    assert back == {"channel": "bridge", "ok": True, "project": "Test Project", "previous": "Scratch",
                    "saved_previous": True, "timeline_count": 1, "current_timeline": "Main"}
    assert _saves() == 1                 # saved once, when it was left for Scratch


@needs_luajit
def test_opening_the_open_project_changes_nothing(live_bridge, no_studio):
    from resolve_pilot import mcp_server
    out = mcp_server.manage_projects("open", "Test Project")
    assert out == {"channel": "bridge", "ok": True, "project": "Test Project", "already_open": True,
                   "timeline_count": 1}
    assert _saves() == 0


@needs_luajit
def test_wrong_names_come_back_with_the_names_there_are(live_bridge, no_studio):
    from resolve_pilot import mcp_server
    missing = mcp_server.manage_projects("open", "Nope")
    assert missing["ok"] is False and "no project named 'Nope'" in missing["error"]
    assert missing["projects"] == ["Test Project"] and missing["current"] == "Test Project"
    taken = mcp_server.manage_projects("create", "Test Project")
    assert taken["ok"] is False and "already exists" in taken["error"] and taken["projects"] == ["Test Project"]
    assert _saves() == 0 and mcp_server.manage_projects()["current"] == "Test Project"


@needs_luajit
def test_a_project_that_cannot_be_saved_is_not_left(live_bridge, no_studio):
    from resolve_pilot import mcp_server
    mcp_server.manage_projects("create", "Scratch")
    mcp_server.manage_projects("open", "Test Project")
    bridge.exec_lua("mock.save_fails = true")
    refused = mcp_server.manage_projects("open", "Scratch")
    assert refused["ok"] is False and refused["current"] == "Test Project"
    assert "could not be saved" in refused["error"] and "File > Save Project" in refused["error"]
    assert mcp_server.manage_projects()["current"] == "Test Project"
    assert mcp_server.manage_projects("create", "Third")["ok"] is False
    # An empty project has nothing to lose: leaving it is allowed even when it cannot be saved
    # (the untitled project Resolve starts with).
    bridge.exec_lua("mock.save_fails = false")
    mcp_server.manage_projects("open", "Scratch")
    bridge.exec_lua("mock.save_fails = true")
    out = mcp_server.manage_projects("open", "Test Project")
    assert out["ok"] is True and out["saved_previous"] is False and out["previous"] == "Scratch"


@needs_luajit
def test_resolve_refusing_to_open_is_reported(live_bridge, no_studio):
    from resolve_pilot import mcp_server
    mcp_server.manage_projects("create", "Scratch")
    bridge.exec_lua("resolve:GetProjectManager().LoadProject = function() return nil end")
    out = mcp_server.manage_projects("open", "Test Project")
    assert out["ok"] is False and out["error"] == "Resolve did not open the project 'Test Project'."
    assert out["current"] == "Scratch" and out["previous"] == "Scratch"


def test_without_a_live_channel_the_lua_is_handed_over():
    from resolve_pilot import mcp_server
    out = mcp_server.manage_projects("open", "Vlog", channel="manual")
    assert out["channel"] == "manual" and 'pm:LoadProject(name)' in out["lua_code"] and '"Vlog"' in out["lua_code"]
