"""resolve-pilot MCP server.

Exposes the editing toolkit over Model Context Protocol so Claude Desktop /
Claude Code can drive a video editing session end-to-end without an API key.

Tools are grouped:
- inspect:    probe_media, detect_scenes, list_lua_snippets
- transcribe: transcribe_audio
- plan:       plan_from_transcript, plan_from_scenes
- export:     export_fcpxml, export_srt, render_lua_snippet
- ui:         is_resolve_running, focus_resolve, open_page, send_keystroke, menu_pick
- workspace:  resolve_pilot_status

Run as:
    python -m resolve_pilot.mcp_server
or via Claude Desktop config (see README).
"""
from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from resolve_pilot.analyzers import detect_beats, detect_scenes, probe_media
from resolve_pilot.editorial.roughcut import (
    RoughCutPlan,
    plan_from_scenes,
    plan_from_transcript,
    plan_to_timeline_spec,
)
from resolve_pilot.exporters.fcpxml import write_fcpxml
from resolve_pilot.exporters.srt import SrtCue, write_srt
from resolve_pilot.lua_snippets import library as lua_lib
from resolve_pilot.transcribe import Segment

mcp = FastMCP("resolve-pilot")


# ────────────────────────────────────────────────────────────────────────────
# Inspection
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def probe_media_tool(path: str) -> dict:
    """Return container, codecs, fps, resolution, duration for a video/audio file.

    Use this first whenever you need to know what you're working with.
    """
    info = probe_media(path)
    return info.to_dict()


@mcp.tool()
def detect_scenes_tool(path: str, threshold: float = 10.0) -> dict:
    """Detect scene cuts in a video file using ffmpeg's scdet filter.

    threshold: 0-100. Lower → more sensitive (e.g. 8 catches lighting changes).
    Higher (e.g. 30) only returns hard cuts.

    Returns a list of {pts_seconds, score} entries.
    """
    cuts = detect_scenes(path, threshold=threshold)
    return {"count": len(cuts), "cuts": [c.to_dict() for c in cuts]}


@mcp.tool()
def detect_beats_tool(
    path: str,
    min_seconds_between: float = 0.20,
    energy_threshold_db: float = -25.0,
) -> dict:
    """Detect beats / onsets in an audio or video file (energy-based).

    For music-video editing: pass the song's path, filter by strength, then
    use the timestamps as cut points.

    min_seconds_between: smallest allowed gap between beats (0.20 ≈ 300 BPM cap).
    energy_threshold_db: relative to peak. Higher (less negative) → fewer, stronger beats.
    """
    beats = detect_beats(path, min_seconds_between=min_seconds_between,
                         energy_threshold_db=energy_threshold_db)
    return {"count": len(beats), "beats": [b.to_dict() for b in beats]}


# ────────────────────────────────────────────────────────────────────────────
# Transcription
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def transcribe_audio(
    path: str,
    model: str = "mlx-community/whisper-large-v3-turbo",
    language: str | None = None,
    word_timestamps: bool = False,
) -> dict:
    """Transcribe a media file with local mlx-whisper (Apple Silicon).

    First call downloads the model (~1.5GB for large-v3-turbo). Subsequent runs
    are fast — turbo transcribes faster than realtime on M-series.
    """
    from resolve_pilot.transcribe import transcribe
    result = transcribe(path, model=model, language=language,
                        word_timestamps=word_timestamps)
    return result.to_dict()


# ────────────────────────────────────────────────────────────────────────────
# Roughcut planning
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def plan_roughcut_from_transcript(
    source_path: str,
    segments_json: str,
    title: str = "Transcript Roughcut",
    pad_seconds: float = 0.2,
    fps: float | None = None,
) -> dict:
    """Build a draft rough cut from transcript segments — one clip per spoken segment.

    segments_json: JSON array of {start, end, text} (the output of transcribe_audio
    can be passed straight through after json.dumps on the 'segments' field).

    The returned plan is meant to be edited by you (Claude) — drop bad takes,
    re-order, tighten — and then handed to export_fcpxml.
    """
    raw = json.loads(segments_json)
    segs = [Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]))
            for s in raw]
    plan = plan_from_transcript(source_path, segs, title=title,
                                pad_seconds=pad_seconds, fps=fps)
    return plan.to_dict()


