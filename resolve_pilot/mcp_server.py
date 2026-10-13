"""resolve-pilot MCP server.

Exposes the editing toolkit over Model Context Protocol so Claude Desktop /
Claude Code can drive a video editing session end-to-end without an API key.

Tool groups:
- session:  resolve_pilot_status, get_workflow_guide, setup_bridge
- analyze:  probe_media, detect_scenes, detect_beats, transcribe_audio, analyze_quality
- see:      grab_frames, scene_frames, read_video, screenshot_resolve (macOS)
- plan:     plan_from_transcript, plan_from_scenes, create_plan, get_plan, edit_plan
- export:   export_fcpxml, export_srt
- resolve:  get_resolve_state, import_timeline, add_markers, add_quality_markers,
            set_clip_color, apply_lut, queue_render, render_status, open_page, run_lua,
            list_lua_snippets, render_lua_snippet
- timeline: import_media, append_clips, remove_clips (live building, no FCPXML)
- macOS UI: send_keystroke, menu_pick, open_ai_feature

Run as:
    python -m resolve_pilot.mcp_server
or via Claude Desktop config (see README).
"""
# No `from __future__ import annotations`: the MCP SDK builds tool schemas
# from these signatures, so keep the annotations as real objects.
import base64
import functools
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

try:  # mcp >= 2.0 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from resolve_pilot import __version__, analyzers, bridge, control, luts
from resolve_pilot.analyzers import frames as frame_grab
from resolve_pilot._paths import state_dir
from resolve_pilot.editorial import ops as plan_ops
from resolve_pilot.editorial import roughcut, store
from resolve_pilot.exporters import fcpxml as fcpxml_export
from resolve_pilot.exporters import srt as srt_export
from resolve_pilot.lua_snippets import library as lua_lib
from resolve_pilot.transcribe import store as transcripts
from resolve_pilot.transcribe import Segment, available_engines
from resolve_pilot.workflows import get_workflow, list_workflows

INSTRUCTIONS = """\
resolve-pilot edits video with DaVinci Resolve, including the FREE edition (which has no \
external scripting API).

Start of session: call resolve_pilot_status once. Its `channel` says how Resolve is reached:
- studio: Resolve Studio's Python API, direct control.
- bridge: the resolve-pilot Lua bridge running inside Resolve (works in Free). The user starts \
it once per session; setup_bridge installs it into Workspace > Scripts.
- manual: nothing live. Resolve actions return `lua_code` + `instructions`; relay them to the \
user verbatim and ask them to paste it into Workspace > Console. Offer setup_bridge if they \
want hands-free control.
Resolve actions (get_resolve_state, import_timeline, import_media, append_clips, remove_clips, \
add_markers, add_quality_markers, set_clip_color, apply_lut, queue_render, open_page) choose the \
channel automatically and report it in `channel`.

Seeing: grab_frames (frames at given times), scene_frames (one frame per scene) and read_video \
(speech next to the matching frames) return images you can look at; layout="grid" packs them \
into one cheaper contact sheet. analyze_quality finds silence, black, frozen and loud/quiet \
stretches. timeline_frames shows the current timeline as Resolve renders it (grades, LUTs, \
titles, transitions) on any OS; use it to verify edits. On macOS screenshot_resolve shows \
Resolve's own window.

Editing loop:
1. Analyze: probe_media, then detect_scenes / detect_beats / transcribe_audio (cached; returns \
numbered segments). Look before choosing shots: scene_frames, read_video, grab_frames.
2. Draft: plan_from_transcript (pick segment_indices), plan_from_scenes, or create_plan (any \
sources, optional music bed). Plans are stored server-side; you get a plan_id.
3. Refine with edit_plan operations (keep/remove/move/trim/update/insert/set) instead of \
resending the plan. Every plan response lists `errors` and `warnings`; resolve them.
4. export_fcpxml(plan_id), then import_timeline(path) when live, or tell the user \
File > Import > Timeline.
5. Verify with get_resolve_state(include_clips=true) and timeline_frames (look at the result), \
and iterate. When live, change the open timeline directly instead of re-importing (each import \
makes a new timeline): append_clips adds source ranges (source seconds; a B-roll shot on track 2 \
at record_seconds; new_timeline creates one), import_media fills bins, remove_clips deletes by \
index from get_resolve_state (pass names; ask first). Read their `warnings`.
6. After queue_render(start=true), render_status(wait_seconds=...) says when it is done.

Conventions: times are seconds unless a parameter says frames. Marker positions can be given \
in seconds; Resolve converts them with the timeline's real frame rate. Paths must be absolute \
paths on the machine running this server (~ is expanded).
Safety: run_lua executes code inside the user's Resolve. Prefer the dedicated tools. Before \
anything destructive (deleting clips, markers or timelines, replacing grades, rendering over \
files) ask the user and suggest saving the project first.
Playbooks: get_workflow_guide() lists step-by-step guides (rough cut, subtitles, music video, \
color grade, markers, live control with a Resolve Lua API cheat sheet). Read the matching one \
before a multi-step job.
"""

mcp = _Server("resolve-pilot", instructions=INSTRUCTIONS)


# ────────────────────────────────────────────────────────────────────────────
# Tool registration
# ────────────────────────────────────────────────────────────────────────────

def _hints(read_only: bool, destructive: bool = False, idempotent: bool = False) -> ToolAnnotations:
    # Alias names are accepted by both mcp 1.x (camelCase fields) and 2.x (snake_case).
    return ToolAnnotations.model_validate({
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent or read_only,
        "openWorldHint": False,
    })


READ = _hints(read_only=True)
WRITE_FILES = _hints(read_only=False)  # creates plans / files; repeating a call repeats its effect
EDIT_RESOLVE = _hints(read_only=False)
DESTRUCTIVE = _hints(read_only=False, destructive=True)


def _explain(e: BaseException) -> str:
    """Turn an exception into a message the agent can act on."""
    if isinstance(e, FileNotFoundError):
        return (f"File not found: {e.filename or e}. Use an absolute path that exists on the "
                "machine running resolve-pilot (~ is expanded).")
    if isinstance(e, subprocess.CalledProcessError):
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        tail = " | ".join(err.strip().splitlines()[-3:])
        return f"{Path(str(e.cmd[0])).name} failed (exit {e.returncode}): {tail or 'no output'}"
    if isinstance(e, ValidationError):
        return f"Invalid input: {e}"
    return str(e) or type(e).__name__


