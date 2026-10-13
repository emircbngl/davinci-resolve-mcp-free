"""Inspector properties, track control and timeline export: the real snippets against the
mock Resolve through the live bridge, the Studio twins against fakes, and input checks."""
from __future__ import annotations

import pytest

from resolve_pilot import bridge, control
from resolve_pilot.lua_snippets import library as lua
from resolve_pilot.studio_api import actions
from tests.conftest import needs_luajit


@pytest.fixture
def no_studio(monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))


def _clip_props(index: int, *keys: str):
    keys_lua = ", ".join(f'"{k}"' for k in keys)
    return bridge.exec_lua(
        f"local it = mock.project:GetCurrentTimeline().tracks.video[1][{index + 1}] "
        f"local out = {{enabled = it:GetClipEnabled()}} "
        f"for _, k in ipairs({{{keys_lua}}}) do out[k] = it:GetProperty(k) end return out").result


# ── Inspector properties ─────────────────────────────────────────────────────


def test_property_validation():
    rows = lua.normalize_clip_properties({"pan": 10, "zoomx": 2, "Scaling": "Fill",
                                          "CompositeMode": 3, "FlipX": True})
    assert [r["key"] for r in rows] == ["Scaling", "ZoomX", "Pan", "FlipX", "CompositeMode"]
    assert rows[0]["const"] == "SCALE_FILL" and rows[4]["value"] == 3
    assert rows[1]["value"] == 2.0 and isinstance(rows[1]["value"], float)
    for bad, match in [({"Volume": 1}, "Unknown"), ({"Opacity": 101}, "between"),
                       ({"FlipX": 1}, "true or false"), ({"Pan": True}, "number"),
                       ({"Scaling": "squash"}, "fill"), ({"ZoomX": float("inf")}, "number"),
                       ({"CropLeft": -1}, "between")]:
        with pytest.raises(ValueError, match=match):
            lua.normalize_clip_properties(bad)
    with pytest.raises(ValueError, match="Nothing to change"):
        lua.snippet_set_clip_properties()


def test_values_match_tolerates_float_storage_and_numeric_bools():
    assert lua.values_match("number", 1.8, 1.8000001)
    assert not lua.values_match("number", 1.8, 1.0)
    assert not lua.values_match("number", 1.0, None)
    assert lua.values_match("bool", True, 1) and lua.values_match("bool", False, False)
    assert not lua.values_match("bool", True, False)


@needs_luajit
@pytest.mark.parametrize("live_bridge", [None, "21.1"], indirect=True)
def test_set_clip_properties_writes_and_reads_back(live_bridge):
    res = bridge.exec_lua(lua.snippet_set_clip_properties(
        properties={"ZoomX": 1.8, "ZoomY": 1.8, "Pan": -240.5, "Scaling": "fill",
                    "FlipX": True, "Opacity": 50})).result
    assert res["ok"] is True and res["changed"] == 2 and res["clip_count"] == 2
    assert res["resolved_values"] == {"Scaling": 503}
    first = res["clips"][0]
    assert first["index"] == 0 and first["name"] == "A" and first["mismatched"] == []
    assert first["requested"]["Scaling"] == "fill" and first["actual"]["Scaling"] == 503
    assert abs(first["actual"]["ZoomX"] - 1.8) < 1e-3  # stored with float error, still ok
    seen = _clip_props(1, "ZoomX", "Pan", "FlipX", "Opacity")
    assert seen["FlipX"] is True and seen["Opacity"] == pytest.approx(50) and seen["enabled"] is True


@needs_luajit
def test_setter_that_returns_true_without_applying_is_not_ok(live_bridge):
    bridge.exec_lua("local it = mock.project:GetCurrentTimeline().tracks.video[1][2] "
                    "it.SetProperty = function() return true end")
    res = bridge.exec_lua(lua.snippet_set_clip_properties(properties={"Opacity": 25})).result
    assert res["ok"] is False and res["changed"] == 1 and "did not stick" in res["error"]
    good, liar = res["clips"]
    assert good["ok"] is True
    assert liar["ok"] is False and liar["mismatched"] == ["Opacity"] and liar["contradicted"] == ["Opacity"]
    assert liar["actual"]["Opacity"] == 100 and liar["requested"]["Opacity"] == 25


