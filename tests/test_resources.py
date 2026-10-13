"""MCP resources (traps, capabilities, state, guides) and the known-traps ledger."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from resolve_pilot import __version__, bridge, mcp_server, traps, workflows
from tests.conftest import LUAJIT, start_bridge, stop_bridge
from tests.test_server_e2e import connect


def _field(obj, *names):
    for n in names:  # mcp 1.x uses camelCase attributes, 2.x snake_case
        if hasattr(obj, n):
            return getattr(obj, n)
    raise AttributeError(names)


async def _read(session, uri: str) -> str:
    res = await session.read_resource(uri)
    return res.contents[0].text


def test_resources_over_a_real_client_session(isolated_home, tmp_path):
    asyncio.run(_session(isolated_home, tmp_path))


async def _session(home, tmp_path) -> None:
    async with connect(home) as (agent, init):
        assert "resolve://traps" in (init.instructions or "")
        s = agent.s

        uris = {str(r.uri) for r in (await s.list_resources()).resources}
        assert {"resolve://traps", "resolve://capabilities", "resolve://state",
                "resolve://guide/rough-cut", "resolve://guide/live-control"} <= uris
        templates = _field(await s.list_resource_templates(), "resourceTemplates", "resource_templates")
        assert "resolve://guide/{name}" in {_field(t, "uriTemplate", "uri_template") for t in templates}

        md = await _read(s, "resolve://traps")
        assert all(t.title in md for t in traps.TRAPS)

        caps = json.loads(await _read(s, "resolve://capabilities"))
        tools = {t.name for t in (await s.list_tools()).tools}
        assert set(caps["tools"]) == tools
        assert caps["tools"]["run_lua"]["channels"] == ["bridge"]
        assert caps["tools"]["add_markers"]["channels"] == ["studio", "bridge", "manual"]
        assert caps["tools"]["probe_media"]["needs_resolve"] is False
        assert caps["tools"]["screenshot_resolve"]["macos_only"] is True
        assert caps["tools"]["remove_clips"]["destructive"] is True
        assert "21.1" in caps["editions"]["resolve_21_1_free"]

        start = time.monotonic()
        state = json.loads(await _read(s, "resolve://state"))
        assert time.monotonic() - start < 5
        assert state["connected"] is False and state["channel"] == "manual"
        assert any("setup_bridge" in step for step in state["next_steps"])

        assert (await _read(s, "resolve://guide/rough-cut")).startswith("# resolve-rough-cut")
        assert (await _read(s, "resolve://guide/traps")).startswith("# Known traps")
        assert "Known traps" in (await agent.call("get_workflow_guide", name="traps"))["guide"]
        prompts = {p.name for p in (await s.list_prompts()).prompts}
        assert "resolve-rough-cut" in prompts and "traps" not in prompts

        if not LUAJIT:
            return
        proc = start_bridge(tmp_path)
        try:
            state = json.loads(await _read(s, "resolve://state"))
            assert state["connected"] is True and state["channel"] == "bridge"
            assert "current_timeline" in state

            ran = await agent.call("run_lua", code=(
                "local backup = function() return rp.project():DuplicateTimeline('b') end\nreturn 1"))
            assert ran["result"] == 1
            assert any("duplicate-timeline-becomes-current" in h for h in ran["known_traps"])
            ran = await agent.call("run_lua", code="return 2")
            assert "known_traps" not in ran
        finally:
            stop_bridge(proc)


def test_capabilities_cover_exactly_the_registered_tools():
    registered = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    caps = mcp_server.capabilities()
    assert set(caps["tools"]) == registered
    assert set(mcp_server._TOOL_CHANNELS) <= registered
    assert mcp_server._MACOS_ONLY <= registered
    json.dumps(caps)  # serializable


def test_trap_ledger_is_consistent():
    registered = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    ids = [t.id for t in traps.TRAPS]
    assert len(ids) == len(set(ids))
    for t in traps.TRAPS:
        assert set(t.tools) <= registered, (t.id, set(t.tools) - registered)
        assert t.symptom and t.cause and t.handling
        assert (t.source == "measured") == bool(t.measured_on), t.id
    md = traps.to_markdown()
    assert md.count("\n## ") == len(traps.TRAPS)
    assert "reported, not yet measured here" in md and "measured on Resolve 20.3" in md


def test_traps_guide_is_listed():
    assert "traps" in {g["name"] for g in workflows.list_workflows()}
    assert workflows.get_workflow("traps") == traps.to_markdown()


def test_lua_hints_only_for_called_methods():
    assert traps.hints_for_lua("tl:DeleteClips({it}, false)")[0].startswith(
        "Known trap (deleteclips-needs-edit-page)")
    hints = traps.hints_for_lua("tl:AppendToTimeline({{mediaPoolItem = it}})")
    assert len(hints) == 2  # subtitle crash + exclusive endFrame
    assert traps.hints_for_lua("print('DeleteClips is risky')") == []
    assert traps.hints_for_lua("return 1") == []


def test_bridge_timeout_mentions_a_modal_dialog():
    # A fresh heartbeat but nothing reading requests: the request times out.
    (bridge.bridge_dir() / "heartbeat").write_text(f"{time.time()} {__version__}", encoding="utf-8")
    with pytest.raises(bridge.BridgeNotRunning, match="modal dialog"):
        bridge.exec_lua("return 1", timeout=0.3)


def test_stale_heartbeat_mentions_a_modal_dialog():
    (bridge.bridge_dir() / "heartbeat").write_text(f"{time.time() - 60} {__version__}", encoding="utf-8")
    st = bridge.status()
    assert not st.running and "modal dialog" in st.detail


def test_state_resource_never_hangs(monkeypatch):
    monkeypatch.setattr(mcp_server, "STATE_DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(mcp_server, "_state_snapshot", lambda: time.sleep(1.5) or {})
    start = time.monotonic()
    out = json.loads(asyncio.run(mcp_server._bounded_state()))
    assert out["connected"] is False and "did not answer" in out["error"]
    assert time.monotonic() - start < 2.5


def test_state_snapshot_reports_errors_instead_of_raising(monkeypatch):
    def boom(*a, **k):
        raise bridge.BridgeError("Resolve took the request but did not answer")
    monkeypatch.setattr(mcp_server.control, "choose_channel", lambda prefer="auto", **k: "bridge")
    monkeypatch.setattr(mcp_server.control, "perform", boom)
    out = mcp_server._state_snapshot()
    assert out["connected"] is False and "did not answer" in out["error"] and out["next_steps"]


def test_tools_without_a_studio_path_do_not_claim_studio():
    tools = mcp_server.capabilities()["tools"]
    assert tools["add_title"]["channels"] == ["bridge", "manual"]
    assert tools["checkpoint"]["channels"] == ["bridge", "manual"]
    assert traps.hints_for_lua('tl:InsertFusionTitleIntoTimeline("Text+")')[0].startswith(
        "Known trap (fusion-title-insert-edit)")