@mcp.tool()
def plan_roughcut_from_scenes(
    source_path: str,
    cuts_json: str,
    title: str = "Scene Roughcut",
    min_duration: float = 1.0,
    fps: float | None = None,
) -> dict:
    """Build a draft rough cut from scene cuts — one clip per detected scene.

    cuts_json: JSON array of {pts_seconds, score} (the 'cuts' field from
    detect_scenes_tool).
    """
    from resolve_pilot.analyzers import SceneCut
    raw = json.loads(cuts_json)
    cuts = [SceneCut(pts_seconds=float(c["pts_seconds"]),
                     score=float(c.get("score", 0.0))) for c in raw]
    plan = plan_from_scenes(source_path, cuts, title=title,
                            min_duration=min_duration, fps=fps)
    return plan.to_dict()


# ────────────────────────────────────────────────────────────────────────────
# Export
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def apply_default_transitions(
    plan_json: str,
    transition_seconds: float = 0.5,
    fade_in_seconds: float = 0.0,
    fade_out_seconds: float = 0.0,
) -> dict:
    """Add cross-dissolve transitions and/or fades to a rough-cut plan.

    transition_seconds: Cross Dissolve duration applied at every cut between
        adjacent clips. Set to 0 to leave cuts hard. Per-clip overrides set via
        `clip.transition_out_seconds` always win.
    fade_in_seconds:  Fade up from black at the start (0 disables).
    fade_out_seconds: Fade to black at the end (0 disables).

    Returns the modified plan ready to hand to export_fcpxml.
    """
    plan = RoughCutPlan.from_dict(json.loads(plan_json))
    plan.default_transition_seconds = max(0.0, float(transition_seconds))
    plan.fade_in_seconds = max(0.0, float(fade_in_seconds))
    plan.fade_out_seconds = max(0.0, float(fade_out_seconds))
    return plan.to_dict()


@mcp.tool()
def export_fcpxml(plan_json: str, out_path: str) -> dict:
    """Write a RoughCutPlan to disk as FCPXML 1.10.

    plan_json: the full plan structure (as returned by plan_roughcut_*).
    out_path: where to write. Resolve imports via File > Import > Timeline.

    Works in Free and Studio editions of Resolve.
    """
    plan = RoughCutPlan.from_dict(json.loads(plan_json))
    spec = plan_to_timeline_spec(plan)
    final = write_fcpxml(spec, out_path)
    return {"path": str(final), "clip_count": len(plan.clips), "title": plan.title}


@mcp.tool()
def export_srt_from_transcript(transcript_json: str, out_path: str) -> dict:
    """Write transcript segments to an SRT subtitle file.

    transcript_json: the full transcribe_audio result (we use the 'segments' field).
    """
    data = json.loads(transcript_json)
    cues = [SrtCue(start=float(s["start"]), end=float(s["end"]),
                   text=str(s["text"])) for s in data["segments"]]
    final = write_srt(cues, out_path)
    return {"path": str(final), "cue_count": len(cues)}


# ────────────────────────────────────────────────────────────────────────────
# Lua snippets for Resolve Console (works in Free!)
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_lua_snippets() -> list[dict]:
    """List every Lua snippet ready to paste into Resolve's Console.

    These work in DaVinci Resolve Free (where external scripting is disabled)
    because the Console is built into the app. User opens Workspace > Console,
    pastes, runs.
    """
    return lua_lib.list_snippets()


@mcp.tool()
def render_lua_snippet(
    snippet: str,
    args_json: str = "{}",
) -> dict:
    """Render a parameterized Lua snippet ready for paste into Resolve Console.

    snippet: one of the names from list_lua_snippets.
    args_json: JSON object with snippet-specific parameters.

    Returns {snippet, code} — the user pastes 'code' into Workspace > Console
    inside DaVinci Resolve.
    """
    args: dict[str, Any] = json.loads(args_json) if args_json else {}
    name = snippet.strip()
    fn_name = f"snippet_{name}"
    fn = getattr(lua_lib, fn_name, None)
    if not fn or not callable(fn):
        raise ValueError(
            f"Unknown snippet '{snippet}'. Available: "
            f"{', '.join(s['name'] for s in lua_lib.list_snippets())}"
        )
    code = fn(**args)
    return {"snippet": name, "code": code}


