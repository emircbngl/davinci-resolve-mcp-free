"""Smoke tests for the Lua snippet renderers."""
from __future__ import annotations

import pytest

from resolve_pilot.lua_snippets import library as lua


def test_validate_color_rejects_unknown():
    with pytest.raises(ValueError):
        lua._validate_color("Octarine")


def test_batch_markers_embeds_frames():
    code = lua.snippet_batch_markers([10, 20, 30], color="Red", name_prefix="Cue")
    assert "10, 20, 30" in code
    assert '"Red"' in code


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
