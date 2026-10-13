"""Quality spans as timeline markers, through the same channels as add_markers."""
from __future__ import annotations

import pytest

from resolve_pilot import bridge, control, mcp_server
from resolve_pilot.analyzers.quality import Span
from resolve_pilot.lua_snippets import library as lua
from resolve_pilot.studio_api import actions


@pytest.fixture
def no_studio(monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))


def test_spans_are_placed_relative_to_the_clip(tmp_path):
    src = tmp_path / "interview.mov"
    src.touch()
    spans = [Span(1, 2, "silence", 1), Span(9, 12, "black", 1), Span(30, 31, "loud", 0.5),
             Span(0, 0.5, "freeze", 1)]
    markers, outside, prefix = mcp_server.quality_markers(
        spans, str(src), timeline_start_seconds=100, source_in_seconds=5, source_out_seconds=20)
    assert outside == 3  # before the in-point (two) and after the out-point (one)
    (m,) = markers
    assert m["seconds"] == 104.0 and m["duration_seconds"] == 3.0  # 9-12 s of media at 100 + (9-5)
    assert m["color"] == "Cocoa" and m["name"] == "Black"
    assert m["custom_data"].startswith(prefix) and prefix.startswith("resolve-pilot:quality:")


def test_marker_identity_is_stable_and_scoped_per_source(tmp_path):
    a, b = tmp_path / "a.mov", tmp_path / "b.mov"
    spans = [Span(1, 2, "silence", 1)]
    first = mcp_server.quality_markers(spans, str(a))
    assert first == mcp_server.quality_markers(spans, str(a))
    other = mcp_server.quality_markers(spans, str(b))
    assert other[2] != first[2] and other[0][0]["custom_data"] != first[0][0]["custom_data"]


@pytest.mark.parametrize("prefix", ["x", "resolve-pilot:", "my-markers:"])
def test_only_a_resolve_pilot_namespace_can_be_replaced(prefix):
    # "resolve-pilot:" alone would match every marker any resolve-pilot tool made.
    with pytest.raises(ValueError, match="resolve-pilot:"):
        lua.snippet_add_markers([{"frame": 1}], replace_owned_prefix=prefix)


def test_manual_channel_hands_back_lua_that_replaces_only_owned_markers(tmp_path, no_studio):
    src = tmp_path / "a.mov"
    src.touch()
    out = mcp_server.add_quality_markers(
        str(src), spans=[mcp_server.QualitySpanIn(start=1, end=3, kind="silence")])
    assert out["channel"] == "manual"
    code = out["lua_code"]
    assert "DeleteMarkerByCustomData" in code and "resolve-pilot:quality:" in code
    assert "DeleteMarkersByColor" not in code
    assert "duration_seconds" in code


def test_nothing_to_mark_is_not_an_error(tmp_path, no_studio):
    out = mcp_server.add_quality_markers(
        str(tmp_path / "a.mov"), spans=[mcp_server.QualitySpanIn(start=1, end=1.2, kind="black")])
    assert out["added"] == 0 and "No quality spans" in out["note"]


def test_rerun_through_the_bridge_replaces_its_markers_and_keeps_the_users(
        live_bridge, tmp_path, no_studio):
    src = tmp_path / "a.mov"
    src.touch()
    user = bridge.exec_lua(lua.snippet_add_markers([{"frame": 200, "name": "mine"}])).result
    assert user["added"] == 1

    spans = [mcp_server.QualitySpanIn(start=1, end=2, kind="silence"),
             mcp_server.QualitySpanIn(start=4, end=6.5, kind="black", score=0.9)]
    first = mcp_server.add_quality_markers(str(src), spans=spans, timeline_start_seconds=1)
    assert first["channel"] == "bridge" and first["added"] == 2 and first["removed_previous"] == 0
    second = mcp_server.add_quality_markers(str(src), spans=spans, timeline_start_seconds=1)
    assert second["added"] == 2 and second["removed_previous"] == 2 and second["skipped_frames"] == []

    state = bridge.exec_lua(lua.snippet_get_state(include_markers=True)).result
    marks = {m["frame"]: m for m in state["current_timeline"]["markers"]}
    assert sorted(marks) == [48, 120, 200]  # 24 fps: 2 s, 5 s, and the user's frame 200
    assert marks[120]["duration"] == 60 and marks[120]["color"] == "Cocoa"
    assert marks[200]["name"] == "mine"


class _FakeTimeline:
    def __init__(self):
        self.markers = {10: {"name": "mine", "customData": ""},
                        20: {"name": "old", "customData": "resolve-pilot:quality:abcd1234:black:x"},
                        30: {"name": "other file", "customData": "resolve-pilot:quality:ffff0000:black:y"}}

    def GetMarkers(self):
        return dict(self.markers)

    def DeleteMarkerByCustomData(self, data):
        for frame, m in list(self.markers.items()):
            if m["customData"] == data:
                del self.markers[frame]
                return True
        return False

    def AddMarker(self, frame, color, name, note, duration, data):
        if frame in self.markers:
            return False
        self.markers[frame] = {"name": name, "customData": data, "duration": duration}
        return True

    def GetName(self):
        return "TL"


def test_studio_path_mirrors_the_lua(monkeypatch):
    tl = _FakeTimeline()
    monkeypatch.setattr(actions, "_current_project", lambda: object())
    monkeypatch.setattr(actions, "_current_timeline", lambda: tl)
    monkeypatch.setattr(actions, "_timeline_fps", lambda p, t: 25.0)
    rows = lua.normalize_markers([{"seconds": 2, "duration_seconds": 1.5, "custom_data": "resolve-pilot:quality:abcd1234:black:z"}])
    out = actions.add_markers_batch(rows, "resolve-pilot:quality:abcd1234:")
    assert out["removed_previous"] == 1 and out["added"] == 1
    assert sorted(tl.markers) == [10, 30, 50]
    assert tl.markers[50]["duration"] == 38


def test_spans_starting_on_the_same_frame_become_one_marker(tmp_path):
    src = tmp_path / "a.mp4"
    src.write_text("")
    spans = [Span(0.0, 2.0, "black", 1.0), Span(0.01, 1.0, "loud", 1.0), Span(3.0, 4.0, "silence", 1.0)]
    markers, _, _ = mcp_server.quality_markers(spans, str(src))
    assert [m["name"] for m in markers] == ["Black + Loud", "Silence"]
    assert markers[0]["duration_seconds"] == 2.0 and "loud" in markers[0]["note"]