# ────────────────────────────────────────────────────────────────────────────
# UI automation (macOS) — covers what the scripting API can't reach
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def is_resolve_running_tool() -> dict:
    """Check whether DaVinci Resolve is currently running. macOS only."""
    if platform.system() != "Darwin":
        return {"running": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation import is_resolve_running
    return {"running": is_resolve_running()}


@mcp.tool()
def focus_resolve_tool() -> dict:
    """Bring DaVinci Resolve to the front. macOS only."""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation import focus_resolve
    focus_resolve()
    return {"ok": True}


@mcp.tool()
def open_page_tool(page: str) -> dict:
    """Switch Resolve to a page: media, cut, edit, fusion, color, fairlight, deliver.

    Works in the Free edition because it sends a keyboard shortcut.
    """
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation import open_page
    open_page(page)  # type: ignore[arg-type]
    return {"ok": True, "page": page}


@mcp.tool()
def send_keystroke_tool(key: str, modifiers: str = "") -> dict:
    """Send a keystroke to Resolve (must be frontmost). macOS only.

    Examples:
        key='s', modifiers='command' → save
        key='return', modifiers=''   → confirm dialog
        key='space'                  → play/pause
    """
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation import send_keystroke
    send_keystroke(key, modifiers)
    return {"ok": True}


@mcp.tool()
def apply_video_transition_tool() -> dict:
    """Apply Resolve's default video transition (Cmd+T) to the selected edits.

    The Edit page must be open with one or more cuts selected (clips, edits, or
    a range covering them). Resolve adds the default transition — Cross
    Dissolve by default — to every selected edit. macOS only; works in both
    Free and Studio editions.
    """
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation import send_keystroke
    send_keystroke("t", "command")
    return {"ok": True, "action": "Cmd+T → Apply Video Transition"}


@mcp.tool()
def menu_pick_tool(menu_path_json: str) -> dict:
    """Click a Resolve menu item by path.

    menu_path_json: JSON array, e.g. '["File", "Import", "Timeline..."]'.
    macOS only. Requires Accessibility permission on first run.
    """
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation import menu_pick
    path = json.loads(menu_path_json)
    menu_pick(path)
    return {"ok": True, "path": path}


# ────────────────────────────────────────────────────────────────────────────
# AI features via UI automation (Magic Mask, Smart Reframe, etc.)
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def trigger_magic_mask(mode: str = "person") -> dict:
    """Open Magic Mask on the Color page for the selected clip.

    mode: 'person' (default) is the recommended starting point. After opening,
    Resolve waits for the user (or further UI clicks) to indicate the subject.
    Returns step-by-step instructions for the click-paint phase.
    """
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation.ai_features import trigger_magic_mask_person
    return trigger_magic_mask_person()


@mcp.tool()
def trigger_smart_reframe_tool(target_aspect: str = "9:16") -> dict:
    """Open Smart Reframe with a target aspect.

    target_aspect: '1:1', '9:16', '4:5', '16:9'. The dialog opens; the user
    confirms in the GUI because aspect popup clicking is per-build fragile.
    """
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation.ai_features import trigger_smart_reframe
    return trigger_smart_reframe(target_aspect=target_aspect)  # type: ignore[arg-type]


@mcp.tool()
def trigger_voice_isolation_tool(intensity: int = 60) -> dict:
    """Open the Voice Isolation dialog. Studio-only feature."""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation.ai_features import trigger_voice_isolation
    return trigger_voice_isolation(intensity=intensity)


@mcp.tool()
def trigger_scene_cut_detection_tool() -> dict:
    """Open Resolve's built-in scene cut detection dialog."""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation.ai_features import trigger_scene_cut_detection
    return trigger_scene_cut_detection()


@mcp.tool()
def trigger_auto_subtitle_tool(language: str = "en") -> dict:
    """Open the Create Subtitles from Audio dialog (Studio-only AI engine)."""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "UI automation is macOS-only"}
    from resolve_pilot.ui_automation.ai_features import trigger_auto_subtitle
    return trigger_auto_subtitle(language=language)