def tool(annotations: ToolAnnotations, title: str | None = None):
    """Register a tool whose exceptions reach the agent as readable ToolErrors.

    mcp 2.x hides the message of unexpected exceptions from the client, which
    leaves the agent guessing; here every failure carries its explanation.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except Exception as e:
                raise ToolError(_explain(e)) from e

        mcp.tool(title=title, annotations=annotations)(wrapper)
        return fn
    return decorator


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(float(x), nd)


_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _content(header: dict[str, Any], *items: "str | Path") -> CallToolResult:
    """A tool result the model can look at: a JSON header, then text labels and images.

    Images are read into the result right away, so their temp files can be deleted
    when the tool returns. Built from plain content blocks, which mcp 1.x and 2.x
    both pass through unchanged (their Image helpers differ).
    """
    blocks: list[Any] = [TextContent(type="text", text=json.dumps(header, ensure_ascii=False, indent=2))]
    for item in items:
        if isinstance(item, Path):
            mime = _MIME.get(item.suffix.lower())
            if mime is None:
                raise ValueError(f"Unsupported image type: {item.name}")
            data = base64.b64encode(item.read_bytes()).decode("ascii")
            # Alias field names validate on both SDKs (mimeType in 1.x, mime_type in 2.x).
            blocks.append(ImageContent.model_validate({"type": "image", "data": data, "mimeType": mime}))
        else:
            blocks.append(TextContent(type="text", text=str(item)))
    return CallToolResult.model_validate({"content": blocks})


def _platform_supports_ui() -> bool:
    return platform.system() == "Darwin"


# ────────────────────────────────────────────────────────────────────────────
# Session
# ────────────────────────────────────────────────────────────────────────────

def _resolve_running() -> bool | None:
    try:
        sysname = platform.system()
        if sysname == "Darwin":
            from resolve_pilot.ui_automation import is_resolve_running
            return is_resolve_running()
        if sysname == "Windows":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"],
                                 capture_output=True, text=True, timeout=5).stdout
            return "Resolve.exe" in out
        return subprocess.run(["pgrep", "-x", "resolve"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return None


def _has_quartz() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("Quartz") is not None
    except (ImportError, ValueError):
        return False


_MAC_RESOLVE_APPS = (
    Path("/Applications/DaVinci Resolve/DaVinci Resolve.app"),
    Path("/Applications/DaVinci Resolve Studio/DaVinci Resolve Studio.app"),
)


def _resolve_app_version() -> str | None:
    """Installed Resolve version from the app bundle (macOS); None elsewhere or if unknown."""
    if platform.system() != "Darwin":
        return None
    import plistlib
    for app in _MAC_RESOLVE_APPS:
        try:
            with open(app / "Contents" / "Info.plist", "rb") as f:
                return str(plistlib.load(f).get("CFBundleShortVersionString") or "") or None
        except (OSError, plistlib.InvalidFileException):
            continue
    return None


def _version_at_least(text: str | None, major: int, minor: int) -> bool:
    parts = [int(p) for p in (text or "").split(".")[:2] if p.isdigit()]
    return len(parts) == 2 and (parts[0], parts[1]) >= (major, minor)


@tool(READ, title="Status and capabilities")
def resolve_pilot_status() -> dict[str, Any]:
    """Report what resolve-pilot can do on this machine and how it reaches Resolve.

    Call once at the start of a session. `channel` is studio / bridge / manual (see the
    server instructions); `next_steps` says what to set up for missing capabilities.
    """
    studio_ok, studio_detail = control.studio_status(refresh=True)
    br = bridge.status()
    channel = "studio" if studio_ok else ("bridge" if br.running else "manual")
    engines = available_engines()
    has_ffmpeg = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))
    try:
        sdk = version("mcp")
    except PackageNotFoundError:
        sdk = None

    resolve_version = _resolve_app_version()
    next_steps: list[str] = []
    if not has_ffmpeg:
        next_steps.append("Install ffmpeg (macOS: brew install ffmpeg, Windows: winget install ffmpeg, "
                          "Linux: apt install ffmpeg); media analysis and frames need it.")
    if channel == "manual":
        next_steps.append(
            "No live connection to Resolve. For hands-free control (works in Resolve Free): call "
            "setup_bridge(action='install'), then ask the user to click Workspace > Scripts > "
            "ResolvePilotBridge in Resolve. Until then Resolve actions return Lua for the user to paste."
        )
        if _version_at_least(resolve_version, 21, 1):
            next_steps.append(
                f"Resolve {resolve_version}: from 21.1 the Free edition limits scripts started from "
                "the Workspace > Scripts menu. If the menu entry does nothing, have the user paste "
                "setup_bridge's console_command into Workspace > Console (Lua tab)."
            )
    else:
        next_steps.append("Call get_resolve_state to see the open project and timeline.")
    if not engines:
        next_steps.append("Transcription is not installed: Apple Silicon pip install '.[transcribe]', "
                          "otherwise pip install '.[transcribe-cpu]'.")

    return {
        "version": __version__,
        "platform": platform.system(),
        "arch": platform.machine(),
        "python": sys.version.split()[0],
        "mcp_sdk": sdk,
        "ffmpeg": has_ffmpeg,
        "resolve_running": _resolve_running(),
        "resolve_version": resolve_version,
        "channel": channel,
        "studio_api": {"available": studio_ok, "detail": studio_detail},
        "bridge": br.to_dict(),
        "transcription_engines": engines,
        "capabilities": {
            "media_analysis": has_ffmpeg,
            "transcription": bool(engines),
            "fcpxml_srt_export": True,
            "live_resolve_control": channel != "manual",
            "lua_for_manual_paste": True,
            "see_footage": has_ffmpeg,
            "macos_ui_automation": _platform_supports_ui(),
            "resolve_screenshots": _platform_supports_ui() and _has_quartz(),
        },
        "next_steps": next_steps,
    }


@tool(READ, title="Workflow guides")
def get_workflow_guide(
    name: Annotated[str | None, Field(description="Guide name; omit to list all guides.")] = None,
) -> dict[str, Any]:
    """Step-by-step playbooks for multi-step jobs (rough cut, subtitles, music video, ...).

    Omit `name` to list them; read the matching guide before starting a bigger task.
    """
    if not name:
        return {"guides": list_workflows()}
    return {"name": name, "guide": get_workflow(name)}


@tool(WRITE_FILES, title="Set up the live bridge")
def setup_bridge(
    action: Annotated[
        Literal["install", "show_script", "status", "stop"],
        Field(description="install: put the bridge in Resolve's Workspace > Scripts menu. "
                          "show_script: return Lua to paste into the Console. "
                          "status: is a bridge running. stop: ask the running bridge to exit."),
    ] = "install",
    idle_minutes: Annotated[int, Field(ge=1, le=24 * 60,
                                       description="Bridge exits after this long without requests.")] = 60,
) -> dict[str, Any]:
    """Set up the live Lua bridge that lets you control Resolve directly — in the Free edition too.

    The bridge is a small Lua loop the USER starts inside Resolve once per session; it then
    runs your requests (get_resolve_state, import_timeline, run_lua, ...). Prefer
    action='install' and tell the user: "In Resolve choose Workspace > Scripts >
    ResolvePilotBridge (restart Resolve if it is not listed yet)". Then check with
    action='status'.
    """
    if action == "status":
        return bridge.status().to_dict()
    if action == "stop":
        return {"was_running": bridge.stop()}
    if action == "show_script":
        return {
            "lua_code": bridge.render_script(idle_minutes),
            "instructions": (
                "In DaVinci Resolve: Workspace > Console, choose the Lua tab, paste the whole "
                "script and press Enter. It prints '[bridge] ... ready' and keeps running in "
                "the background. Keep Resolve open."
            ),
        }
    path = bridge.install_script(idle_minutes)
    out: dict[str, Any] = {
        "installed": str(path),
        "console_command": f"dofile({lua_lib.lua_string(str(path))})",
        "instructions": (
            "In DaVinci Resolve choose Workspace > Scripts > ResolvePilotBridge (it may sit under "
            "a 'Utility' submenu; restart Resolve if it is not listed yet). If the menu entry does "
            "nothing (Resolve 21.1+ Free limits scripts run from that menu), open Workspace > "
            "Console, pick the Lua tab, paste console_command and press Enter. Then call "
            "setup_bridge(action='status') to confirm it is running."
        ),
    }
    others = sorted(p.name for p in path.parent.glob("*.lua")
                    if p != path and "bridge" in p.name.lower() and "pilot" in p.name.lower())
    if others:
        out["other_bridge_scripts"] = others
        out["note"] = (f"{', '.join(others)} in {path.parent} look like older resolve-pilot bridges. "
                       "Tell the user to start ResolvePilotBridge, not those; they can delete them.")
    return out


# ────────────────────────────────────────────────────────────────────────────
# Media analysis
# ────────────────────────────────────────────────────────────────────────────

@tool(READ, title="Probe media")
def probe_media(path: str) -> dict[str, Any]:
    """Container, codecs, fps, resolution, duration and audio layout of a media file.

    Use this first whenever you need to know what you're working with.
    """
    return analyzers.probe_media(path).to_dict()


@tool(READ, title="Detect scene cuts")
def detect_scenes(
    path: str,
    threshold: Annotated[float, Field(ge=0, le=100, description=(
        "Lower = more sensitive (8 catches lighting changes); 30+ returns only hard cuts."))] = 10.0,
    limit: Annotated[int, Field(ge=1, description="Return at most this many cuts (the strongest).")] = 500,
) -> dict[str, Any]:
    """Detect scene cuts in a video with ffmpeg's scdet filter.

    Returns cut times in seconds with a 0-100 score. Decoding is roughly real-time or
    faster, so hour-long files take a while.
    """
    cuts = analyzers.detect_scenes(path, threshold=threshold)
    kept = sorted(sorted(cuts, key=lambda c: -c.score)[:limit], key=lambda c: c.pts_seconds)
    return {
        "count": len(cuts),
        "returned": len(kept),
        "cuts": [{"pts_seconds": _r(c.pts_seconds), "score": _r(c.score, 2)} for c in kept],
    }


@tool(READ, title="Detect beats")
def detect_beats(
    path: str,
    min_seconds_between: Annotated[float, Field(gt=0, description="Smallest gap between beats (0.2 caps at 300 BPM).")] = 0.20,
    energy_threshold_db: Annotated[float, Field(description="Relative to the loudest onset; higher (less negative) keeps fewer, stronger beats.")] = -25.0,
    min_strength: Annotated[float, Field(ge=0, le=1, description="Drop beats weaker than this (0-1).")] = 0.0,
) -> dict[str, Any]:
    """Detect beats / onsets in a song (energy based) for cutting to music.

    `beats` is a list of [seconds, strength 0-1]. `tempo_bpm_estimate` is a rough guide only.
    """
    beats = analyzers.detect_beats(path, min_seconds_between=min_seconds_between,
                                   energy_threshold_db=energy_threshold_db)
    kept = [b for b in beats if b.strength >= min_strength]
    return {
        "count": len(kept),
        "tempo_bpm_estimate": analyzers.estimate_tempo(beats),
        "beat_format": "[seconds, strength]",
        "beats": [[_r(b.pts_seconds), _r(b.strength, 2)] for b in kept],
    }


# ────────────────────────────────────────────────────────────────────────────
# Seeing footage: frames returned as images the model can look at
# ────────────────────────────────────────────────────────────────────────────

FrameWidth = Annotated[int, Field(ge=64, le=1920, description=(
    "Pixel width of each frame (height keeps the aspect). Smaller is cheaper to look at."))]
Layout = Annotated[Literal["separate", "grid"], Field(description=(
    "separate: one image per frame with its own label. grid: one contact sheet "
    "(cheaper; cells read left to right, top to bottom)."))]


def _frames_result(header: dict[str, Any], frames: list, labels: list[str], layout: str,
                   folder: Path) -> CallToolResult:
    """Header + labelled frames, separately or as one contact sheet."""
    header["returned"] = len(frames)
    if not frames:
        header["ok"] = False
        header["hint"] = "No frame could be extracted; see `dropped` for each timestamp's reason."
        return _content(header)
    if layout == "grid":
        sheet = folder / "contact_sheet.jpg"
        cols, rows = frame_grab.contact_sheet(frames, sheet)
        header["grid"] = {"cols": cols, "rows": rows}
        order = "; ".join(f"cell {i + 1}: {label}" for i, label in enumerate(labels))
        return _content(header, f"Contact sheet {cols}x{rows}, reading order - {order}", sheet)
    items: list[Any] = []
    for frame, label in zip(frames, labels):
        items.extend([label, Path(frame.path)])
    return _content(header, *items)


@tool(READ, title="Grab frames")
def grab_frames(
    path: str,
    at_seconds: Annotated[list[float], Field(min_length=1, description="Timestamps in seconds from the start of the file.")],
    layout: Layout = "separate",
    width: FrameWidth = 480,
    max_frames: Annotated[int, Field(ge=1, le=48, description="Extra timestamps are reported in `dropped`.")] = 12,
) -> CallToolResult:
    """Look at a video: return the frames at the given times as images.

    Use it to check a shot before choosing it, verify a cut point, or read on-screen
    text. Timestamps that cannot be served (past the end, over max_frames) are listed in
    `dropped` with a reason instead of failing the call.
    """
    with tempfile.TemporaryDirectory(prefix="resolve-pilot-frames-") as tmp:
        frames, dropped, folder = frame_grab.extract_frames(path, at_seconds, width, max_frames, tmp)
        header = {"ok": True, "path": str(Path(path).expanduser().resolve()), "layout": layout,
                  "requested": len(at_seconds), "dropped": dropped}
        labels = [f"t={f.pts_seconds:.2f}s" for f in frames]
        return _frames_result(header, frames, labels, layout, folder)


@tool(READ, title="Scene frames")
def scene_frames(
    path: str,
    threshold: Annotated[float, Field(ge=0, le=100, description="As in detect_scenes: lower = more cuts.")] = 10.0,
    layout: Layout = "grid",
    width: FrameWidth = 320,
    max_frames: Annotated[int, Field(ge=1, le=48, description=(
        "At most this many scenes are shown, spread evenly over the video."))] = 16,
    offset_seconds: Annotated[float, Field(ge=0, description=(
        "Sample this long after each cut (capped at the scene's middle), past transitions."))] = 0.4,
) -> CallToolResult:
    """See what is in a video: detect its scenes and return one representative frame per scene.

    The fastest way to get a visual overview of unfamiliar footage. `scenes` lists every
    scene's start/end; the frames show a sample of them (all when they fit max_frames).
    """
    info = analyzers.probe_media(path)
    if not info.video_codec:
        raise ValueError(f"{Path(path).name} has no video stream.")
    cuts = analyzers.detect_scenes(path, threshold=threshold)
    starts = [0.0] + [c.pts_seconds for c in cuts if 0.0 < c.pts_seconds < info.duration_seconds]
    ends = starts[1:] + [info.duration_seconds]
    points = frame_grab.sample_points(starts, offset_seconds, info.duration_seconds)
    shown = frame_grab.pick_evenly(len(starts), max_frames)
    scenes = [{"scene": i, "start": _r(starts[i]), "end": _r(ends[i])} for i in range(len(starts))]
    with tempfile.TemporaryDirectory(prefix="resolve-pilot-scenes-") as tmp:
        frames, dropped, folder = frame_grab.extract_frames(
            path, [points[i] for i in shown], width, len(shown), tmp)
        for d in dropped:
            d["scene"] = shown[d.pop("index")]
        header = {
            "ok": True, "path": str(Path(path).expanduser().resolve()), "layout": layout,
            "scene_count": len(starts), "threshold": threshold, "scenes": scenes,
            "shown_scenes": [shown[f.index] for f in frames], "dropped": dropped,
        }
        labels = [f"scene {shown[f.index]}: {starts[shown[f.index]]:.2f}-{ends[shown[f.index]]:.2f}s "
                  f"(frame at {f.pts_seconds:.2f}s)" for f in frames]
        return _frames_result(header, frames, labels, layout, folder)


QualityKind = Literal["silence", "black", "freeze", "loud", "quiet"]


def _quality_spans(path: str, kinds: list[str] | None, min_seconds: float, window_seconds: float,
                   merge_gap_seconds: float) -> tuple[list, dict[str, Any]]:
    spans, meta = analyzers.analyze_quality(path, window_seconds=window_seconds,
                                            merge_gap_seconds=merge_gap_seconds)
    wanted = set(kinds or analyzers.quality.KINDS)
    return [s for s in spans if s.kind in wanted and s.end - s.start >= min_seconds], meta


@tool(READ, title="Analyze quality")
def analyze_quality(
    path: str,
    kinds: Annotated[list[QualityKind] | None, Field(description="Only these kinds; omit for all.")] = None,
    min_seconds: Annotated[float, Field(ge=0, description="Drop spans shorter than this.")] = 0.0,
    window_seconds: Annotated[float, Field(gt=0, le=600, description=(
        "loud/quiet are judged against the median level of this window around each moment."))] = 30.0,
    merge_gap_seconds: Annotated[float, Field(ge=0, le=10, description="Join same-kind spans closer than this.")] = 0.5,
    limit: Annotated[int, Field(ge=1, description="Return at most this many spans (the strongest and longest).")] = 200,
) -> dict[str, Any]:
    """Find questionable or notable stretches: silence, black frames, frozen picture,
    and audio that is unusually loud or quiet for its surroundings.

    Times are seconds in the media file. Use it to skip dead air and black/frozen
    footage when planning a cut, or turn the spans into timeline markers with
    add_quality_markers. Detectors missing from this ffmpeg build are listed in
    `detectors_skipped`.
    """
    spans, meta = _quality_spans(path, kinds, min_seconds, window_seconds, merge_gap_seconds)
    kept = sorted(spans, key=lambda s: -(s.score * (s.end - s.start)))[:limit]
    kept.sort(key=lambda s: (s.start, s.kind))
    counts: dict[str, int] = {}
    for s in spans:
        counts[s.kind] = counts.get(s.kind, 0) + 1
    return {
        "duration_seconds": _r(meta["duration"]),
        "detectors_ran": meta["detectors_ran"],
        "detectors_skipped": meta["detectors_skipped"],
        "counts": counts,
        "returned": len(kept),
        "spans": [{"start": _r(s.start), "end": _r(s.end), "kind": s.kind, "score": _r(s.score, 2)}
                  for s in kept],
    }


@tool(READ, title="Transcribe audio")
def transcribe_audio(
    path: str,
    language: Annotated[str | None, Field(description="ISO code like 'en' or 'tr'; omit to auto-detect.")] = None,
    model: Annotated[str | None, Field(description="large-v3-turbo (default), large-v3, medium, small, base, tiny.")] = None,
    engine: Literal["auto", "mlx", "faster-whisper"] = "auto",
    word_timestamps: bool = False,
    offset: Annotated[int, Field(ge=0, description="First segment to return (for paging).")] = 0,
    limit: Annotated[int, Field(ge=0, description="Segments to return; 0 = all.")] = 300,
    wait_seconds: Annotated[float, Field(ge=0, le=600, description="How long to wait before returning status='running'.")] = 45.0,
    refresh: bool = False,
) -> dict[str, Any]:
    """Transcribe speech locally with Whisper; results are cached per file/model/language.

    Returns `transcript_id` and numbered segments as "[i] start-end text". Long files keep
    transcribing in the background: if `status` is "running", call again with the same
    arguments a little later. Page through long transcripts with offset/limit (cached
    calls are instant). Pass `transcript_id` + `segment_indices` to plan_from_transcript.
    """
    res = transcripts.get_or_start(path, model=model, language=language,
                                   word_timestamps=word_timestamps, engine=engine,
                                   wait_seconds=wait_seconds, refresh=refresh)
    if res["status"] != "done":
        res["hint"] = ("Still transcribing in the background. Call transcribe_audio again with the "
                       "same path, model and language to collect the result (refresh will not "
                       "restart a job that is running).")
        return res
    segs = res["segments"]
    shown = transcripts.format_segments(segs, offset, limit)
    out = {
        "status": "done",
        "cached": res["cached"],
        "transcript_id": res["transcript_id"],
        "source_path": res["source_path"],
        "language": res["language"],
        "engine": res["engine"],
        "model": res["model"],
        "segment_count": len(segs),
        "segments": shown,
    }
    nxt = offset + len(shown)
    if nxt < len(segs):
        out["next_offset"] = nxt
    return out


def _segment_groups(n: int, k: int) -> list[tuple[int, int]]:
    """Split segments 0..n-1 into at most ``k`` contiguous (first, last) groups."""
    k = max(1, min(k, n))
    return [(i * n // k, (i + 1) * n // k - 1) for i in range(k)]


def _clip_text(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:max(1, limit - 1)].rstrip() + "…"


@tool(READ, title="Read video")
def read_video(
    path: Annotated[str | None, Field(description="Media file; transcribed (cached) if needed.")] = None,
    transcript_id: Annotated[str | None, Field(description="Reuse a transcript from transcribe_audio instead.")] = None,
    language: Annotated[str | None, Field(description="As in transcribe_audio.")] = None,
    model: Annotated[str | None, Field(description="As in transcribe_audio.")] = None,
    engine: Literal["auto", "mlx", "faster-whisper"] = "auto",
    max_frames: Annotated[int, Field(ge=1, le=48, description=(
        "One frame per segment; with more segments than this, neighbours are grouped."))] = 12,
    width: FrameWidth = 320,
    layout: Layout = "separate",
    max_chars_per_label: Annotated[int, Field(ge=40, le=4000)] = 400,
    wait_seconds: Annotated[float, Field(ge=0, le=600, description="As in transcribe_audio.")] = 45.0,
) -> CallToolResult:
    """Watch and listen at once: what is said, next to what is on screen while it is said.

    Each label carries the transcript segment indices ([first-last], usable as
    plan_from_transcript segment_indices), the time range and the speech; the image is a
    frame from the middle of that range. Transcripts come from the same cache as
    transcribe_audio. A range whose frame cannot be extracted is still listed, with the
    reason, in `missing_frames`.
    """
    if transcript_id:
        data = transcripts.require_transcript(transcript_id)
    elif path:
        data = transcripts.get_or_start(path, model=model, language=language, engine=engine,
                                        wait_seconds=wait_seconds)
        if data["status"] != "done":
            return _content({**data, "hint": (
                "Still transcribing in the background. Call read_video again with the same "
                "arguments in a little while.")})
    else:
        raise ValueError("Pass path (or transcript_id from transcribe_audio).")
    source = data["source_path"]
    segs = data["segments"]
    header: dict[str, Any] = {
        "ok": True, "transcript_id": data["transcript_id"], "source_path": source,
        "language": data.get("language"), "segment_count": len(segs), "layout": layout,
    }
    if not segs:
        header["hint"] = "No speech was found; use scene_frames for a visual overview."
        return _content(header)

    info = analyzers.probe_media_cached(source)
    if info is not None and not info.video_codec:
        header["hint"] = "The source has no video stream, so there are no frames; this is the speech only."
        header["segments"] = [f"[{i}] {float(s['start']):.2f}-{float(s['end']):.2f} {s['text']}"
                              for i, s in enumerate(segs)]
        return _content(header)

    groups = _segment_groups(len(segs), max_frames)
    spans = [(float(segs[a]["start"]), float(segs[b]["end"])) for a, b in groups]
    texts = [_clip_text(" ".join(str(segs[i]["text"]) for i in range(a, b + 1)), max_chars_per_label)
             for a, b in groups]
    labels = [f"[{a}-{b}] {t0:.1f}-{t1:.1f}s | speech: {txt}" if a != b
              else f"[{a}] {t0:.1f}-{t1:.1f}s | speech: {txt}"
              for (a, b), (t0, t1), txt in zip(groups, spans, texts)]
    with tempfile.TemporaryDirectory(prefix="resolve-pilot-read-") as tmp:
        frames, dropped, folder = frame_grab.extract_frames(
            source, [(t0 + t1) / 2 for t0, t1 in spans], width, len(groups), tmp)
        # Pair by request index: a dropped frame must never shift or hide a segment.
        header["missing_frames"] = [
            {"segments": list(groups[d["index"]]), "start": _r(spans[d["index"]][0]),
             "end": _r(spans[d["index"]][1]), "speech": texts[d["index"]], "reason": d["reason"]}
            for d in dropped
        ]
        header["grouped"] = len(groups) < len(segs)
        if layout == "grid" or not frames:
            return _frames_result(header, frames, [labels[f.index] for f in frames], layout, folder)
        by_index = {f.index: f for f in frames}
        items: list[Any] = []
        for i, label in enumerate(labels):
            if i in by_index:
                items.extend([label, Path(by_index[i].path)])
            else:
                items.append(f"{label} (no frame: {_dropped_reason(dropped, i)})")
        header["returned"] = len(frames)
        return _content(header, *items)


def _dropped_reason(dropped: list[dict[str, Any]], index: int) -> str:
    return next((d["reason"] for d in dropped if d["index"] == index), "unknown")


# ────────────────────────────────────────────────────────────────────────────
# Plans
# ────────────────────────────────────────────────────────────────────────────

class SegmentIn(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> "SegmentIn":
        if self.end <= self.start:
            raise ValueError(f"segment end ({self.end}) must be after start ({self.start})")
        return self


def _store_and_summarize(plan: roughcut.RoughCutPlan, include_clips: bool | None = None,
                         plan_id: str | None = None) -> dict[str, Any]:
    pid = store.save_plan(plan, plan_id)
    return store.plan_summary(pid, plan, include_clips=include_clips)


@tool(WRITE_FILES, title="Plan from transcript")
def plan_from_transcript(
    transcript_id: Annotated[str | None, Field(description="From transcribe_audio (preferred).")] = None,
    segment_indices: Annotated[list[int] | None, Field(description=(
        "Segments to keep, in cut order. Omit to use every segment in source order."))] = None,
    source_path: Annotated[str | None, Field(description="Defaults to the transcript's file.")] = None,
    segments: Annotated[list[SegmentIn] | None, Field(description="Inline {start, end, text} segments instead of transcript_id.")] = None,
    title: str = "Transcript Roughcut",
    pad_seconds: Annotated[float, Field(ge=0, le=2, description="Breathing room around each segment.")] = 0.2,
    fps: Annotated[float | None, Field(gt=0, description="Timeline fps; defaults to the source's.")] = None,
) -> dict[str, Any]:
    """Draft a rough cut from transcript segments: one clip per kept segment.

    Choose the segments editorially (best takes, complete thoughts, no false starts) and
    pass their indices in the order they should play. Neighbouring segments never
    overlap, so no words are repeated. Returns the stored plan (plan_id, clips, warnings).
    """
    if transcript_id:
        data = transcripts.require_transcript(transcript_id)
        segs = transcripts.transcript_segments(data)
        source_path = source_path or data["source_path"]
    elif segments:
        segs = [Segment(s.start, s.end, s.text) for s in segments]
    else:
        raise ValueError("Pass transcript_id (from transcribe_audio) or segments.")
    if not source_path:
        raise ValueError("source_path is required when passing inline segments.")
    plan = roughcut.plan_from_transcript(source_path, segs, title=title, pad_seconds=pad_seconds,
                                         fps=fps, segment_indices=segment_indices)
    return _store_and_summarize(plan)


@tool(WRITE_FILES, title="Plan from scenes")
def plan_from_scenes(
    source_path: str,
    cuts: Annotated[list[float] | None, Field(description=(
        "Cut times in seconds (pts_seconds from detect_scenes). Omit to detect them now."))] = None,
    threshold: Annotated[float, Field(ge=0, le=100, description="Used only when cuts are omitted.")] = 10.0,
    title: str = "Scene Roughcut",
    min_duration: Annotated[float, Field(ge=0, description="Skip scenes shorter than this.")] = 1.0,
    fps: Annotated[float | None, Field(gt=0)] = None,
) -> dict[str, Any]:
    """Draft a rough cut with one clip per scene; refine it with edit_plan."""
    if cuts is None:
        scene_cuts = analyzers.detect_scenes(source_path, threshold=threshold)
    else:
        scene_cuts = [analyzers.SceneCut(pts_seconds=float(t), score=0.0) for t in cuts]
    plan = roughcut.plan_from_scenes(source_path, scene_cuts, title=title,
                                     min_duration=min_duration, fps=fps)
    return _store_and_summarize(plan)


@tool(WRITE_FILES, title="Create plan")
def create_plan(
    title: str,
    clips: Annotated[list[plan_ops.ClipSpec], Field(min_length=1, description="Clips in playback order.")],
    fps: Annotated[float | None, Field(gt=0, description="Defaults to the first clip's source fps.")] = None,
    width: Annotated[int | None, Field(gt=0)] = None,
    height: Annotated[int | None, Field(gt=0)] = None,
    default_transition_seconds: Annotated[float, Field(ge=0, description="Cross dissolve at every cut; 0 = hard cuts.")] = 0.0,
    fade_in_seconds: Annotated[float, Field(ge=0)] = 0.0,
    fade_out_seconds: Annotated[float, Field(ge=0)] = 0.0,
    audio_bed: Annotated[plan_ops.AudioBedSpec | None, Field(description="Music under the whole cut (e.g. a music video's song).")] = None,
    notes: str = "",
) -> dict[str, Any]:
    """Build a plan from any clips — several sources, B-roll, music videos with a music bed.

    Each clip is a source range (in_seconds + duration_seconds or out_seconds). Returns the
    stored plan with a plan_id and validation warnings.
    """
    info = analyzers.probe_media_cached(clips[0].source_path)
    plan = roughcut.RoughCutPlan(
        title=title,
        fps=fps or (info.fps if info and info.fps else 24.0),
        width=width or (info.width if info and info.width else 1920),
        height=height or (info.height if info and info.height else 1080),
        clips=[c.to_clip() for c in clips],
        notes=notes,
        default_transition_seconds=default_transition_seconds,
        fade_in_seconds=fade_in_seconds,
        fade_out_seconds=fade_out_seconds,
        audio_bed=audio_bed.to_bed() if audio_bed else None,
    )
    return _store_and_summarize(plan)


@tool(READ, title="Get plan")
def get_plan(plan_id: str, include_clips: bool = True) -> dict[str, Any]:
    """Show a stored plan: clips (sources listed once in `sources`), totals, errors, warnings."""
    return store.plan_summary(plan_id, store.load_plan(plan_id), include_clips=include_clips)


@tool(WRITE_FILES, title="Edit plan")
def edit_plan(
    plan_id: str,
    operations: Annotated[list[plan_ops.PlanOp], Field(min_length=1, description=(
        "Applied in order; indices refer to the plan as it is at that step. All-or-nothing."))],
    include_clips: Annotated[bool | None, Field(description=(
        "List the clips in the reply. Default: only for plans of up to 60 clips."))] = None,
) -> dict[str, Any]:
    """Change a stored plan with small operations instead of re-sending it.

    Operations (field `op`):
    - keep {indices}: keep only these clips, in this order (select + reorder)
    - remove {indices} · move {index, to}
    - trim {index, head_seconds, tail_seconds}: positive shortens, negative extends
    - update {index, in_seconds?, duration_seconds? | out_seconds?, name?, rationale?,
      transition_out_seconds?, source_path?}
    - insert {clip, index?}: index omitted = append
    - set {title?, fps?, width?, height?, default_transition_seconds?, fade_in_seconds?,
      fade_out_seconds?, audio_bed?, notes?}: audio_bed=null removes the music bed
    """
    with store.plan_lock(plan_id):
        plan, log = plan_ops.apply_ops(store.load_plan(plan_id), operations)
        out = _store_and_summarize(plan, include_clips=include_clips, plan_id=plan_id)
    out["changes"] = log
    return out


# ────────────────────────────────────────────────────────────────────────────
# Export
# ────────────────────────────────────────────────────────────────────────────

def _free_path(p: Path) -> Path:
    """``p``, or ``name (2).ext``, ``name (3).ext``... if it already exists."""
    n = 2
    candidate = p
    while candidate.exists():
        candidate = p.with_name(f"{p.stem} ({n}){p.suffix}")
        n += 1
    return candidate


def _default_output(stem_source: str | None, title: str, suffix: str) -> Path:
    """Default export location; never an existing file (the user's own subtitles, say)."""
    slug = re.sub(r"[^\w\-]+", "_", title).strip("_") or "resolve_pilot"
    target = state_dir("exports") / f"{slug}{suffix}"
    if stem_source:
        d = Path(stem_source).expanduser().parent
        if d.is_dir() and os.access(d, os.W_OK):
            target = d / f"{slug}{suffix}"
    return _free_path(target)


@tool(WRITE_FILES, title="Export FCPXML")
def export_fcpxml(
    plan_id: str,
    out_path: Annotated[str | None, Field(description=(
        "Defaults to <title>.fcpxml next to the first source, numbered if that file exists. "
        "An explicit path is overwritten."))] = None,
) -> dict[str, Any]:
    """Write a stored plan as FCPXML 1.10 (Resolve Free & Studio: File > Import > Timeline).

    Refuses plans with errors. Afterwards call import_timeline(path) to load it into Resolve
    directly when the bridge or Studio API is available.
    """
    plan = store.load_plan(plan_id)
    report = roughcut.validate_plan(plan)
    if report["errors"]:
        raise ValueError("Fix these plan errors first (edit_plan): " + "; ".join(report["errors"]))
    target = Path(out_path).expanduser() if out_path else _default_output(
        plan.clips[0].source_path, plan.title, ".fcpxml")
    final = fcpxml_export.write_fcpxml(roughcut.plan_to_timeline_spec(plan), target)
    return {
        "path": str(final),
        "clip_count": len(plan.clips),
        "total_seconds": _r(plan.total_seconds),
        "warnings": report["warnings"],
        "next_step": f"import_timeline(path={str(final)!r}) or, without a live channel, "
                     "tell the user: File > Import > Timeline.",
    }


@tool(WRITE_FILES, title="Export SRT")
def export_srt(
    transcript_id: Annotated[str | None, Field(description="From transcribe_audio.")] = None,
    segments: Annotated[list[SegmentIn] | None, Field(description="Inline cues instead, e.g. a translation you wrote.")] = None,
    out_path: Annotated[str | None, Field(description=(
        "Defaults to <source>.<language>.srt, numbered if that file exists. "
        "An explicit path is overwritten."))] = None,
    max_chars_per_line: Annotated[int, Field(ge=0, description="Wrap lines; 0 disables. 42 broadcast, ~32 vertical video.")] = 42,
    max_lines: Annotated[int, Field(ge=1, le=3)] = 2,
) -> dict[str, Any]:
    """Write subtitles as SRT; long cues are wrapped and split into readable chunks.

    Import in Resolve by dragging the .srt onto the timeline or File > Import > Subtitle.
    """
    if transcript_id:
        data = transcripts.require_transcript(transcript_id)
        raw = [srt_export.SrtCue(float(s["start"]), float(s["end"]), str(s["text"]))
               for s in data["segments"]]
        if not out_path:
            src = Path(data["source_path"])
            lang = f".{data['language']}" if data.get("language") else ""
            out_path = str(_free_path(src.with_name(f"{src.stem}{lang}.srt")))
    elif segments:
        raw = [srt_export.SrtCue(s.start, s.end, s.text) for s in segments]
        if not out_path:
            raise ValueError("out_path is required with inline segments.")
    else:
        raise ValueError("Pass transcript_id or segments.")
    cues = srt_export.wrap_cues(raw, max_chars_per_line=max_chars_per_line, max_lines=max_lines)
    final = srt_export.write_srt(cues, out_path)
    durations = [c.end - c.start for c in cues] or [0.0]
    return {
        "path": str(final),
        "cue_count": len(cues),
        "average_cue_seconds": _r(sum(durations) / len(durations), 2),
        "longest_cue_seconds": _r(max(durations), 2),
    }


# ────────────────────────────────────────────────────────────────────────────
# Acting on Resolve (auto-routed: Studio API → live bridge → paste-able Lua)
# ────────────────────────────────────────────────────────────────────────────

Channel = Annotated[
    Literal["auto", "studio", "bridge", "manual"],
    Field(description="auto picks Studio API, then the live bridge, else returns Lua to paste."),
]


def _studio():
    from resolve_pilot.studio_api import actions
    return actions


@tool(READ, title="Get Resolve state")
def get_resolve_state(
    include_clips: bool = False,
    track_type: Literal["video", "audio", "subtitle"] = "video",
    track_index: Annotated[int, Field(ge=1)] = 1,
    include_markers: bool = False,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """What is open in Resolve: project, timelines, current timeline (fps, duration, tracks,
    markers) and optionally the clips on one track with their timeline positions.

    Use it to read the timeline fps before frame math and to verify every change you make.
    """
    args = dict(include_clips=include_clips, track_type=track_type, track_index=track_index,
                include_markers=include_markers)
    if control.choose_channel(channel) == "manual":
        return {
            "channel": "manual",
            "connected": False,
            "lua_code": lua_lib.snippet_dump_project_info(),
            "instructions": control.PASTE_INSTRUCTIONS,
            "note": "Not connected to Resolve. Ask the user to paste this and read you the Console "
                    "output, or set up the live bridge with setup_bridge.",
        }
    return control.perform(lua_lib.snippet_get_state(**args),
                           lambda: _studio().resolve_state(**args), channel)


@tool(EDIT_RESOLVE, title="Import timeline")
def import_timeline(
    path: Annotated[str, Field(description="FCPXML (e.g. from export_fcpxml), EDL, AAF, DRT or OTIO file.")],
    timeline_name: Annotated[str | None, Field(description="Must not clash with an existing timeline.")] = None,
    import_source_clips: Annotated[bool, Field(description="Also add the source media to the media pool.")] = True,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Import a timeline file into the open project and make it the current timeline."""
    full = str(Path(path).expanduser().resolve())
    if not Path(full).exists():
        raise FileNotFoundError(full)
    return control.perform(
        lua_lib.snippet_import_timeline(full, timeline_name, import_source_clips),
        lambda: _studio().import_timeline(full, timeline_name, import_source_clips),
        channel,
        timeout=180.0,
    )


def _existing_file(path: str) -> str:
    full = Path(path).expanduser().resolve()
    if not full.is_file():
        raise FileNotFoundError(str(full))
    return str(full)


@tool(_hints(read_only=False, idempotent=True), title="Import media")
def import_media(
    paths: Annotated[list[str], Field(min_length=1, max_length=200, description="Absolute media file paths.")],
    bin: Annotated[str | None, Field(description=(
        "Bin to import into, e.g. 'B-roll' or 'B-roll/Day 1' below the root bin; created when "
        "missing. Omit for the current bin."))] = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Add media files to the open project's media pool, without a timeline.

    Files already in that bin (same File Path) are reused, not imported twice. Each item
    comes back with its media pool name and `media_pool_id` (use it in append_clips) and
    `already_in_pool`. append_clips imports by itself, so call this only to organize
    bins or check what Resolve accepts.
    """
    full = [_existing_file(p) for p in paths]
    rows_bin = lua_lib.normalize_bin(bin)
    return control.perform(lua_lib.snippet_import_media(full, rows_bin),
                           lambda: _studio().import_media(full, rows_bin),
                           channel, timeout=60.0 + 5.0 * len(full))


class ClipIn(BaseModel):
    source: str | None = Field(None, description=(
        "Absolute media file path. Imported into the current bin if it is not in the media pool yet."))
    media_pool_id: str | None = Field(None, description="media_pool_id from import_media, instead of source.")
    in_seconds: float = Field(0.0, ge=0, description="Source in point, seconds into the file.")
    out_seconds: float | None = Field(None, gt=0, description="Source out point (exclusive), seconds into the file.")
    duration_seconds: float | None = Field(None, gt=0, description="Instead of out_seconds. Omit both for 'to the end'.")
    track: int | None = Field(None, ge=1, description=(
        "Track number (V2/A2 = 2); missing tracks are added. Default: track 1."))
    media: Literal["both", "video", "audio"] = Field("both", description="Which part of the clip to use.")
    record_seconds: float | None = Field(None, ge=0, description=(
        "Timeline position (from the timeline start). Omit to append after the track's last clip."))

    @model_validator(mode="after")
    def _check(self) -> "ClipIn":
        if (self.source is None) == (self.media_pool_id is None):
            raise ValueError("give exactly one of source or media_pool_id")
        if self.out_seconds is not None and self.duration_seconds is not None:
            raise ValueError("give out_seconds or duration_seconds, not both")
        if self.out_seconds is not None and self.out_seconds <= self.in_seconds:
            raise ValueError("out_seconds must be after in_seconds")
        return self


@tool(EDIT_RESOLVE, title="Append clips / new timeline")
def append_clips(
    clips: Annotated[list[ClipIn] | None, Field(max_length=200, description=(
        "Source ranges in playback order, in SOURCE seconds."))] = None,
    timeline: Annotated[str | None, Field(description=(
        "Existing timeline to append to (made current). Default: the current timeline."))] = None,
    new_timeline: Annotated[str | None, Field(description=(
        "Create an empty timeline with this name first (refused if the name exists), make it "
        "current and append there. clips may be empty."))] = None,
    new_timeline_fps: Annotated[float | None, Field(gt=0, le=120, description=(
        "Frame rate for the new timeline; default: the project's. Read back in timeline.fps."))] = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Build or extend a timeline live: append source ranges to the current, a named or a new
    timeline, without an FCPXML round trip.

    Seconds are converted with the source clip's own fps (source range) and the timeline's
    fps (record position) inside Resolve. Returns the timeline items that actually landed
    (track, start/end seconds) plus `warnings` wherever Resolve did something other than
    asked; check them. Verify with get_resolve_state(include_clips=true) or timeline_frames.
    """
    rows = lua_lib.normalize_clips([
        {"path": _existing_file(c.source) if c.source else None, "media_pool_id": c.media_pool_id,
         "in_seconds": c.in_seconds, "out_seconds": c.out_seconds, "duration_seconds": c.duration_seconds,
         "track": c.track, "media": c.media, "record_seconds": c.record_seconds}
        for c in clips or []])
    code = lua_lib.snippet_append_clips(rows, timeline, new_timeline, new_timeline_fps)
    return control.perform(
        code, lambda: _studio().append_clips(rows, timeline, new_timeline, new_timeline_fps),
        channel, timeout=60.0 + 10.0 * len(rows))


@tool(DESTRUCTIVE, title="Remove clips")
def remove_clips(
    indices: Annotated[list[int], Field(min_length=1, description=(
        "0-based clip indices on the track, exactly as get_resolve_state(include_clips=true) lists "
        "them for the same track (transitions are not counted)."))],
    track_type: Literal["video", "audio"] = "video",
    track_index: Annotated[int, Field(ge=1)] = 1,
    names: Annotated[list[str] | None, Field(description=(
        "The clip names get_resolve_state showed at those indices. If any differs (the timeline "
        "changed), nothing is deleted. Strongly recommended."))] = None,
    ripple: Annotated[bool, Field(description="Close the gaps by moving later clips left.")] = False,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Delete clips from one track of the current timeline. Destructive: confirm with the
    user first. Read the track with get_resolve_state(include_clips=true) right before and
    pass `names` so a changed timeline cannot delete the wrong clips. Only this track is
    read back: whether Resolve also removes linked audio is not verified, so check the
    audio track afterwards.
    """
    code = lua_lib.snippet_remove_clips(indices, track_type, track_index, names, ripple)
    return control.perform(
        code, lambda: _studio().remove_clips(indices, track_type, track_index, names, ripple), channel)


class MarkerSpec(BaseModel):
    frame: int | None = Field(None, ge=0, description="Frame offset from the timeline start.")
    seconds: float | None = Field(None, ge=0, description=(
        "Time from the timeline start; converted with the timeline's real fps inside Resolve."))
    color: Literal[lua_lib.MARKER_COLORS] = "Blue"  # type: ignore[valid-type]
    name: str = ""
    note: str = ""
    duration: int = Field(1, ge=1, description="Length in frames.")

    @field_validator("color", mode="before")
    @classmethod
    def _color(cls, v: Any) -> Any:
        return lua_lib._validate_color(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _position(self) -> "MarkerSpec":
        if (self.frame is None) == (self.seconds is None):
            raise ValueError("give exactly one of frame or seconds")
        return self


@tool(EDIT_RESOLVE, title="Add markers")
def add_markers(
    markers: Annotated[list[MarkerSpec], Field(min_length=1)],
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Add markers to the current timeline (beats, scene changes, review notes, chapters).

    Positions are offsets from the timeline start, in seconds or frames. A position that
    already has a marker is reported in `skipped_frames`.
    """
    rows = lua_lib.normalize_markers([m.model_dump() for m in markers])
    return control.perform(lua_lib.snippet_add_markers(rows),
                           lambda: _studio().add_markers_batch(rows), channel)


QUALITY_MARKER_COLORS = {"silence": "Blue", "black": "Cocoa", "freeze": "Lavender",
                         "loud": "Red", "quiet": "Sky"}


class QualitySpanIn(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    kind: QualityKind
    score: float = Field(1.0, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> "QualitySpanIn":
        if self.end < self.start:
            raise ValueError(f"span end ({self.end}) is before start ({self.start})")
        return self


def quality_markers(
    spans: list[Any], source_path: str, timeline_start_seconds: float = 0.0,
    source_in_seconds: float = 0.0, source_out_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], int, str]:
    """Map media-relative spans onto the timeline as marker dicts.

    Returns (markers, spans outside the clip, owned customData prefix for this source).
    Each marker's customData is stable for the same source and span, and every marker
    for one source shares a prefix, so a re-run replaces exactly its own markers.
    """
    source = str(Path(source_path).expanduser().resolve())
    prefix = f"resolve-pilot:quality:{hashlib.sha1(source.encode()).hexdigest()[:8]}:"
    out_point = math.inf if source_out_seconds is None else source_out_seconds
    markers, outside = [], 0
    for s in spans:
        start, end = max(s.start, source_in_seconds), min(s.end, out_point)
        if end < start or (end == start and s.end > s.start):
            outside += 1
            continue
        digest = hashlib.sha1(json.dumps([s.kind, round(s.start, 3), round(s.end, 3)]).encode()).hexdigest()[:12]
        markers.append({
            "seconds": round(timeline_start_seconds + start - source_in_seconds, 3),
            "duration_seconds": max(end - start, 0.001),
            "color": QUALITY_MARKER_COLORS[s.kind],
            "name": s.kind.capitalize(),
            "note": f"{s.kind} {s.start:.2f}-{s.end:.2f}s in {Path(source).name} (score {s.score:.2f})",
            "custom_data": f"{prefix}{s.kind}:{digest}",
        })
    return _combine_same_frame(markers), outside, prefix


# Resolve keeps one marker per frame; spans that start within a frame of each other
# (at 24 fps or faster) become one marker instead of the second being skipped.
_SAME_FRAME_SECONDS = 0.04


def _combine_same_frame(markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in sorted(markers, key=lambda m: m["seconds"]):
        if out and m["seconds"] - out[-1]["seconds"] < _SAME_FRAME_SECONDS:
            first = out[-1]
            first["name"] = f"{first['name']} + {m['name']}"
            first["note"] = f"{first['note']}; {m['note']}"
            first["duration_seconds"] = max(first["duration_seconds"], m["duration_seconds"])
        else:
            out.append(dict(m))
    return out


@tool(EDIT_RESOLVE, title="Add quality markers")
def add_quality_markers(
    path: Annotated[str, Field(description="The media file the spans come from.")],
    spans: Annotated[list[QualitySpanIn] | None, Field(description=(
        "Spans from analyze_quality (start, end, kind, score). Omit to analyze path now."))] = None,
    kinds: Annotated[list[QualityKind] | None, Field(description="Only these kinds; omit for all.")] = None,
    min_seconds: Annotated[float, Field(ge=0, description="Skip spans shorter than this.")] = 0.5,
    timeline_start_seconds: Annotated[float, Field(ge=0, description=(
        "Where the clip starts on the timeline (start_seconds from get_resolve_state); "
        "0 when the timeline is just this file."))] = 0.0,
    source_in_seconds: Annotated[float, Field(ge=0, description=(
        "The clip's in-point inside the file (source_in_frame / fps)."))] = 0.0,
    source_out_seconds: Annotated[float | None, Field(gt=0, description=(
        "The clip's out-point inside the file; spans past it are skipped."))] = None,
    replace_previous: Annotated[bool, Field(description=(
        "Delete the quality markers this tool added earlier for the same file first, "
        "so re-running does not pile up duplicates. Never touches other markers."))] = True,
    max_markers: Annotated[int, Field(ge=1, le=500)] = 100,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Mark silence, black, frozen and loud/quiet stretches on the current timeline.

    Spans are media times; they are placed relative to where the clip sits on the
    timeline (timeline_start_seconds / source_in_seconds / source_out_seconds; read them
    with get_resolve_state(include_clips=true)). Colors: silence Blue, black Cocoa,
    freeze Lavender, loud Red, quiet Sky.
    """
    if spans is None:
        found, _meta = _quality_spans(path, kinds, min_seconds, 30.0, 0.5)
    else:
        wanted = set(kinds or QUALITY_MARKER_COLORS)
        found = [s for s in spans if s.kind in wanted and s.end - s.start >= min_seconds]
    if source_out_seconds is not None and source_out_seconds <= source_in_seconds:
        raise ValueError("source_out_seconds must be after source_in_seconds.")
    found = sorted(found, key=lambda s: -(s.score * (s.end - s.start)))[:max_markers]
    found.sort(key=lambda s: s.start)
    markers, outside, prefix = quality_markers(found, path, timeline_start_seconds,
                                               source_in_seconds, source_out_seconds)
    if not markers:
        return {"ok": True, "added": 0, "spans_outside_clip": outside,
                "note": "No quality spans to mark (after kinds / min_seconds / clip range)."}
    rows = lua_lib.normalize_markers(markers)
    owned = prefix if replace_previous else None
    out = control.perform(lua_lib.snippet_add_markers(rows, owned),
                          lambda: _studio().add_markers_batch(rows, owned), channel)
    out["spans_outside_clip"] = outside
    return out


@tool(_hints(read_only=False, idempotent=True), title="Set clip color")
def set_clip_color(
    color: Literal[lua_lib.CLIP_COLORS],  # type: ignore[valid-type]
    track: Annotated[int, Field(ge=1, description="Video track number (V1 = 1).")] = 1,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Color-code every clip on a video track of the current timeline."""
    def studio():
        return {"ok": True, "colored": _studio().set_clip_color(track=track, color=color),
                "track": track, "color": color}
    return control.perform(lua_lib.snippet_set_clip_color(color, track), studio, channel)


@tool(DESTRUCTIVE, title="Apply LUT")
def apply_lut(
    lut_path: Annotated[str, Field(description="Absolute path of a .cube / .dat LUT.")],
    track: Annotated[int, Field(ge=1)] = 1,
    node_index: Annotated[int, Field(ge=1, description="Color node that receives the LUT (replaces its LUT).")] = 1,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Apply a LUT to every clip on a video track, at one color node.

    LUTs outside Resolve's LUT folder are copied into <LUT folder>/resolve-pilot first;
    Resolve silently refuses LUTs anywhere else.
    """
    full, copied = luts.stage_lut(lut_path)
    out = control.perform(lua_lib.snippet_apply_lut_to_clips(full, track, node_index),
                          lambda: _studio().apply_lut_report(full, track, node_index), channel)
    if copied and isinstance(out, dict):
        out["staged_lut"] = full
    return out


@tool(EDIT_RESOLVE, title="Queue render")
def queue_render(
    preset: Annotated[str, Field(description="Render preset name; a wrong name returns available_presets.")] = "H.264 Master",
    target_dir: str = "~/Desktop/Renders",
    custom_name: Annotated[str | None, Field(description="Output file name without extension.")] = None,
    start: Annotated[bool, Field(description="Start rendering right away.")] = False,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Add the current timeline to the render queue with a preset, optionally starting it."""
    return control.perform(
        lua_lib.snippet_render_current_timeline(preset, target_dir, custom_name, start),
        lambda: _studio().render_current_timeline(preset, target_dir, custom_name, start),
        channel,
    )


@tool(_hints(read_only=False, idempotent=True), title="Open page")
def open_page(
    page: Literal[lua_lib.PAGES],  # type: ignore[valid-type]
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Switch Resolve to a page: media, cut, edit, fusion, color, fairlight, deliver.

    Uses the scripting API / bridge when live, otherwise a keyboard shortcut on macOS.
    """
    chosen = control.choose_channel(channel)
    if chosen == "manual" and _platform_supports_ui() and channel == "auto":
        from resolve_pilot.ui_automation import open_page as ui_open_page
        from resolve_pilot.ui_automation.applescript import UIAutomationError
        try:
            ui_open_page(page)
        except UIAutomationError as e:
            return {"channel": "macos_ui", "ok": False, "error": str(e)}
        return {"channel": "macos_ui", "ok": True, "page": page}
    return control.perform(lua_lib.snippet_open_page(page), lambda: _studio().open_page(page), chosen)


@tool(DESTRUCTIVE, title="Run Lua in Resolve")
def run_lua(
    code: Annotated[str | None, Field(description=(
        "Lua to run inside Resolve. `resolve` is available, plus helpers rp.project(), "
        "rp.timeline(), rp.media_pool(), rp.fps(). `return` a value (tables become JSON); "
        "print() output is captured."))] = None,
    snippet: Annotated[str | None, Field(description="Run a library snippet instead of code (see list_lua_snippets).")] = None,
    args: Annotated[dict[str, Any] | None, Field(description="Arguments for the snippet.")] = None,
    timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 30.0,
) -> dict[str, Any]:
    """Execute Lua inside the running Resolve through the live bridge (Free or Studio).

    The escape hatch for anything the dedicated tools don't cover. It can change or delete
    anything in the project, so read state first, keep scripts small, and confirm
    destructive steps with the user. See get_workflow_guide('live-control') for the API.
    """
    if (code is None) == (snippet is None):
        raise ValueError("Pass exactly one of code or snippet.")
    lua = code if code is not None else lua_lib.render_snippet(snippet, args or {})
    return bridge.exec_lua(lua, timeout=timeout_seconds).to_dict()


@tool(READ, title="List Lua snippets")
def list_lua_snippets() -> list[dict]:
    """Ready-made Lua snippets (name, description, params) for Resolve's Console.

    Run one live with run_lua(snippet=..., args=...), or render it for the user to paste.
    """
    return lua_lib.list_snippets()


@tool(READ, title="Render Lua snippet")
def render_lua_snippet(
    snippet: str,
    args: Annotated[dict[str, Any] | None, Field(description="Snippet parameters (see list_lua_snippets).")] = None,
) -> dict[str, Any]:
    """Return a snippet's Lua for the user to paste into Workspace > Console (Free & Studio)."""
    return {"snippet": snippet, "code": lua_lib.render_snippet(snippet, args or {}),
            "instructions": control.PASTE_INSTRUCTIONS}


# ────────────────────────────────────────────────────────────────────────────
# macOS UI automation — for what no scripting API reaches
# ────────────────────────────────────────────────────────────────────────────

def _require_macos(what: str) -> None:
    if not _platform_supports_ui():
        raise RuntimeError(f"{what} uses macOS UI automation and is not available on "
                           f"{platform.system()}. With the live bridge, use open_page or run_lua.")


def _ui_call(fn, *args) -> dict[str, Any]:
    from resolve_pilot.ui_automation.applescript import StudioOnlyFeature, UIAutomationError
    try:
        out = fn(*args)
    except StudioOnlyFeature as e:
        return {"ok": False, "error": str(e),
                "hint": "This needs DaVinci Resolve Studio. Tell the user, or do it another way."}
    except UIAutomationError as e:
        from resolve_pilot.ui_automation.permissions import permission_hint
        return {"ok": False, "error": str(e),
                "hint": "If this is a permission error: " + permission_hint("Accessibility")}
    return out if isinstance(out, dict) else {"ok": True}


@tool(DESTRUCTIVE, title="Send keystroke (macOS)")
def send_keystroke(
    key: Annotated[str, Field(description="One character, or return/tab/space/delete/escape/left/right/up/down/f1-f12.")],
    modifiers: Annotated[str, Field(description=(
        "'+'-joined: command/cmd, shift, option/opt/alt, control/ctrl/ctl; e.g. 'shift+command'."))] = "",
) -> dict[str, Any]:
    """Send a keyboard shortcut to Resolve (brought to the front first). macOS only.

    Nothing is typed if Resolve cannot be brought to the front (ok=false), so the
    keystroke never lands in another app.

    Examples: key='s' + 'command' saves; 'space' plays/pauses; 't' + 'command' applies the
    default transition to selected edits on the Edit page.
    """
    _require_macos("send_keystroke")
    from resolve_pilot.ui_automation import send_keystroke as ui_send
    res = _ui_call(ui_send, key, modifiers)
    return {**res, "key": key, "modifiers": modifiers}


@tool(DESTRUCTIVE, title="Click menu item (macOS)")
def menu_pick(
    menu_path: Annotated[list[str], Field(min_length=2, description='e.g. ["File", "Import", "Timeline…"].')],
) -> dict[str, Any]:
    """Click a Resolve menu item by its path. macOS only; needs Accessibility permission."""
    _require_macos("menu_pick")
    from resolve_pilot.ui_automation import menu_pick as ui_menu_pick
    res = _ui_call(ui_menu_pick, menu_path)
    return {**res, "path": menu_path}


def _screen_error(error: str, hint: str, **extra: Any) -> CallToolResult:
    return _content({"ok": False, "error": error, "hint": hint, **extra})


class TimelinePosition(BaseModel):
    seconds: float | None = Field(None, ge=0, description="Offset from the timeline start.")
    frame: int | None = Field(None, ge=0, description="Frame offset from the timeline start.")

    @model_validator(mode="after")
    def _one(self) -> "TimelinePosition":
        if (self.seconds is None) == (self.frame is None):
            raise ValueError("give exactly one of seconds or frame")
        return self


@tool(_hints(read_only=False, idempotent=True), title="Timeline frames")
def timeline_frames(
    positions: Annotated[list[TimelinePosition], Field(min_length=1, max_length=24, description=(
        "Where to look, as offsets from the timeline start (seconds or frame)."))],
    layout: Layout = "separate",
    width: FrameWidth = 640,
    channel: Channel = "auto",
) -> CallToolResult:
    """See the edit the way Resolve renders it: grades, LUTs, titles, effects, transitions.

    Use it to verify what you changed (a LUT, a title, a cut point). Unlike grab_frames,
    which reads source files, this exports frames of the current timeline from inside
    Resolve, on any OS. The playhead and page are put back afterwards. Needs a live
    channel (bridge or Studio).
    """
    rows = [p.model_dump(exclude_none=True) for p in positions]
    with tempfile.TemporaryDirectory(prefix="stills-", dir=state_dir("stills")) as tmp:
        folder = Path(tmp)
        out = control.perform(lua_lib.snippet_export_timeline_stills(rows, tmp),
                              lambda: _studio().export_timeline_stills(rows, tmp),
                              channel, timeout=30.0 + 5.0 * len(rows))
        if out.get("channel") == "manual":
            out["note"] = ("timeline_frames needs a live connection to Resolve; set up the bridge "
                           "with setup_bridge, or use grab_frames on the source files instead.")
            out.pop("lua_code", None)
            out["ok"] = False
            return _content(out)
        frames, labels, dropped = [], [], []
        for row in out.get("stills", []):
            png = Path(row["path"]) if row.get("path") else None
            if not row.get("ok") or png is None or not png.is_file():
                dropped.append({"index": row["index"], "seconds": _r(row["seconds"]),
                                "reason": row.get("error") or "no still was written"})
                continue
            jpg = folder / f"frame_{row['index']:03d}.jpg"
            proc = subprocess.run(frame_grab._frame_cmd(png, 0.0, jpg, width),
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode != 0 or not jpg.is_file():
                dropped.append({"index": row["index"], "seconds": _r(row["seconds"]),
                                "reason": "could not convert the still: " + proc.stderr.strip()[:200]})
                continue
            frames.append(frame_grab.Frame(index=row["index"], pts_seconds=row["seconds"], path=str(jpg)))
            labels.append(f"{row['timecode']} (t={row['seconds']:.2f}s, frame {row['frame']})")
        header = {"ok": True, "channel": out["channel"], "timeline": out.get("timeline"),
                  "fps": out.get("fps"), "drop_frame": out.get("drop_frame"), "layout": layout,
                  "requested": len(rows), "dropped": dropped}
        return _frames_result(header, frames, labels, layout, folder)


@tool(READ, title="Render status")
def render_status(
    wait_seconds: Annotated[float, Field(ge=0, le=3600, description=(
        "Keep polling until Resolve stops rendering or this many seconds pass; 0 = just look."))] = 0.0,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Render queue: every job's status, percent done and time left, and whether Resolve is
    rendering. Call after queue_render(start=true) to know when the file is ready."""
    import time
    deadline = time.monotonic() + wait_seconds
    while True:
        out = control.perform(lua_lib.snippet_render_status(),
                              lambda: _studio().render_jobs_status(), channel)
        if out.get("channel") == "manual" or not out.get("rendering") or time.monotonic() >= deadline:
            break
        time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
    if out.get("channel") != "manual":
        out["done"] = not out.get("rendering")
    return out


@tool(READ, title="Screenshot Resolve (macOS)")
def screenshot_resolve(
    width: Annotated[int, Field(ge=320, le=3840, description="Downscale to this pixel width (never upscaled).")] = 1280,
    focus_first: Annotated[bool, Field(description=(
        "Bring Resolve to the front if none of its windows is on screen."))] = True,
) -> CallToolResult:
    """See Resolve's UI: capture its main window as an image. macOS only.

    Use it to check what the user sees (page, dialogs, timeline, viewer) after acting
    through the UI tools. Needs Screen Recording permission for the app running this
    server; without it (or with Resolve minimized) you get ok=false and what to do,
    never a blank or old image.
    """
    if not _platform_supports_ui():
        return _screen_error(f"screenshot_resolve is macOS only (this is {platform.system()}).",
                             "Use get_resolve_state to inspect Resolve instead.")
    from resolve_pilot.ui_automation import screen
    if screen._quartz() is None:
        return _screen_error("pyobjc's Quartz module is not installed.",
                             "Install the UI extra: pip install 'davinci-resolve-mcp-free[ui]' "
                             "(or pip install -e '.[ui]' in a checkout), then restart the server.")
    if not screen.screen_recording_available():
        from resolve_pilot.ui_automation.permissions import permission_hint
        return _screen_error(
            "Screen Recording permission is not granted to the app running resolve-pilot.",
            permission_hint("Screen Recording"))
    onscreen, every = screen.find_resolve_windows()
    if not every:
        return _screen_error("No DaVinci Resolve window found.", "Is Resolve running? Ask the user to open it.")
    if not onscreen and focus_first:
        from resolve_pilot.ui_automation import applescript
        applescript.focus_resolve()
        onscreen, every = screen.find_resolve_windows()
    if not onscreen:
        return _screen_error("DaVinci Resolve's windows are minimized or on another Space.",
                             "Ask the user to bring Resolve onto the current screen, then retry.",
                             windows=len(every))
    window = max(onscreen, key=lambda w: float(w["bounds"].get("Width", 0)) * float(w["bounds"].get("Height", 0)))
    with tempfile.TemporaryDirectory(prefix="resolve-pilot-screen-") as tmp:
        captured = Path(tmp) / "capture.png"
        if not screen.capture_window(window["id"], captured):
            return _screen_error("Capturing the Resolve window failed.",
                                 "Check Screen Recording permission and that Resolve is visible, then retry.")
        native = screen.png_size(captured)
        if native is None:
            return _screen_error("The capture is not a readable PNG.", "Retry; if it persists, restart Resolve.")
        final = captured
        if native[0] > width:
            final = Path(tmp) / "resolve.png"
            proc = subprocess.run(["sips", "--resampleWidth", str(width), str(captured), "--out", str(final)],
                                  capture_output=True, text=True, timeout=30.0, check=False)
            if proc.returncode != 0 or screen.png_size(final) is None:
                return _screen_error("Downscaling the screenshot failed.", "Retry with a larger width.")
        returned = screen.png_size(final)
        bounds = window["bounds"]
        header = {
            "ok": True, "window_id": window["id"], "window_name": window["name"], "bounds": bounds,
            "native_px": {"width": native[0], "height": native[1]},
            "returned_px": {"width": returned[0], "height": returned[1]},
            # Screen points per returned pixel: x_screen = bounds.X + x_image * this.
            "points_per_pixel": round(float(bounds.get("Width", 0)) / returned[0], 4),
        }
        return _content(header, f"Resolve window '{window['name']}' ({returned[0]}x{returned[1]})", final)


@tool(EDIT_RESOLVE, title="Open an AI feature (macOS)")
def open_ai_feature(
    feature: Literal["magic_mask", "smart_reframe", "voice_isolation", "scene_cut_detection", "auto_subtitle"],
    target_aspect: Annotated[Literal["1:1", "9:16", "4:5", "16:9"], Field(description="smart_reframe only.")] = "9:16",
    intensity: Annotated[int, Field(ge=0, le=100, description="voice_isolation only.")] = 60,
    language: Annotated[str, Field(description="auto_subtitle only.")] = "en",
) -> dict[str, Any]:
    """Open one of Resolve's AI tools for the user to finish (they need a GUI click).

    Several are Studio-only or Inspector-only depending on the Resolve version; then the
    result is ok=false with manual steps to relay. macOS only.
    """
    _require_macos("open_ai_feature")
    from resolve_pilot.ui_automation import ai_features as ai
    calls = {
        "magic_mask": (ai.trigger_magic_mask_person,),
        "smart_reframe": (ai.trigger_smart_reframe, target_aspect),
        "voice_isolation": (ai.trigger_voice_isolation, intensity),
        "scene_cut_detection": (ai.trigger_scene_cut_detection,),
        "auto_subtitle": (ai.trigger_auto_subtitle, language),
    }
    fn, *args = calls[feature]
    return {"feature": feature, **_ui_call(fn, *args)}


# ────────────────────────────────────────────────────────────────────────────
# Prompts: each workflow guide as a ready-to-use prompt
# ────────────────────────────────────────────────────────────────────────────

def _register_prompts() -> None:
    for wf in list_workflows():
        def make(name: str):
            def prompt(task: str = "") -> str:
                body = f"Follow this resolve-pilot workflow.\n\n{get_workflow(name)}"
                return body + (f"\n\n## The task\n{task}" if task else "")
            return prompt
        mcp.prompt(name=wf["name"], description=wf["description"])(make(wf["name"]))


_register_prompts()


def main() -> None:
    """Entry point for `python -m resolve_pilot.mcp_server` and the console script."""
    mcp.run()


if __name__ == "__main__":
    main()
