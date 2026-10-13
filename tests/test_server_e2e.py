"""End-to-end: a real MCP client talks to the server over stdio.

Runs against whichever mcp SDK is installed (CI covers 1.x and 2.x) and checks
what an agent actually sees: tool results, error messages, and the full
analyze → plan → edit → export → import → verify loop through the live bridge.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.conftest import LUAJIT, start_bridge, stop_bridge


def _is_error(res) -> bool:
    return bool(res.isError if hasattr(res, "isError") else res.is_error)


def _text(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in res.content)


class Agent:
    def __init__(self, session: ClientSession):
        self.s = session

    async def call(self, tool: str, /, **args: Any) -> dict:
        res = await self.s.call_tool(tool, args)
        assert not _is_error(res), f"{tool} failed: {_text(res)}"
        return json.loads(res.content[0].text)

    async def fail(self, tool: str, /, **args: Any) -> str:
        res = await self.s.call_tool(tool, args)
        assert _is_error(res), f"{tool} unexpectedly succeeded: {_text(res)}"
        return _text(res)


@asynccontextmanager
async def connect(home: Path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "resolve_pilot.mcp_server"],
        env={"RESOLVE_PILOT_HOME": str(home), "PATH": os.environ.get("PATH", "")},
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            yield Agent(session), init


def test_agent_session_end_to_end(media, isolated_home, tmp_path):
    asyncio.run(_session(media, isolated_home, tmp_path))


async def _session(media, home: Path, tmp_path: Path) -> None:
    clip, song = str(media["clip"]), str(media["song"])
    async with connect(home) as (agent, init):
        assert "resolve_pilot_status" in (init.instructions or "")

        status = await agent.call("resolve_pilot_status")
        assert status["channel"] == "manual" and status["ffmpeg"] is True
        assert any("setup_bridge" in s for s in status["next_steps"])

        # Errors arrive as readable text, also on mcp 2.x which masks plain exceptions.
        msg = await agent.fail("probe_media", path=str(tmp_path / "missing.mov"))
        assert "File not found" in msg

        info = await agent.call("probe_media", path=clip)
        assert info["fps"] == 24 and info["width"] == 320

        scenes = await agent.call("detect_scenes", path=clip)
        assert any(abs(c["pts_seconds"] - 3.0) < 0.1 for c in scenes["cuts"])

        beats = await agent.call("detect_beats", path=song)
        assert beats["tempo_bpm_estimate"] == pytest.approx(120, abs=3)

        guide = await agent.call("get_workflow_guide", name="rough-cut")
        assert "plan_id" in guide["guide"]

        # Plan with a music bed, stored server-side.
        plan = await agent.call(
            "create_plan", title="Promo",
            clips=[
                {"source_path": clip, "in_seconds": 0.5, "out_seconds": 2.5, "rationale": "pattern"},
                {"source_path": clip, "in_seconds": 3.5, "duration_seconds": 2.0},
            ],
            audio_bed={"source_path": song},
        )
        pid = plan["plan_id"]
        assert plan["fps"] == 24 and plan["clip_count"] == 2 and plan["errors"] == []
        assert plan["sources"] == [str(Path(clip).resolve())] or plan["sources"] == [clip]

        edited = await agent.call("edit_plan", plan_id=pid, operations=[
            {"op": "trim", "index": 0, "tail_seconds": 0.5},
            {"op": "set", "default_transition_seconds": 0.5, "fade_out_seconds": 0.5},
            {"op": "move", "index": 1, "to": 0},
        ])
        assert edited["changes"][0].startswith("trimmed clip 0")
        assert [c["in_seconds"] for c in edited["clips"]] == [3.5, 0.5]
        assert edited["total_seconds"] == 4.0

        msg = await agent.fail("edit_plan", plan_id=pid, operations=[{"op": "remove", "indices": [9]}])
        assert "out of range" in msg and "No changes were saved" in msg

        # Claude Desktop sometimes sends nested arguments as JSON strings.
        res = await agent.s.call_tool("edit_plan", {
            "plan_id": pid, "operations": json.dumps([{"op": "set", "title": "Promo v2"}])})
        assert not _is_error(res), _text(res)

        exported = await agent.call("export_fcpxml", plan_id=pid, out_path=str(tmp_path / "promo.fcpxml"))
        root = ET.parse(exported["path"]).getroot()
        assert root.find(".//project").get("name") == "Promo v2"
        bed = [c for c in root.iter("asset-clip") if c.get("lane") == "-1"]
        assert len(bed) == 1
        assert len(list(root.iter("transition"))) == 2  # one cut dissolve + the fade out

        state = await agent.call("get_resolve_state")
        assert state["channel"] == "manual" and "lua_code" in state

        manual = await agent.call("add_markers", markers=[{"seconds": 1, "color": "red"}])
        assert manual["channel"] == "manual" and "AddMarker" in manual["lua_code"]

        srt = await agent.call("export_srt", out_path=str(tmp_path / "subs.srt"), max_chars_per_line=20,
                               segments=[{"start": 0, "end": 4, "text": "one two three four five six seven "
                                                                      "eight nine ten eleven twelve"}])
        assert srt["cue_count"] == 2

        msg = await agent.fail("run_lua", code="return 1")
        assert "Workspace > Scripts" in msg

        if not LUAJIT:
            return
        proc = start_bridge(tmp_path)
        try:
            status = await agent.call("resolve_pilot_status")
            assert status["channel"] == "bridge"

            imported = await agent.call("import_timeline", path=exported["path"], timeline_name="Promo")
            assert imported["channel"] == "bridge" and imported["ok"] and imported["timeline"] == "Promo"

            state = await agent.call("get_resolve_state", include_clips=True)
            assert state["current_timeline"]["name"] == "Promo"

            added = await agent.call("add_markers", markers=[{"seconds": 0.5, "color": "Green", "name": "hook"}])
            assert added["added"] == 1 and added["channel"] == "bridge"

            ran = await agent.call("run_lua", code="print(rp.timeline():GetName()) return rp.fps()")
            assert ran["ok"] and ran["result"] == 24 and ran["output"] == "Promo"

            ran = await agent.call("run_lua", snippet="set_clip_color", args={"clip_color": "Teal"})
            assert ran["result"]["colored"] == 1

            assert (await agent.call("open_page", page="color"))["ok"] is True
        finally:
            stop_bridge(proc)


def test_agent_sees_frames_as_images(media, isolated_home):
    asyncio.run(_frames_session(media, isolated_home))


async def _frames_session(media, home: Path) -> None:
    async with connect(home) as (agent, _init):
        res = await agent.s.call_tool("grab_frames", {"path": str(media["clip"]), "at_seconds": [1, 4, 99]})
        assert not _is_error(res), _text(res)
        header = json.loads(res.content[0].text)
        assert header["returned"] == 2 and header["dropped"][0]["index"] == 2
        kinds = [c.type for c in res.content]
        assert kinds == ["text", "text", "image", "text", "image"]
        image = res.content[2]
        assert (getattr(image, "mimeType", None) or image.mime_type) == "image/jpeg"

        res = await agent.s.call_tool("scene_frames", {"path": str(media["clip"])})
        assert not _is_error(res), _text(res)
        assert json.loads(res.content[0].text)["scene_count"] == 2
        assert [c.type for c in res.content] == ["text", "text", "image"]
