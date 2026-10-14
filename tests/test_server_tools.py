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
    for name in ("run_lua", "send_keystroke", "menu_pick", "apply_lut", "remove_clips"):
        assert ann[name]["destructiveHint"] is True, name
    for name in ("probe_media", "get_plan", "get_resolve_state", "list_lua_snippets"):
        assert ann[name]["readOnlyHint"] is True, name


# The exact tool surface. An exact set, not a count: a tool that silently fails to
# register (or a stray one) must fail here, not in an agent session.
EXPECTED_TOOLS = {
    # session
    "resolve_pilot_status", "get_workflow_guide", "setup_bridge",
    # analyze
    "probe_media", "detect_scenes", "detect_beats", "transcribe_audio", "analyze_quality",
    # see
    "grab_frames", "scene_frames", "read_video", "screenshot_resolve", "timeline_frames",
    # plan
    "plan_from_transcript", "plan_from_scenes", "create_plan", "get_plan", "edit_plan",
    # export
    "export_fcpxml", "export_srt",
    # resolve
    "get_resolve_state", "import_timeline", "add_markers", "add_quality_markers", "set_clip_color",
    "apply_lut", "queue_render", "render_status", "add_subtitles", "add_title", "checkpoint", "grade_clips", "make_format_version", "open_page", "run_lua", "list_lua_snippets", "render_lua_snippet",
    # live timeline building
    "import_media", "append_clips", "remove_clips",
    # macOS UI
    "send_keystroke", "menu_pick", "open_ai_feature",
    # inspector / tracks
    "set_clip_properties", "set_tracks", "export_timeline",
}


def test_server_registers_exactly_the_expected_tools():
    names = set(_annotations())
    assert len(EXPECTED_TOOLS) == 46
    assert names == EXPECTED_TOOLS, (sorted(EXPECTED_TOOLS - names), sorted(names - EXPECTED_TOOLS))


def test_vision_tools_are_read_only_and_markers_are_not():
    ann = _annotations()
    for name in ("grab_frames", "scene_frames", "read_video", "screenshot_resolve", "analyze_quality"):
        assert ann[name]["readOnlyHint"] is True, name
    assert ann["add_quality_markers"]["readOnlyHint"] is False


def test_timeline_building_hints():
    ann = _annotations()
    assert ann["import_media"]["idempotentHint"] is True and ann["import_media"]["destructiveHint"] is False
    assert ann["append_clips"]["idempotentHint"] is False and ann["append_clips"]["destructiveHint"] is False
    assert ann["remove_clips"]["destructiveHint"] is True


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


def test_version_compare_for_the_21_1_note():
    from resolve_pilot import mcp_server
    assert mcp_server._version_at_least("21.1.0", 21, 1)
    assert mcp_server._version_at_least("22.0", 21, 1)
    assert not mcp_server._version_at_least("20.3.0", 21, 1)
    assert not mcp_server._version_at_least(None, 21, 1)
