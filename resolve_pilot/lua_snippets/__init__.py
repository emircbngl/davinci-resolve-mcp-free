"""Lua snippet library — runs inside DaVinci Resolve's built-in Console.

In the Free edition, external scripting is disabled but the Console (Workspace >
Console) still accepts Lua and Python. We generate ready-to-paste snippets that
Claude can hand the user with one line: "paste this into the Resolve Console."

Each snippet returns a string. They are intentionally self-contained — no
external requires beyond Resolve's built-in globals (`resolve`, `fusion`,
`bmd`).
"""
from resolve_pilot.lua_snippets.library import (
    snippet_batch_markers,
    snippet_export_timeline,
    snippet_set_clip_color,
    snippet_create_compound_from_selection,
    snippet_render_all_timelines,
    snippet_dump_project_info,
    snippet_apply_lut_to_clips,
    snippet_relink_media,
    snippet_proxy_generate,
    snippet_grade_from_drx,
    list_snippets,
)

__all__ = [
    "snippet_batch_markers",
    "snippet_export_timeline",
    "snippet_set_clip_color",
    "snippet_create_compound_from_selection",
    "snippet_render_all_timelines",
    "snippet_dump_project_info",
    "snippet_apply_lut_to_clips",
    "snippet_relink_media",
    "snippet_proxy_generate",
    "snippet_grade_from_drx",
    "list_snippets",
]
