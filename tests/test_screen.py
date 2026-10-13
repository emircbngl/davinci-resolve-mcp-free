"""screenshot_resolve: Quartz window discovery and capture, mocked at the Quartz/subprocess edge."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from resolve_pilot import mcp_server
from resolve_pilot.ui_automation import applescript, screen


def _quartz(windows, permitted=True):
    return SimpleNamespace(
        kCGWindowListOptionAll=1, kCGNullWindowID=0,
        kCGWindowLayer="layer", kCGWindowOwnerName="owner", kCGWindowBounds="bounds",
        kCGWindowNumber="number", kCGWindowName="name", kCGWindowIsOnscreen="onscreen",
        CGPreflightScreenCaptureAccess=lambda: permitted,
        CGWindowListCopyWindowInfo=lambda _options, _window_id: windows,
    )


def _win(number, name, w, h, onscreen=True, owner="DaVinci Resolve", layer=0):
    return {"layer": layer, "owner": owner, "number": number, "name": name,
            "bounds": {"X": 0, "Y": 0, "Width": w, "Height": h}, "onscreen": onscreen}


def _png(width, height):
    return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(mcp_server, "_platform_supports_ui", lambda: True)


def _header(result):
    return json.loads(result.content[0].text)


def test_window_discovery_filters_layers_owners_and_offscreen(monkeypatch):
    monkeypatch.setitem(sys.modules, "Quartz", _quartz([
        _win(7, "Main", 1920, 1080), _win(8, "Minimized", 500, 400, onscreen=False),
        _win(9, "Overlay", 200, 100, layer=1), _win(10, "Other", 200, 100, owner="Finder"),
        _win(11, "Zero", 0, 0),
    ]))
    assert screen.screen_recording_available() is True
    onscreen, every = screen.find_resolve_windows()
    assert [w["id"] for w in onscreen] == [7]
    assert [w["id"] for w in every] == [7, 8]


def test_quartz_is_optional(monkeypatch):
    monkeypatch.setitem(sys.modules, "Quartz", None)  # import Quartz -> ImportError
    assert screen._quartz() is None
    assert screen.screen_recording_available() is False
    assert screen.find_resolve_windows() == ([], [])


def test_failed_capture_never_returns_a_stale_file(monkeypatch, tmp_path):
    out = tmp_path / "capture.png"
    out.write_bytes(_png(10, 10))  # left over from an earlier run
    monkeypatch.setattr(screen.subprocess, "run",
                        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="denied"))
    assert screen.capture_window(7, out) is False
    assert not out.exists()


def test_screenshot_returns_header_label_and_downscaled_png(monkeypatch, mac):
    monkeypatch.setitem(sys.modules, "Quartz", _quartz([
        _win(41, "Small", 200, 100), _win(42, "Project", 1000, 500)]))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[0])
        if argv[0] == "screencapture":
            assert "-l" in argv and argv[argv.index("-l") + 1] == "42"
            Path(argv[-1]).write_bytes(_png(2000, 1000))
        else:
            assert argv[0] == "sips" and argv[argv.index("--resampleWidth") + 1] == "640"
            Path(argv[-1]).write_bytes(_png(640, 320))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(screen.subprocess, "run", run)
    monkeypatch.setattr(mcp_server.subprocess, "run", run)
    result = mcp_server.screenshot_resolve(width=640)
    header = _header(result)
    assert header["ok"] is True and header["window_id"] == 42
    assert header["native_px"] == {"width": 2000, "height": 1000}
    assert header["returned_px"] == {"width": 640, "height": 320}
    assert header["points_per_pixel"] == pytest.approx(1000 / 640, abs=1e-4)
    label, image = result.content[1:]
    assert label.text == "Resolve window 'Project' (640x320)"
    assert image.type == "image" and (getattr(image, "mimeType", None) or image.mime_type) == "image/png"
    assert calls == ["screencapture", "sips"]


def test_missing_permission_is_an_error_not_a_blank_image(monkeypatch, mac):
    monkeypatch.setitem(sys.modules, "Quartz", _quartz([_win(1, "Main", 100, 100)], permitted=False))
    from resolve_pilot.ui_automation import permissions
    monkeypatch.setattr(permissions, "responsible_app", lambda: "/Applications/Host.app")
    monkeypatch.setattr(screen.subprocess, "run", lambda *a, **k: pytest.fail("must not capture"))
    result = mcp_server.screenshot_resolve()
    header = _header(result)
    assert header["ok"] is False and "Screen Recording" in header["hint"]
    assert "/Applications/Host.app" in header["hint"]
    assert len(result.content) == 1


def test_offscreen_windows_are_reported_after_trying_to_focus(monkeypatch, mac):
    monkeypatch.setitem(sys.modules, "Quartz", _quartz([_win(1, "Main", 100, 100, onscreen=False)]))
    focused = []
    monkeypatch.setattr(applescript, "focus_resolve", lambda: focused.append(True) or False)
    monkeypatch.setattr(screen.subprocess, "run", lambda *a, **k: pytest.fail("must not capture"))
    result = mcp_server.screenshot_resolve()
    header = _header(result)
    assert header["ok"] is False and "minimized" in header["error"] and focused == [True]
    assert len(result.content) == 1


def test_no_resolve_window_and_no_quartz(monkeypatch, mac):
    monkeypatch.setitem(sys.modules, "Quartz", _quartz([]))
    assert "No DaVinci Resolve window" in _header(mcp_server.screenshot_resolve())["error"]
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert "[ui]" in _header(mcp_server.screenshot_resolve())["hint"]


def test_capture_failure_is_reported(monkeypatch, mac):
    monkeypatch.setitem(sys.modules, "Quartz", _quartz([_win(1, "Main", 100, 100)]))
    monkeypatch.setattr(screen.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="no"))
    header = _header(mcp_server.screenshot_resolve())
    assert header["ok"] is False and "failed" in header["error"]


def test_other_platforms_get_a_clear_answer(monkeypatch):
    monkeypatch.setattr(mcp_server, "_platform_supports_ui", lambda: False)
    assert "macOS only" in _header(mcp_server.screenshot_resolve())["error"]
