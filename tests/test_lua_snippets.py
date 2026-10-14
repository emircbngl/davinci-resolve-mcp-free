"""Smoke tests for the Lua snippet renderers."""
from __future__ import annotations

import pytest

from resolve_pilot.lua_snippets import library as lua


def test_validate_color_rejects_unknown():
    with pytest.raises(ValueError):
        lua._validate_color("Octarine")


def test_batch_markers_embeds_frames():
    code = lua.snippet_batch_markers([10, 20, 30], color="Red", name_prefix="Cue")
    for f in (10, 20, 30):
        assert f"frame = {f}" in code
    assert 'color = "Red"' in code
    assert 'name = "Cue 3"' in code


def test_set_clip_color_track_and_color():
    code = lua.snippet_set_clip_color(clip_color="Orange", track=2)
    assert '"Orange"' in code
    assert "video" in code
    assert "V%d" in code  # printf-style format string in the Lua source


def test_relink_media_uses_relative_walk():
    code = lua.snippet_relink_media("/Volumes/MyFootage")
    assert "/Volumes/MyFootage" in code
    # The fixed version threads a `rel` argument through the recursive walk.
    assert "walk(sub, rel" in code
    assert "walk(mp:GetRootFolder(), \"\")" in code


def test_list_snippets_matches_renderers():
    names = {s["name"] for s in lua.list_snippets()}
    for n in names:
        assert callable(getattr(lua, f"snippet_{n}", None)), f"missing renderer for {n}"


def test_lua_string_escapes_quotes_backslashes_and_controls():
    assert lua.lua_string('a"b\\c\nd\x01') == '"a\\"b\\\\c\\nd\\001"'
    assert lua.lua_string("İstanbul ş") == '"İstanbul ş"'


def test_to_lua_encodes_nested_values():
    assert lua.to_lua({"a": 1, "b": [1.5, True, None], "end": "x", "skip": None}) == \
        '{a = 1, b = {1.5, true, nil}, ["end"] = "x"}'
    with pytest.raises(ValueError):
        lua.to_lua(float("nan"))


def test_paths_cannot_break_out_of_the_string():
    evil = '/tmp/x]]; os.execute("rm -rf /") --[['
    code = lua.snippet_apply_lut_to_clips(evil)
    assert lua.lua_string(evil) in code
    assert "[[" not in code.replace(lua.lua_string(evil), "")  # no long-bracket strings left


def test_colors_are_normalized_case_insensitively():
    assert lua._validate_color("red") == "Red"
    assert lua.validate_clip_color("teal") == "Teal"
    with pytest.raises(ValueError):
        lua.snippet_set_clip_color("Octarine")


def test_add_markers_requires_a_position():
    with pytest.raises(ValueError):
        lua.snippet_add_markers([{"color": "Blue"}])


def test_list_snippets_reports_parameters():
    by_name = {s["name"]: s for s in lua.list_snippets()}
    params = {p["name"]: p for p in by_name["apply_lut_to_clips"]["params"]}
    assert params["lut_path"]["required"] is True
    assert params["track"] == {"name": "track", "required": False, "default": 1}


def test_render_snippet_validates_arguments():
    assert "AddMarker" in lua.render_snippet("batch_markers", {"frames": [1]})
    with pytest.raises(ValueError, match="Signature"):
        lua.render_snippet("batch_markers", {"frame": [1]})
    with pytest.raises(ValueError, match="Available"):
        lua.render_snippet("nope")


def test_home_paths_are_expanded_before_reaching_resolve():
    code = lua.snippet_render_current_timeline(target_dir="~/Renders")
    assert "~/" not in code and "\"~" not in code  # "~=" is Lua's not-equal


def test_export_timeline_passes_subtypes_for_aaf_and_edl():
    assert "resolve.EXPORT_AAF, resolve.EXPORT_AAF_NEW)" in lua.snippet_export_timeline("/x.aaf", "AAF")
    assert "resolve.EXPORT_EDL, resolve.EXPORT_CDL)" in lua.snippet_export_timeline("/x.edl", "EDL", "CDL")
    assert "resolve.EXPORT_FCPXML_1_10)" in lua.snippet_export_timeline("/x.fcpxml")
    with pytest.raises(ValueError):
        lua.snippet_export_timeline("/x.edl", "EDL", "BOGUS")


def test_grade_from_drx_rejects_modes_resolve_does_not_have():
    with pytest.raises(ValueError, match="start frames aligned"):
        lua.snippet_grade_from_drx("/g.drx", mode=3)
