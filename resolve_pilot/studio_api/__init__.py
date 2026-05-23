"""Studio Python scripting API integration.

These tools require **DaVinci Resolve Studio** (paid) with external scripting
enabled in Preferences > System > General > "External scripting using" → Local.

On import, this module tries to wire up the Resolve scripting bridge by
locating fusionscript via the platform-specific paths. If Resolve Studio is
not installed or not running, the tools return a clear error rather than
crash. The Free-edition flow (Lua console + FCPXML + UI automation) keeps
working without this.
"""
from resolve_pilot.studio_api.bridge import (
    StudioNotAvailable,
    get_resolve,
    studio_available,
)
from resolve_pilot.studio_api.actions import (
    list_projects,
    list_timelines,
    timeline_clips_summary,
    add_marker,
    apply_lut,
    queue_render_job,
    set_clip_color,
    project_summary,
)

__all__ = [
    "StudioNotAvailable",
    "get_resolve",
    "studio_available",
    "list_projects",
    "list_timelines",
    "timeline_clips_summary",
    "add_marker",
    "apply_lut",
    "queue_render_job",
    "set_clip_color",
    "project_summary",
]
