"""Lua snippet library — runs inside DaVinci Resolve's built-in Console.

In the Free edition, external scripting is disabled but the Console (Workspace >
Console) still accepts Lua and Python. We generate ready-to-paste snippets that
Claude can hand the user with one line: "paste this into the Resolve Console."
The same code runs unattended through the live bridge (`resolve_pilot.bridge`).

Each snippet returns a string. They are intentionally self-contained — no
external requires beyond Resolve's built-in globals (`resolve`, `fusion`,
`bmd`).
"""
from resolve_pilot.lua_snippets.library import (
    list_snippets,
    lua_string,
    render_snippet,
    snippet_add_markers,
    snippet_apply_lut_to_clips,
    snippet_batch_markers,
    snippet_create_compound_from_selection,
    snippet_dump_project_info,
    snippet_export_timeline,
    snippet_get_state,
    snippet_grade_from_drx,
    snippet_import_timeline,
    snippet_open_page,
    snippet_proxy_generate,
    snippet_relink_media,
    snippet_render_all_timelines,
    snippet_render_current_timeline,
    snippet_set_clip_color,
    to_lua,
)

__all__ = [
    "list_snippets",
    "lua_string",
    "render_snippet",
    "snippet_add_markers",
    "snippet_apply_lut_to_clips",
    "snippet_batch_markers",
    "snippet_create_compound_from_selection",
    "snippet_dump_project_info",
    "snippet_export_timeline",
    "snippet_get_state",
    "snippet_grade_from_drx",
    "snippet_import_timeline",
    "snippet_open_page",
    "snippet_proxy_generate",
    "snippet_relink_media",
    "snippet_render_all_timelines",
    "snippet_render_current_timeline",
    "snippet_set_clip_color",
    "to_lua",
]
