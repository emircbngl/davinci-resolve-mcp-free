"""setup_bridge(action="start"): Resolve up, the bridge's menu entry clicked, heartbeat seen;
and plain words for whatever this machine does not let the server do."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from resolve_pilot import mcp_server
from resolve_pilot.ui_automation import applescript


class World:
    """A machine as the server sees it. Time only passes when the server sleeps."""

    def __init__(self, monkeypatch, tmp_path):
        self.now = 0.0
        self.resolve_up_at: float | None = None      # when a started Resolve shows up as running
        self.resolve_running = False
        self.app = "/Applications/DaVinci Resolve/DaVinci Resolve.app"
        self.launched: list[str] = []
        self.menu_ready_at = 0.0                      # before this the menu click fails (still loading)
        self.permission = True
        self.clicks: list[list[str]] = []
        self.click_attempts = 0
        self.bridge_up_after_click: float | None = 3.0
        self.bridge_running = False
        self.clicked_at: float | None = None
        self.version, self.studio = "20.3.0", False
        self.macos = True
        m = monkeypatch
        m.setattr(mcp_server.time, "monotonic", lambda: self.now)
        m.setattr(mcp_server.time, "sleep", self.sleep)
        m.setattr(mcp_server, "_resolve_running", lambda: self.resolve_running)
        m.setattr(mcp_server, "_launch_resolve", self.launch)
        m.setattr(mcp_server, "_resolve_app", lambda: (self.version, self.studio))
        m.setattr(mcp_server, "_platform_supports_ui", lambda: self.macos)
        m.setattr(mcp_server.bridge, "status", lambda: SimpleNamespace(running=self.bridge_running))
        m.setattr(mcp_server.bridge, "install_script", lambda idle: tmp_path / "ResolvePilotBridge.lua")
        import resolve_pilot.ui_automation as ui
        m.setattr(ui, "menu_pick", self.click)

    def sleep(self, seconds):
        self.now += seconds
        if self.resolve_up_at is not None and self.now >= self.resolve_up_at:
            self.resolve_running = True
        if (self.clicked_at is not None and self.bridge_up_after_click is not None
                and self.now >= self.clicked_at + self.bridge_up_after_click):
            self.bridge_running = True

    def launch(self):
        if self.app is None:
            return None
        self.launched.append(self.app)
        return self.app

    def click(self, path):
        self.click_attempts += 1
        if not self.permission:
            raise applescript.UIAutomationError(
                "osascript failed (exit 1): System Events got an error: osascript is not allowed "
                "assistive access. (-1719)")
        if self.now < self.menu_ready_at:
            raise applescript.UIAutomationError("DaVinci Resolve did not become frontmost; menu click not sent.")
        self.clicks.append(path)
        self.clicked_at = self.now


@pytest.fixture
def world(monkeypatch, tmp_path):
    return World(monkeypatch, tmp_path)


def test_a_running_bridge_is_left_alone(world):
    world.bridge_running = True
    assert mcp_server.setup_bridge("start") == {"bridge_running": True, "already_running": True}
    assert world.launched == [] and world.clicks == []


def test_from_a_closed_resolve_to_a_running_bridge(world):
    world.resolve_up_at, world.menu_ready_at = 8.0, 20.0      # the window appears, then finishes loading
    out = mcp_server.setup_bridge("start", wait_seconds=90)
    assert out["bridge_running"] is True and out["menu_clicked"] is True
    assert out["resolve_started"] == world.app and world.launched == [world.app]
    assert world.clicks == [["Workspace", "Scripts", "ResolvePilotBridge"]]     # clicked once, not per try
    assert "manage_projects" in out["next_step"] and "left_for_user" not in out
    assert world.now <= 90


def test_an_open_resolve_is_not_started_again(world):
    world.resolve_running = True
    out = mcp_server.setup_bridge("start")
    assert out["bridge_running"] is True and "resolve_started" not in out and world.launched == []


def test_no_permission_to_click_is_said_once_with_what_to_do(world):
    world.resolve_running, world.permission = True, False
    out = mcp_server.setup_bridge("start", wait_seconds=90)
    assert out["bridge_running"] is False and out["menu_clicked"] is False
    assert world.click_attempts == 1                         # it does not keep trying a click it may not make
    (left,) = out["left_for_user"]
    assert left.startswith("In DaVinci Resolve choose Workspace > Scripts > ResolvePilotBridge.")
    assert "Privacy & Security > Accessibility" in left


def test_a_resolve_that_never_finishes_loading_stops_at_the_deadline(world):
    world.resolve_running, world.menu_ready_at = True, 10_000.0
    out = mcp_server.setup_bridge("start", wait_seconds=30)
    assert out["menu_clicked"] is False and 30 <= world.now <= 32 and world.click_attempts == 16
    assert "did not become frontmost" in out["left_for_user"][0]


def test_a_click_that_starts_nothing_is_not_called_running(world):
    world.resolve_running, world.bridge_up_after_click = True, None
    out = mcp_server.setup_bridge("start", wait_seconds=20)
    assert out["menu_clicked"] is True and out["bridge_running"] is False and "next_step" not in out
    assert "dofile(" in out["left_for_user"][0] and "ResolvePilotBridge.lua" in out["left_for_user"][0]


def test_resolve_not_installed_where_expected(world):
    world.app = None
    out = mcp_server.setup_bridge("start")
    assert out["bridge_running"] is False and out["resolve_started"] is None
    assert "Start DaVinci Resolve" in out["left_for_user"][0] and world.clicks == []


def test_resolve_started_but_not_up_in_time(world):
    world.resolve_up_at = 500.0
    out = mcp_server.setup_bridge("start", wait_seconds=10)
    assert out["resolve_started"] == world.app and "not up yet" in out["left_for_user"][0]
    assert world.clicks == [] and 10 <= world.now <= 11


def test_a_resolve_where_the_bridge_cannot_start_is_not_clicked(world):
    world.resolve_running, world.version = True, "21.2.0"     # 21.1.2+ Free: reported not to start
    out = mcp_server.setup_bridge("start")
    assert out["bridge_can_start"] is False and world.clicks == []
    assert "the bridge cannot start" in out["left_for_user"][0] and out["resolve_note"] == out["left_for_user"][0]


def test_on_other_systems_the_click_is_left_to_the_user(world):
    world.resolve_running, world.macos = True, False
    out = mcp_server.setup_bridge("start")
    assert out["bridge_running"] is False and world.clicks == []
    assert "macOS only" in out["left_for_user"][0]


def test_launching_uses_the_installed_program(monkeypatch, tmp_path):
    app = tmp_path / "DaVinci Resolve.app"
    app.mkdir()
    ran = []
    monkeypatch.setattr(mcp_server.platform, "system", lambda: "Darwin")
    monkeypatch.setitem(mcp_server._RESOLVE_PROGRAMS, "Darwin", [str(tmp_path / "missing.app"), str(app)])
    monkeypatch.setattr(mcp_server.subprocess, "run", lambda args, **kw: ran.append(args))
    assert mcp_server._launch_resolve() == str(app) and ran == [["open", "-a", str(app)]]
    monkeypatch.setitem(mcp_server._RESOLVE_PROGRAMS, "Darwin", [str(tmp_path / "missing.app")])
    assert mcp_server._launch_resolve() is None and len(ran) == 1