@needs_luajit
def test_disable_one_clip_by_index_and_report_missing_indices(live_bridge):
    assert bridge.exec_lua("return mock.add_transition()").result == 3
    res = bridge.exec_lua(lua.snippet_set_clip_properties(indices=[1, 7], enabled=False)).result
    assert res["ok"] is False and res["missing_indices"] == [7] and res["changed"] == 1
    assert [(c["index"], c["name"], c["actual"]["enabled"]) for c in res["clips"]] == [(1, "B", False)]
    assert _clip_props(0)["enabled"] is True and _clip_props(2)["enabled"] is False  # 2 = B after the dissolve


@needs_luajit
def test_refused_property_and_missing_enum_constant(live_bridge):
    bridge.exec_lua("for _, it in ipairs(mock.project:GetCurrentTimeline().tracks.video[1]) do "
                    "it.props.Distortion = nil end")
    res = bridge.exec_lua(lua.snippet_set_clip_properties(properties={"Distortion": 0.5})).result
    assert res["ok"] is False and res["clips"][0]["mismatched"] == ["Distortion"]
    assert res["clips"][0]["contradicted"] == []  # Resolve said no; no contradiction
    res = bridge.exec_lua(lua.snippet_set_clip_properties(properties={"MotionEstimation": "speed_warp_better"})).result
    assert res["ok"] is False and "MOTION_EST_SPEED_WARP_BETTER" in res["error"]
    empty = bridge.exec_lua(lua.snippet_set_clip_properties(track=1, track_type="audio",
                                                            enabled=False)).result
    assert empty["ok"] is False and "No clips" in empty["error"]


@needs_luajit
def test_set_clip_properties_tool_routes_through_the_bridge(live_bridge, no_studio):
    from resolve_pilot import mcp_server
    out = mcp_server.set_clip_properties(indices=[0], properties={"RotationAngle": 90})
    assert out["channel"] == "bridge" and out["ok"] is True
    assert out["clips"][0]["actual"]["RotationAngle"] == pytest.approx(90)


def test_set_clip_properties_manual_channel_and_bad_input(no_studio):
    from resolve_pilot import mcp_server
    out = mcp_server.set_clip_properties(properties={"Opacity": 0})
    assert out["channel"] == "manual" and "SetProperty" in out["lua_code"]
    with pytest.raises(ValueError, match="Unknown"):
        mcp_server.set_clip_properties(properties={"Zoom": 2})
    with pytest.raises(ValueError, match="Nothing"):
        mcp_server.set_clip_properties()


class _FakeItem:
    def __init__(self, name, lie=False):
        self.name, self.lie = name, lie
        self.props = {"Opacity": 100.0, "ZoomX": 1.0, "Scaling": 0, "FlipX": False}
        self.enabled = True

    def GetName(self):
        return self.name

    def GetMediaPoolItem(self):
        return object()

    def GetProperty(self, key=None):
        return dict(self.props) if key is None else self.props.get(key)

    def SetProperty(self, key, value):
        if key not in self.props:
            return False
        if not self.lie:
            self.props[key] = value
        return True

    def GetClipEnabled(self):
        return self.enabled

    def SetClipEnabled(self, on):
        self.enabled = on
        return True


class _FakeResolve:
    SCALE_FILL = 3


def test_studio_twin_mirrors_the_lua(monkeypatch):
    items = [_FakeItem("A"), _FakeItem("B", lie=True)]

    class TL:
        def GetItemListInTrack(self, kind, index):
            return items if (kind, index) == ("video", 1) else []

        def GetName(self):
            return "Main"

    monkeypatch.setattr(actions, "get_resolve", lambda: _FakeResolve())
    monkeypatch.setattr(actions, "_current_timeline", lambda: TL())
    out = actions.set_clip_properties(properties={"ZoomX": 2, "Scaling": "fill"}, enabled=False)
    assert out["ok"] is False and out["changed"] == 1 and out["resolved_values"] == {"Scaling": 3}
    a, b = out["clips"]
    assert a == {"index": 0, "name": "A", "requested": {"Scaling": "fill", "ZoomX": 2, "enabled": False},
                 "actual": {"Scaling": 3, "ZoomX": 2.0, "enabled": False}, "mismatched": [],
                 "contradicted": [], "ok": True}
    assert b["mismatched"] == ["Scaling", "ZoomX"] and b["contradicted"] == ["Scaling", "ZoomX"]
    out = actions.set_clip_properties(indices=[5], properties={"Opacity": 1})
    assert out["ok"] is False and out["missing_indices"] == [5] and "No clips" in out["error"]