# ────────────────────────────────────────────────────────────────────────────
# Studio Python API (Studio-only)
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def studio_probe() -> dict:
    """Check whether the Studio Python scripting bridge is reachable.

    Returns {available: bool, reason: str}. Free edition always returns
    available=False. If Studio is installed and running but the bridge fails,
    the reason field explains why (env vars, external scripting disabled, etc.).
    """
    try:
        from resolve_pilot.studio_api import studio_available, get_resolve
        if not studio_available():
            return {"available": False, "reason": "bridge load failed"}
        r = get_resolve()
        return {
            "available": True,
            "version": r.GetVersionString() if hasattr(r, "GetVersionString") else None,
            "product": r.GetProduct() if hasattr(r, "GetProduct") else None,
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


@mcp.tool()
def studio_project_summary() -> dict:
    """One-call snapshot of the currently-open project — Studio only."""
    from resolve_pilot.studio_api import project_summary
    return project_summary()


@mcp.tool()
def studio_list_timelines() -> dict:
    """List all timelines in the open project with track counts and frame extents."""
    from resolve_pilot.studio_api import list_timelines
    return {"timelines": list_timelines()}


@mcp.tool()
def studio_timeline_clips(track_type: str = "video", track_index: int = 1) -> dict:
    """Per-clip summary on V<n> or A<n> of the active timeline. Studio only."""
    from resolve_pilot.studio_api import timeline_clips_summary
    return {
        "track_type": track_type, "track_index": track_index,
        "clips": timeline_clips_summary(track_type=track_type, track_index=track_index),
    }


@mcp.tool()
def studio_add_marker(
    frame: int, color: str = "Blue", name: str = "Marker",
    note: str = "", duration: int = 1,
) -> dict:
    """Add a marker to the current timeline. Studio only (Free → use Lua snippet)."""
    from resolve_pilot.studio_api import add_marker
    ok = add_marker(frame, color=color, name=name, note=note, duration=duration)
    return {"ok": ok, "frame": frame}


@mcp.tool()
def studio_set_clip_color(track: int = 1, color: str = "Orange") -> dict:
    """Color-code every clip on V<track>. Studio only."""
    from resolve_pilot.studio_api import set_clip_color
    n = set_clip_color(track=track, color=color)
    return {"clips_touched": n, "track": track, "color": color}


@mcp.tool()
def studio_apply_lut(lut_path: str, track: int = 1, node_index: int = 1) -> dict:
    """Apply a LUT to every clip on V<track> at color node N. Studio only."""
    from resolve_pilot.studio_api import apply_lut
    n = apply_lut(lut_path, track=track, node_index=node_index)
    return {"clips_touched": n, "lut": lut_path, "node": node_index}


@mcp.tool()
def studio_queue_render(
    preset: str = "H.264 Master",
    target_dir: str = "~/Desktop/Renders",
    custom_name: str | None = None,
) -> dict:
    """Queue one render job for the current timeline. Studio only."""
    from resolve_pilot.studio_api import queue_render_job
    jid = queue_render_job(preset=preset, target_dir=target_dir, custom_name=custom_name)
    return {"job_id": jid, "preset": preset, "target_dir": target_dir}


# ────────────────────────────────────────────────────────────────────────────
# Status / capabilities probe
# ────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def resolve_pilot_status() -> dict:
    """Report what resolve-pilot can do on this machine.

    Use this when starting a session — it tells you whether Resolve is
    installed, whether external scripting will work, what optional deps are
    available, and which OS-specific code paths apply.
    """
    info: dict[str, Any] = {
        "platform": platform.system(),
        "arch": platform.machine(),
        "python": sys.version.split()[0],
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
    }
    try:
        import mlx_whisper  # noqa: F401
        info["mlx_whisper"] = True
    except ImportError:
        info["mlx_whisper"] = False

    if platform.system() == "Darwin":
        resolve_app = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app")
        info["resolve_installed"] = resolve_app.exists()
        info["resolve_support_dir"] = (
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve"
        )
        # External scripting works only with Studio + setting enabled.
        from resolve_pilot.ui_automation import is_resolve_running
        try:
            info["resolve_running"] = is_resolve_running()
        except Exception:
            info["resolve_running"] = None

    # Probe Studio bridge cheaply
    try:
        from resolve_pilot.studio_api import studio_available
        info["studio_api_available"] = studio_available()
    except Exception as e:
        info["studio_api_available"] = False
        info["studio_api_error"] = str(e)

    info["capabilities"] = {
        "lua_snippets": True,
        "fcpxml_export": True,
        "srt_export": True,
        "ui_automation": platform.system() == "Darwin",
        "transcription": info.get("mlx_whisper", False),
        "scene_detection": info["ffmpeg"],
        "beat_detection": info["ffmpeg"],
        "ai_features_via_ui": platform.system() == "Darwin",
        "studio_python_api": info.get("studio_api_available", False),
    }
    info["lua_snippet_count"] = len(lua_lib.list_snippets())
    return info


def main() -> None:
    """Entry point for `python -m resolve_pilot.mcp_server` and the console script."""
    mcp.run()


if __name__ == "__main__":
    main()
