"""Workflow guides served over MCP, and channel routing."""
from __future__ import annotations

import re
import subprocess

import pytest

from resolve_pilot import bridge, control, workflows
from tests.conftest import LUAJIT, needs_luajit


def test_every_guide_has_front_matter_and_resolves_by_short_name():
    guides = workflows.list_workflows()
    names = {g["name"] for g in guides}
    assert {"resolve-rough-cut", "resolve-live-control", "resolve-subtitle"} <= names
    assert all(g["description"] for g in guides)
    assert workflows.get_workflow("live-control").startswith("# resolve-live-control")
    with pytest.raises(ValueError, match="Available"):
        workflows.get_workflow("nope")


def test_guides_only_mention_tools_that_exist():
    import asyncio

    from resolve_pilot.mcp_server import mcp

    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    old_names = {"probe_media_tool", "detect_scenes_tool", "export_srt_from_transcript",
                 "plan_roughcut_from_transcript", "studio_add_marker", "args_json"}
    for g in workflows.list_workflows():
        body = workflows.get_workflow(g["name"])
        called = set(re.findall(r"`([a-z_]+)\(", body)) | set(re.findall(r"^([a-z_]+)\(", body, re.M))
        assert not (called & old_names), (g["name"], called & old_names)
        unknown = {c for c in called if c.count("_") and c not in tools}
        assert not unknown, (g["name"], unknown)


@needs_luajit
def test_lua_examples_in_guides_compile(tmp_path):
    for g in workflows.list_workflows():
        for i, block in enumerate(re.findall(r"```lua\n(.*?)```", workflows.get_workflow(g["name"]), re.S)):
            f = tmp_path / f"{g['name']}-{i}.lua"
            f.write_text(block)
            subprocess.run([LUAJIT, "-b", str(f), str(tmp_path / "out")], check=True)


def test_channel_falls_back_to_manual(monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))
    assert control.choose_channel("auto") == "manual"
    with pytest.raises(bridge.BridgeNotRunning):
        control.choose_channel("bridge")
    with pytest.raises(control.ChannelUnavailable, match="Free edition"):
        control.choose_channel("studio")
    with pytest.raises(ValueError):
        control.choose_channel("telepathy")


def test_manual_channel_returns_code_to_paste(monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))
    out = control.perform("print(1)", None)
    assert out["channel"] == "manual" and out["lua_code"] == "print(1)"
    assert "Workspace > Console" in out["instructions"]


def test_studio_channel_is_preferred(monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (True, "ok"))
    out = control.perform("ignored", lambda: {"ok": True, "added": 3})
    assert out == {"channel": "studio", "ok": True, "added": 3}
    # Actions without a Studio implementation skip it.
    assert control.choose_channel("auto", studio_supported=False) == "manual"


@needs_luajit
def test_bridge_channel_and_lua_errors(monkeypatch, live_bridge):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))
    assert control.perform("return {ok = true, n = 2}", None) == {"channel": "bridge", "ok": True, "n": 2}
    with pytest.raises(bridge.BridgeError, match="Lua error inside Resolve"):
        control.perform("error('nope')", None)

