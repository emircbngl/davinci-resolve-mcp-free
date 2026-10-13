"""Server-level behavior that does not need a client round trip."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from resolve_pilot import mcp_server
from resolve_pilot.transcribe import store as transcripts


def _annotations() -> dict[str, dict]:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    return {t.name: t.annotations.model_dump(by_alias=True) for t in tools}


def test_tool_hints_match_what_the_tools_do():
    ann = _annotations()
    for name in ("edit_plan", "create_plan", "plan_from_transcript", "plan_from_scenes",
                 "export_fcpxml", "export_srt"):
        assert ann[name]["idempotentHint"] is False, name
        assert ann[name]["readOnlyHint"] is False, name
    for name in ("run_lua", "send_keystroke", "menu_pick", "apply_lut"):
        assert ann[name]["destructiveHint"] is True, name
    for name in ("probe_media", "get_plan", "get_resolve_state", "list_lua_snippets"):
        assert ann[name]["readOnlyHint"] is True, name


def test_inverted_segments_are_rejected():
    with pytest.raises(ValidationError, match="must be after start"):
        mcp_server.SegmentIn(start=2, end=1, text="x")


def test_default_exports_never_overwrite_user_files(tmp_path, monkeypatch):
    src = tmp_path / "interview.mp4"
    src.write_bytes(b"x")
    mine = tmp_path / "interview.en.srt"
    mine.write_text("1\n00:00:00,000 --> 00:00:01,000\nhand-made\n")
    tid = "t-0123456789ab"
    monkeypatch.setattr(transcripts, "require_transcript", lambda _tid: {
        "source_path": str(src), "language": "en",
        "segments": [{"start": 0.0, "end": 1.0, "text": "whisper"}]})
    out = mcp_server.export_srt(transcript_id=tid)
    assert out["path"] == str(tmp_path / "interview.en (2).srt")
    assert "hand-made" in mine.read_text()
    again = mcp_server.export_srt(transcript_id=tid)
    assert again["path"] == str(tmp_path / "interview.en (3).srt")


def test_studio_only_ui_failures_get_a_studio_hint():
    from resolve_pilot.ui_automation.applescript import StudioOnlyFeature, UIAutomationError

    def studio_only():
        raise StudioOnlyFeature("'Timeline > Detect Scene Cuts' is a Studio-only feature")

    def no_permission():
        raise UIAutomationError("osascript failed (exit 1): not allowed assistive access")

    assert "Studio" in mcp_server._ui_call(studio_only)["hint"]
    assert "Accessibility" in mcp_server._ui_call(no_permission)["hint"]
