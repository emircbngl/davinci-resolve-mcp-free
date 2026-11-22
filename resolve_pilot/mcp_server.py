"""resolve-pilot MCP server.

Exposes the editing toolkit over Model Context Protocol so Claude Desktop /
Claude Code can drive a video editing session end-to-end without an API key.

Tool groups:
- session:  resolve_pilot_status, get_workflow_guide, setup_bridge
- analyze:  scan_folder, probe_media, detect_scenes, detect_beats, transcribe_audio, analyze_quality
- see:      grab_frames, scene_frames, read_video, screenshot_resolve (macOS)
- plan:     plan_from_transcript, plan_from_scenes, create_plan, get_plan, edit_plan
- export:   export_fcpxml, export_srt, render_preview
- resolve:  get_resolve_state, import_timeline, add_markers, add_quality_markers,
            set_clip_color, apply_lut, queue_render, render_status, open_page, run_lua,
            list_lua_snippets, render_lua_snippet
- timeline: import_media, append_clips, remove_clips (live building, no FCPXML)
- inspector / tracks: set_clip_properties, set_tracks, export_timeline
- macOS UI: send_keystroke, menu_pick, open_ai_feature

Resources: resolve://traps, resolve://capabilities, resolve://state and
resolve://guide/{name} (read-only context that costs the agent no tool call).

Run as:
    python -m resolve_pilot.mcp_server
or via Claude Desktop config (see README).
"""
# No `from __future__ import annotations`: the MCP SDK builds tool schemas
# from these signatures, so keep the annotations as real objects.
import base64
import functools
import hashlib
import inspect
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
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

from resolve_pilot import __version__, _jobs, analyzers, bridge, control, luts, traps, verified
from resolve_pilot.analyzers import frames as frame_grab
from resolve_pilot.analyzers import speech as speech_levels
from resolve_pilot._paths import state_dir
from resolve_pilot.editorial import ops as plan_ops
from resolve_pilot.editorial import captions, review, wordcut
from resolve_pilot.editorial import roughcut, store
from resolve_pilot.exporters import fcpxml as fcpxml_export
from resolve_pilot.exporters import preview as preview_export
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
add_markers, add_quality_markers, add_subtitles, set_clip_color, apply_lut, queue_render, open_page, \
set_clip_properties, set_tracks, export_timeline) choose the channel automatically and report \
it in `channel`.
Changing clips and tracks: set_clip_properties (zoom, pan, crop, opacity, scaling, enable / \
disable; a 9:16 reframe) and set_tracks (add, rename, mute / hide, lock) read every value back \
from Resolve; trust `ok` and `mismatched`, not the request. After a visual change, look at it \
with timeline_frames. export_timeline writes FCPXML / EDL / AAF / OTIO / DRT for other NLEs \
or as a backup (drt) before large edits.

Seeing: grab_frames (frames at given times), scene_frames (one frame per scene) and read_video \
(speech next to the matching frames) return images you can look at; layout="grid" packs them \
into one cheaper contact sheet. analyze_quality finds silence, black, frozen and loud/quiet \
stretches. timeline_frames shows the current timeline as Resolve renders it (grades, LUTs, \
titles, transitions) on any OS; use it to verify edits. On macOS screenshot_resolve shows \
Resolve's own window.

Editing loop:
1. Analyze: probe_media (a whole folder: scan_folder, in recording order, with transcripts), \
then detect_scenes / detect_beats / transcribe_audio (cached; returns \
numbered segments). Look before choosing shots: scene_frames, read_video, grab_frames.
2. Draft: plan_from_transcript (pick segment_indices; for talking footage run scan_speech \
first and pass its fillers / repeats as drop_words, cuts land in real pauses between words; \
append_to adds the next file to the same plan), \
plan_from_scenes, or create_plan (any sources, optional music bed). Plans are stored server-side; you get a plan_id.
3. Refine with edit_plan operations (keep/remove/move/trim/update/insert/set) instead of \
resending the plan. Every plan response lists `errors` and `warnings`; resolve them.
4. render_preview(plan_id): the plan as a small MP4 without Resolve, plus `issues` (flash \
clips, cuts in sound, level jumps, silence, black or frozen picture, captions too fast). Its \
times are timeline times: look with grab_frames(path, ...), fix with edit_plan, render again. \
Give `path` to the user to watch.
5. export_fcpxml(plan_id), then import_timeline(path) when live, or tell the user \
File > Import > Timeline.
6. Verify with get_resolve_state(include_clips=true) and timeline_frames (look at the result), \
and iterate. When live, change the open timeline directly instead of re-importing (each import \
makes a new timeline): append_clips adds source ranges (source seconds; a B-roll shot on track 2 \
at record_seconds; new_timeline creates one), import_media fills bins, remove_clips deletes by \
index from get_resolve_state (pass names; ask first). Read their `warnings`.
7. After queue_render(start=true), render_status(wait_seconds=...) says when it is done.

Subtitles: transcribe_audio, then add_subtitles(transcript_id=...) puts them on the timeline's \
subtitle track (or export_srt for a file). For a rough cut pass plan_id instead \
(add_subtitles(plan_id=...) / export_srt(plan_id=...)): the captions follow the cut. \
Never append .srt files with append_clips. Before risky edits (removing clips, \
big rebuilds) call checkpoint(action='save'); the API has no undo. Titles: \
add_title overlays Text+ on a track above V1; never insert titles any other way, as Resolve's \
own title insert splits and ripples the edit.

Conventions: times are seconds unless a parameter says frames. Marker positions can be given \
in seconds; Resolve converts them with the timeline's real frame rate. Paths must be absolute \
paths on the machine running this server (~ is expanded).
Safety: run_lua executes code inside the user's Resolve. Prefer the dedicated tools. Before \
anything destructive (deleting clips, markers or timelines, replacing grades, rendering over \
files) ask the user and suggest saving the project first.
Playbooks: get_workflow_guide() lists step-by-step guides (one-prompt edit of a folder of \
clips, rough cut, subtitles, music video, \
color grade, markers, live control with a Resolve Lua API cheat sheet, traps). Read the matching \
one before a multi-step job.
Resources (read-only, no tool call): resolve://traps (known Resolve traps and what resolve-pilot \
does about them; read it before risky edits or run_lua code), resolve://capabilities (which tools \
work on which channel, edition and OS), resolve://state (live project summary, or how to \
connect), resolve://guide/{name} (the playbooks). Results and errors may name a known trap \
(`known_traps`, "known_trap: <id>"); heed it.
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


def _compact_schema(node: Any, parent: str | None = None) -> Any:
    """Drop pydantic's generated "title" entries ("in_seconds" -> "In Seconds").

    They repeat the field names, and the tool list is loaded into every session:
    about a tenth of its tokens. Properties that are *named* title are kept.
    """
    if isinstance(node, dict):
        return {k: _compact_schema(v, k) for k, v in node.items()
                if not (k == "title" and isinstance(v, str) and parent != "properties")}
    if isinstance(node, list):
        return [_compact_schema(v) for v in node]
    return node


def _plain_optionals(schema: dict[str, Any]) -> dict[str, Any]:
    """Advertise an optional parameter as its type, not as `anyOf [type, null]` + `default
    null`: a sixth of the schema text, and some clients lose the type behind the anyOf.
    Leaving the parameter out is how it is omitted; pydantic still accepts null. Only the
    tool's own parameters: inside nested models null can mean something (audio_bed: null).
    """
    props = {}
    for name, prop in (schema.get("properties") or {}).items():
        kinds = prop.get("anyOf") if isinstance(prop, dict) else None
        if (kinds and len(kinds) == 2 and {"type": "null"} in kinds
                and prop.get("default", 0) is None):
            (kind,) = [k for k in kinds if k != {"type": "null"}]
            prop = {**kind, **{k: v for k, v in prop.items() if k not in ("anyOf", "default")}}
        props[name] = prop
    return {**schema, "properties": props} if props else schema


def _compact_registered(name: str) -> None:
    # Both SDKs keep registered tools on a private _tool_manager; if that ever moves,
    # the tool keeps its full schema, which is fine.
    try:
        registered = mcp._tool_manager.get_tool(name)
        registered.parameters = _plain_optionals(_compact_schema(registered.parameters))
        registered.description = inspect.cleandoc(registered.description or "")
    except Exception:
        pass


# Every tool registered through @tool, in order: name -> (function, annotations).
# resolve://capabilities is built from this, so it cannot drift from the server.
_TOOLS: dict[str, tuple[Any, ToolAnnotations]] = {}


def tool(annotations: ToolAnnotations, title: str | None = None):
    """Register a tool whose exceptions reach the agent as readable ToolErrors.

    mcp 2.x hides the message of unexpected exceptions from the client, which
    leaves the agent guessing; here every failure carries its explanation.
    """
    def decorator(fn):
        _TOOLS[fn.__name__] = (fn, annotations)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except Exception as e:
                raise ToolError(_explain(e)) from e

        mcp.tool(title=title, annotations=annotations)(wrapper)
        _compact_registered(fn.__name__)
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


def _resolve_app() -> tuple[str | None, bool]:
    """Installed Resolve version and whether it is Studio, from the app bundle (macOS).

    (None, False) elsewhere or if unknown; with both editions installed, Free is reported.
    """
    if platform.system() != "Darwin":
        return None, False
    import plistlib
    for app in _MAC_RESOLVE_APPS:
        try:
            with open(app / "Contents" / "Info.plist", "rb") as f:
                found = str(plistlib.load(f).get("CFBundleShortVersionString") or "") or None
            return found, "Studio" in app.name
        except (OSError, plistlib.InvalidFileException):
            continue
    return None, False


def _version_at_least(text: str | None, major: int, minor: int, patch: int = 0) -> bool:
    nums: list[int] = []
    for part in (text or "").split(".")[:3]:
        if not part.isdigit():
            break
        nums.append(int(part))
    if len(nums) < 2:
        return False
    return tuple(nums + [0] * (3 - len(nums))) >= (major, minor, patch)


def _bridge_on_this_resolve(resolve_version: str | None, studio: bool) -> tuple[bool, str | None]:
    """(whether the bridge can start, what to tell the user) for the installed Resolve.

    Free 21.1 restricts Console Lua, and each patch release so far took more away. These
    are user reports, not maintainer measurements (resolve://traps).
    """
    if studio or not _version_at_least(resolve_version, 21, 1):
        return True, None
    if _version_at_least(resolve_version, 21, 1, 2):
        return False, (
            f"Resolve {resolve_version} Free: the bridge cannot start. From 21.1.2 the Free "
            "Console's Lua has no dofile, io or ffi, so nothing run there can reach the bridge's "
            "folder (reported on 21.1.2). For live control use Resolve 20.3.x Free, where the "
            "bridge is verified. On this version export_fcpxml still builds timelines to import "
            "with File > Import > Timeline, and the analysis and frame tools work.")
    return True, (
        f"Resolve {resolve_version} Free: Workspace > Scripts entries do nothing from 21.1. Have "
        "the user paste setup_bridge's console_command into Workspace > Console and press Enter "
        "(reported working on 21.1.1).")


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

    resolve_version, studio_app = _resolve_app()
    next_steps: list[str] = []
    if not has_ffmpeg:
        next_steps.append("Install ffmpeg (macOS: brew install ffmpeg, Windows: winget install ffmpeg, "
                          "Linux: apt install ffmpeg); media analysis and frames need it.")
    if channel == "manual":
        can_start, note = _bridge_on_this_resolve(resolve_version, studio_app)
        if can_start:
            next_steps.append(
                "No live connection to Resolve. For hands-free control (works in Resolve Free): call "
                "setup_bridge(action='install'), then ask the user to click Workspace > Scripts > "
                "ResolvePilotBridge in Resolve. Until then Resolve actions return Lua for the user "
                "to paste.")
        if note:
            next_steps.append(note)
    else:
        next_steps.append("Call get_resolve_state to see the open project and timeline.")
    if not engines:
        next_steps.append("Transcription is not installed. In the resolve-pilot checkout run "
                          "pip install -e '.[transcribe]' on Apple Silicon, otherwise "
                          "pip install -e '.[transcribe-cpu]'.")

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
                "In DaVinci Resolve: Workspace > Console, choose the Lua tab (21.1 opens straight "
                "at a Lua> prompt), paste the whole script and press Enter. It prints '[bridge] ... ready' and keeps running in "
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
            "Console, pick the Lua tab (21.1 opens straight at a Lua> prompt), paste "
            "console_command and press Enter. Then call setup_bridge(action='status') to confirm "
            "it is running."
        ),
    }
    can_start, note = _bridge_on_this_resolve(*_resolve_app())
    if note:
        out["bridge_can_start"] = can_start
        out["resolve_note"] = note
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
    word_timestamps: Annotated[bool, Field(description=(
        "Record each word's time (needed for word-level cuts). false is a little faster."))] = True,
    detail: Annotated[Literal["segments", "words"], Field(description=(
        "'words' lists every word with its index, '[12] 31.20-35.90 0:So 1:today (0.8s) 2:we', "
        "where (0.8s) is a measured pause; address a word as '12.1' in drop_words."))] = "segments",
    offset: Annotated[int, Field(ge=0, description="First segment to return (for paging).")] = 0,
    limit: Annotated[int, Field(ge=0, description="Segments to return; 0 = all.")] = 300,
    wait_seconds: Annotated[float, Field(ge=0, le=600, description="How long to wait before returning status='running'.")] = 45.0,
    refresh: bool = False,
) -> dict[str, Any]:
    """Transcribe speech locally with Whisper; results are cached per file/model/language.

    Returns `transcript_id` and numbered segments as "[i] start-end text". Long files keep
    transcribing in the background: if `status` is "running", call again with the same
    arguments a little later. Page through long transcripts with offset/limit (cached
    calls are instant). Pass `transcript_id` + `segment_indices` to plan_from_transcript;
    scan_speech lists pauses, fillers, repeats and retakes worth cutting.
    """
    res = transcripts.get_or_start(path, model=model, language=language,
                                   word_timestamps=word_timestamps, engine=engine,
                                   wait_seconds=wait_seconds,
                                   # Paging reads the cache; only the first page may re-run.
                                   refresh=refresh and offset == 0)
    if res["status"] != "done":
        res["hint"] = ("Still transcribing in the background. Call transcribe_audio again with the "
                       "same arguments to collect the result (refresh will not restart a job that "
                       "is running).")
        return res
    segs = res["segments"]
    has_words = any(s.get("words") for s in segs)
    levels = transcripts.transcript_levels(res)
    if detail == "words":
        shown = wordcut.format_words(transcripts.transcript_segments(res), levels, offset, limit)
    else:
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
        "word_timings": has_words,
        "pauses_measured": levels is not None,
        "segments": shown,
    }
    if levels is None and res.get("levels_error"):
        out["pauses_error"] = res["levels_error"]
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
    end: float = Field(ge=0)
    text: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> "SegmentIn":
        if self.end < self.start:
            raise ValueError(f"segment end ({self.end}) must not be before start ({self.start})")
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
    pad_seconds: Annotated[float, Field(ge=0, le=2, description=(
        "Pause kept on each side of a cut (0.2 natural, 0.05 punchy); never reaches into "
        "a neighbouring word."))] = 0.2,
    fps: Annotated[float | None, Field(gt=0, description="Timeline fps; defaults to the source's.")] = None,
    drop_words: Annotated[list[str] | None, Field(description=(
        "Words to cut out of the kept segments: 'segment.word' or 'segment.first-last', "
        "e.g. ['12.0', '14.3-5']. Indices from transcribe_audio(detail='words') or scan_speech."))] = None,
    max_pause_seconds: Annotated[float | None, Field(ge=0, le=10, description=(
        "Shorten longer pauses inside kept speech to this (0.4 = tight jump cuts). "
        "Default: leave them."))] = None,
    append_to: Annotated[str | None, Field(description=(
        "plan_id to add these clips to the end of (a cut from several files) instead of "
        "making a new plan; title and fps are that plan's."))] = None,
) -> dict[str, Any]:
    """Draft a rough cut from transcript segments: one clip per kept segment.

    Choose the segments editorially (best takes, complete thoughts, no false starts) and
    pass their indices in the order they should play; drop_words removes fillers, repeats
    and false starts inside them. Cuts are placed in the real pause next to each word
    (measured in the audio), never inside a word. `cut_report` counts the cuts that had
    no pause to sit in (`tight`): look at or listen to those. Returns the stored plan
    (plan_id, clips, warnings). The plan remembers its transcript: captions for the cut
    come from export_srt(plan_id=...) or add_subtitles(plan_id=...). For a cut from several
    files call it once per file in playing order, from the second on with append_to.
    """
    levels = None
    if transcript_id:
        data = transcripts.require_transcript(transcript_id)
        segs = transcripts.transcript_segments(data)
        levels = transcripts.transcript_levels(data)
        source_path = source_path or data["source_path"]
    elif segments:
        segs = [Segment(s.start, s.end, s.text) for s in segments]
    else:
        raise ValueError("Pass transcript_id (from transcribe_audio) or segments.")
    if not source_path:
        raise ValueError("source_path is required when passing inline segments.")
    plan, report = roughcut.transcript_cut(
        source_path, segs, title=title, pad_seconds=pad_seconds, fps=fps,
        segment_indices=segment_indices, levels=levels, drop_words=drop_words,
        max_pause_seconds=max_pause_seconds)
    if transcript_id and plan.clips:  # export_srt(plan_id=...) retimes the captions from it
        plan.transcript_ids[plan.clips[0].source_path] = transcript_id
    if not append_to:
        out = _store_and_summarize(plan)
        out["cut_report"] = report
        return out
    with store.plan_lock(append_to):
        whole = store.load_plan(append_to)
        first = len(whole.clips)
        whole.clips.extend(plan.clips)
        whole.transcript_ids.update(plan.transcript_ids)
        out = _store_and_summarize(whole, plan_id=append_to)
    for row in report.get("tight_clips", []):  # the report speaks of the plan's clip numbers
        row["clip"] += first
    out["cut_report"] = report
    out["appended"] = {"first_clip": first, "clips": len(plan.clips),
                       "seconds": _r(sum(c.duration_seconds for c in plan.clips))}
    return out


_MEDIA_SUFFIXES = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".mxf", ".webm",
                   ".wav", ".mp3", ".m4a", ".aac", ".flac", ".aif", ".aiff"}


@tool(READ, title="Scan a folder of footage")
def scan_folder(
    folder: Annotated[str, Field(description="Absolute path of a folder with media files (not searched deeper).")],
    transcribe: Annotated[bool, Field(description=(
        "Also transcribe every file that has sound, in the background; call again with the "
        "same arguments to follow it."))] = False,
    language: Annotated[str | None, Field(description="Spoken language (e.g. 'tr'), as for transcribe_audio.")] = None,
    wait_seconds: Annotated[float, Field(ge=0, le=600, description="With transcribe: how long to wait before answering.")] = 30.0,
) -> dict[str, Any]:
    """The footage of a shoot at a glance: every media file in a folder in recording
    order (the camera's own time, else the file's), with length, frame, rotation tag
    and whether it has sound.

    With transcribe=true each file with sound also gets its transcript (the same cached
    ones transcribe_audio returns): `transcript_id`, word count and how it starts, or
    `status: running`. Start here when the user points at a folder; then read the
    transcripts, cut each file with plan_from_transcript (append_to for the second file
    on) and watch the result with render_preview.
    """
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Not a folder: {root}")
    found, unreadable = [], []
    for p in sorted(root.iterdir()):
        if p.name.startswith(".") or p.suffix.lower() not in _MEDIA_SUFFIXES or not p.is_file():
            continue
        info = analyzers.probe_media_cached(p)
        if info is None or not (info.video_codec or info.audio_codec):
            unreadable.append(p.name)
            continue
        found.append((info.recorded or "", p.stat().st_mtime, p, info))
    # Camera time where every file has one; a folder of copies keeps only that order.
    found.sort(key=(lambda f: (f[0], f[2].name)) if all(f[0] for f in found) else (lambda f: (f[1], f[2].name)))
    files: list[dict[str, Any]] = []
    for recorded, _, p, info in found:
        row: dict[str, Any] = {"path": str(p), "seconds": _r(info.duration_seconds, 1),
                               "has_sound": info.audio_codec is not None}
        if info.video_codec:
            row.update(frame=f"{info.width}x{info.height}", fps=_r(info.fps, 2))
            if info.rotation % 360:
                row["rotation"] = info.rotation
        if recorded:
            row["recorded"] = recorded
        files.append(row)
    out: dict[str, Any] = {"folder": str(root), "file_count": len(files),
                           "total_seconds": _r(sum(f["seconds"] for f in files), 1), "files": files}
    if unreadable:
        out["unreadable"] = unreadable
    if not transcribe:
        return out

    import time as _time
    deadline = _time.monotonic() + wait_seconds
    with_sound = [f for f in files if f["has_sound"]]
    # Start them all (they run one after another), then wait for as many as the time allows.
    for wait in (False, True):
        for f in with_sound:
            if f.get("transcript", {}).get("status") in ("done", "failed"):
                continue
            try:
                res = transcripts.get_or_start(
                    f["path"], language=language,
                    wait_seconds=max(0.0, deadline - _time.monotonic()) if wait else 0.0)
            except Exception as e:  # one bad file must not hide the others
                f["transcript"] = {"status": "failed", "error": _explain(e)}
                continue
            if res["status"] != "done":
                f["transcript"] = {"status": "running"}
                continue
            words = sum(len(s.get("words") or s["text"].split()) for s in res["segments"])
            text = " ".join(s["text"].strip() for s in res["segments"])
            f["transcript"] = {"status": "done", "transcript_id": res["transcript_id"],
                               "language": res["language"], "segments": len(res["segments"]),
                               "words": words, "starts": text[:120]}
    done = sum(f["transcript"]["status"] == "done" for f in with_sound)
    failed = sum(f["transcript"]["status"] == "failed" for f in with_sound)
    out["transcripts"] = {"done": done, "running": len(with_sound) - done - failed, "failed": failed}
    if done + failed < len(with_sound):
        out["next_step"] = "Still transcribing: call scan_folder again with the same arguments."
    else:
        out["next_step"] = ("Read each transcript (transcribe_audio(path) returns the cached one, "
                            "scan_speech(transcript_id) the cut candidates), then plan_from_transcript "
                            "per file in playing order, with append_to from the second file on.")
    return out


@tool(READ, title="Scan speech for cuts")
def scan_speech(
    transcript_id: Annotated[str, Field(description="From transcribe_audio.")],
    min_pause_seconds: Annotated[float, Field(ge=0.2, le=30, description="Report pauses at least this long.")] = 0.7,
    extra_fillers: Annotated[list[str] | None, Field(description=(
        "More filler words to flag, e.g. the speaker's habit words."))] = None,
) -> dict[str, Any]:
    """List what an editor would consider cutting from a talk: long pauses, filler words,
    immediate repeats ("the the", "so we, so we") and retakes (a line started twice).

    Candidates, not decisions: read them against the transcript. `drop` values go into
    plan_from_transcript(drop_words=[...]); for a retake leave one of the two segments
    out of segment_indices (usually the earlier); long pauses inside a segment shrink
    with max_pause_seconds. Whisper often leaves "um" / "uh" out of the text, so a
    missing filler here does not mean it was not said.
    """
    data = transcripts.require_transcript(transcript_id)
    out = wordcut.scan(transcripts.transcript_segments(data), transcripts.transcript_levels(data),
                       language=data.get("language"), min_pause_seconds=min_pause_seconds,
                       extra_fillers=extra_fillers)
    out["transcript_id"] = transcript_id
    if not out["word_timings"]:
        out["note"] = ("This transcript has no word timings, so fillers and repeats cannot be "
                       "found: run transcribe_audio again with word_timestamps=true.")
    elif not out["pauses_measured"]:
        out["note"] = ("Pauses could not be measured in the audio"
                       + (f" ({data['levels_error']})" if data.get("levels_error") else "")
                       + ", so long_pauses is empty.")
    return out


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
    - turn {source, degrees}: turn every clip of one source (index into `sources`) by 90 /
      180 / 270 degrees clockwise, for footage that plays on its side (a phone held
      sideways but tagged upright). render_preview shows it; FCPXML cannot carry it
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

def _reserve(p: Path) -> Path:
    """Claim ``p``, or ``name (2).ext``, ``name (3).ext``..., by creating it empty.

    Creating the file (O_EXCL) is what reserves the name, so parallel exports on
    mcp 2.x can't pick the same one and overwrite each other.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 2
    candidate = p
    while True:
        try:
            with open(candidate, "x"):
                return candidate
        except FileExistsError:
            candidate = p.with_name(f"{p.stem} ({n}){p.suffix}")
            n += 1


def _default_output(stem_source: str | None, title: str, suffix: str) -> Path:
    """Default export location (reserved); never an existing file (the user's own subtitles, say)."""
    slug = re.sub(r"[^\w\-]+", "_", title).strip("_") or "resolve_pilot"
    target = state_dir("exports") / f"{slug}{suffix}"
    if stem_source:
        d = Path(stem_source).expanduser().parent
        if d.is_dir() and os.access(d, os.W_OK):
            target = d / f"{slug}{suffix}"
    return _reserve(target)


def _explicit_output(out_path: str, overwrite: bool) -> Path:
    p = Path(out_path).expanduser()
    if p.exists() and not overwrite:
        raise ValueError(f"{p} already exists. Pass overwrite=true to replace it, or another out_path.")
    return p


def _write_reserved(target: Path, reserved: bool, write) -> Path:
    try:
        return write(target)
    except BaseException:
        if reserved:
            target.unlink(missing_ok=True)
        raise


OutPath = Annotated[str | None, Field(description=(
    "Where to write. Omit for a default next to the source, numbered so it never replaces "
    "an existing file."))]
Overwrite = Annotated[bool, Field(description="Replace out_path if it already exists.")]


@tool(DESTRUCTIVE, title="Export FCPXML")
def export_fcpxml(plan_id: str, out_path: OutPath = None, overwrite: Overwrite = False) -> dict[str, Any]:
    """Write a stored plan as FCPXML 1.10 (Resolve Free & Studio: File > Import > Timeline).

    Refuses plans with errors. Afterwards call import_timeline(path) to load it into Resolve
    directly when the bridge or Studio API is available.
    """
    plan = store.load_plan(plan_id)
    report = roughcut.validate_plan(plan)
    if report["errors"]:
        raise ValueError("Fix these plan errors first (edit_plan): " + "; ".join(report["errors"]))
    spec = roughcut.plan_to_timeline_spec(plan)
    if out_path:
        target, reserved = _explicit_output(out_path, overwrite), False
    else:
        target, reserved = _default_output(plan.clips[0].source_path, plan.title, ".fcpxml"), True
    final = _write_reserved(target, reserved, lambda t: fcpxml_export.write_fcpxml(spec, t))
    warnings = list(report["warnings"])
    turned = []
    for path, degrees in plan.source_turns.items():
        clips = [i for i, c in enumerate(plan.clips) if c.source_path == path]
        if clips and degrees:
            turned.append({"source": path, "degrees": degrees, "clips": clips})
    if turned:
        warnings.append(
            f"{len(turned)} source(s) are turned in the plan (see `turned_sources`); FCPXML does not "
            "carry that, so the imported clips still play as their files are tagged. After "
            "import_timeline turn those clips with set_clip_properties (RotationAngle, then "
            "ZoomX / ZoomY to fill the frame) and look with timeline_frames. Not tried in Resolve yet.")
    return {
        "path": str(final),
        "clip_count": len(plan.clips),
        "total_seconds": _r(plan.total_seconds),
        "warnings": warnings,
        **({"turned_sources": turned} if turned else {}),
        "next_step": f"import_timeline(path={str(final)!r}) or, without a live channel, "
                     "tell the user: File > Import > Timeline.",
    }


def _srt_result(cues: list[srt_export.SrtCue], target: Path, reserved: bool) -> dict[str, Any]:
    final = _write_reserved(target, reserved, lambda t: srt_export.write_srt(cues, t))
    durations = [c.end - c.start for c in cues] or [0.0]
    return {
        "path": str(final),
        "cue_count": len(cues),
        "average_cue_seconds": _r(sum(durations) / len(durations), 2),
        "longest_cue_seconds": _r(max(durations), 2),
    }


def _export_plan_srt(plan_id: str, transcript_ids: list[str], out_path: str | None, overwrite: bool,
                     max_chars_per_line: int, max_lines: int) -> dict[str, Any]:
    """Captions for a plan's timeline: every clip's words, moved to where the clip plays."""
    plan = store.load_plan(plan_id)
    errors = roughcut.validate_plan(plan)["errors"]
    if errors:
        raise ValueError("Fix these plan errors first (edit_plan): " + "; ".join(errors))
    cues, report, warnings = _plan_cues(plan, transcript_ids, max_chars_per_line, max_lines)
    missing = report["clips_without_transcript"]
    if len(missing) == len(plan.clips):
        raise ValueError(
            "No clip of this plan has a transcript. Run transcribe_audio on the sources and pass "
            "transcript_ids. " + " ".join(warnings))
    if not cues:
        raise ValueError("Nothing is said in the kept ranges of this plan; there are no captions to write.")
    if out_path:
        target, reserved = _explicit_output(out_path, overwrite), False
    else:
        target, reserved = _default_output(plan.clips[0].source_path, plan.title, ".srt"), True
    out = _srt_result(cues, target, reserved)
    out.update(timed_to_plan=plan_id, timeline_seconds=_r(plan.total_seconds), warnings=warnings)
    if missing:
        out["clips_without_transcript"] = missing
    return out


def _plan_cues(plan: roughcut.RoughCutPlan, transcript_ids: list[str], max_chars_per_line: int = 42,
               max_lines: int = 2) -> tuple[list[srt_export.SrtCue], dict[str, Any], list[str]]:
    """(captions on the plan's timeline, captions.plan_captions report, warnings)."""
    warnings: list[str] = []
    by_source: dict[str, dict[str, Any]] = {}
    for path, tid in plan.transcript_ids.items():
        data = transcripts.load_transcript(tid)
        if data is None:
            warnings.append(f"transcript {tid} of {Path(path).name} is no longer stored; "
                            "run transcribe_audio on it again")
        else:
            by_source[captions.source_key(path)] = data
    for tid in transcript_ids:  # explicit ones win over what the plan remembers
        data = transcripts.require_transcript(tid)
        by_source[captions.source_key(data["source_path"])] = data
    cues, report = captions.plan_captions(
        plan, {k: transcripts.transcript_segments(d) for k, d in by_source.items()},
        max_chars_per_line=max_chars_per_line, max_lines=max_lines)
    missing = report["clips_without_transcript"]
    if 0 < len(missing) < len(plan.clips):
        names = sorted({Path(plan.clips[i].source_path).name for i in missing})
        warnings.append(f"{len(missing)} clip(s) have no transcript and get no captions "
                        f"({', '.join(names)}); transcribe_audio them and pass transcript_ids")
    if cues and not report["word_level"]:
        warnings.append("a transcript has no word timings, so its captions move per segment "
                        "and cuts inside a segment are not followed; transcribe_audio again "
                        "(word_timestamps=true)")
    return cues, report, warnings


@tool(DESTRUCTIVE, title="Export SRT")
def export_srt(
    transcript_id: Annotated[str | None, Field(description="From transcribe_audio.")] = None,
    segments: Annotated[list[SegmentIn] | None, Field(description="Inline cues instead, e.g. a translation you wrote.")] = None,
    out_path: Annotated[str | None, Field(description=(
        "Where to write. Default: next to the source as <source>.<language>.srt (<plan title>.srt "
        "with plan_id), numbered so it never replaces a file. Required with inline segments."))] = None,
    overwrite: Overwrite = False,
    max_chars_per_line: Annotated[int, Field(ge=0, description="Wrap lines; 0 disables. 42 broadcast, ~32 vertical video.")] = 42,
    max_lines: Annotated[int, Field(ge=1, le=3)] = 2,
    plan_id: Annotated[str | None, Field(description=(
        "Captions timed to this plan's timeline: they follow its cut."))] = None,
    transcript_ids: Annotated[list[str] | None, Field(description=(
        "With plan_id: transcripts of its sources, for plans not made by plan_from_transcript."))] = None,
) -> dict[str, Any]:
    """Write subtitles as SRT; long cues are wrapped and split into readable chunks.

    After a rough cut pass plan_id: each caption moves with the clip it is spoken in, on
    the same frame positions export_fcpxml writes, and edit_plan changes are followed (export
    again after editing). Without plan_id the captions keep the source's own times. Read
    `warnings`. Zero-length cues are dropped. Import in Resolve with add_subtitles, by
    dragging the .srt onto the timeline or File > Import > Subtitle.
    """
    if plan_id:
        if segments:
            raise ValueError("plan_id works from transcripts; pass transcript_ids, not inline segments.")
        ids = list(transcript_ids or []) + ([transcript_id] if transcript_id else [])
        return _export_plan_srt(plan_id, ids, out_path, overwrite, max_chars_per_line, max_lines)
    if transcript_ids:
        raise ValueError("transcript_ids goes with plan_id; without a plan pass one transcript_id.")
    target: Path | None = None
    reserved = False
    if transcript_id:
        data = transcripts.require_transcript(transcript_id)
        raw = [srt_export.SrtCue(float(s["start"]), float(s["end"]), str(s["text"]))
               for s in data["segments"]]
        if not out_path:
            src = Path(data["source_path"])
            lang = f".{data['language']}" if data.get("language") else ""
            target, reserved = _reserve(src.with_name(f"{src.stem}{lang}.srt")), True
    elif segments:
        raw = [srt_export.SrtCue(s.start, s.end, s.text) for s in segments]
        if not out_path:
            raise ValueError("out_path is required with inline segments.")
    else:
        raise ValueError("Pass transcript_id or segments.")
    if target is None:
        target = _explicit_output(out_path, overwrite)
    kept = [c for c in raw if c.end > c.start]
    cues = srt_export.wrap_cues(kept, max_chars_per_line=max_chars_per_line, max_lines=max_lines)
    out = _srt_result(cues, target, reserved)
    if len(kept) < len(raw):
        out["dropped_zero_length_cues"] = len(raw) - len(kept)
    return out


def _preview_key(plan: roughcut.RoughCutPlan, height: int, cues: list[srt_export.SrtCue]) -> str:
    """Changes when the plan, its captions or a source file does."""
    files = []
    for path in sorted({c.source_path for c in plan.clips} | ({plan.audio_bed.source_path} if plan.audio_bed else set())):
        try:
            st = Path(path).expanduser().stat()
            files.append([path, st.st_size, int(st.st_mtime)])
        except OSError:
            files.append([path, None, None])
    blob = json.dumps([plan.to_dict(), height, [[c.start, c.end, c.text] for c in cues], files],
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _render_and_review(plan: roughcut.RoughCutPlan, target: Path, height: int,
                       cues: list[srt_export.SrtCue], report) -> dict[str, Any]:
    out = preview_export.render(plan, target, height=height, cues=cues, on_progress=report)
    lay = fcpxml_export.layout(roughcut.plan_to_timeline_spec(plan))
    starts = [b * out["seconds"] / out["frames"] for b in lay.bounds]
    infos = {c.source_path: analyzers.probe_media_cached(c.source_path) for c in plan.clips}
    issues = review.plan_issues(plan, starts, infos) + review.caption_issues(cues, starts)
    not_checked = []
    try:
        spans, meta = analyzers.analyze_quality(out["path"])
        issues += review.quality_issues(spans, starts, out["seconds"])
        if meta["detectors_skipped"]:
            not_checked.append("this ffmpeg lacks: " + ", ".join(map(str, meta["detectors_skipped"])))
    except Exception as e:  # the preview exists; say what could not be looked at
        not_checked.append(f"silence / black / frozen picture: {e}")
    try:
        issues += review.cut_issues(plan, starts, speech_levels.voice_levels(out["path"]))
    except Exception as e:
        not_checked.append(f"sound at the cuts: {e}")
    out.update(review.summarize(issues), not_checked=not_checked, cuts=len(review.real_cuts(plan)))
    return out


@tool(WRITE_FILES, title="Render and check a preview")
def render_preview(
    plan_id: str,
    height: Annotated[int, Field(ge=90, le=1080, description="Preview height in pixels (never above the plan's).")] = 540,
    transcript_ids: Annotated[list[str] | None, Field(description=(
        "Transcripts of the plan's sources for captions, when the plan does not know them (see export_srt)."))] = None,
    wait_seconds: Annotated[float, Field(ge=0, le=600, description=(
        "How long to wait; on `status: running` call again with the same arguments."))] = 60.0,
    refresh: Annotated[bool, Field(description="Render again even if this plan's preview is up to date.")] = False,
) -> dict[str, Any]:
    """Watch a plan without Resolve: render it to a small MP4 and check it automatically.

    The preview has exactly the timeline's frames, so its times are timeline times:
    look at any moment with grab_frames(path, at_seconds=[...]) or read_video(path), and
    give `path` to the user to watch. Captions ride along as a subtitle track when the
    plan's transcripts are known. `issues` lists what to look at or listen to, each with
    its clip and timeline second: flash_clip, cut_in_sound (no dip to cut in: a clipped
    syllable or a click), level_jump, silence, black_picture, frozen_picture,
    fast_caption, rotated_source. They are candidates, not verdicts: check them, fix the
    plan with edit_plan and render again (one file per plan, replaced). `not_shown` says
    what the preview leaves out (dissolves, fades); `not_checked` what could not be
    measured.
    """
    plan = store.load_plan(plan_id)
    validation = roughcut.validate_plan(plan)
    if validation["errors"]:
        raise ValueError("Fix these plan errors first (edit_plan): " + "; ".join(validation["errors"]))
    cues, _, caption_warnings = _plan_cues(plan, list(transcript_ids or []))
    key = _preview_key(plan, height, cues)
    target = state_dir("previews") / f"{plan_id}.mp4"
    note = target.with_suffix(".json")

    def finish(result: dict[str, Any], cached: bool) -> dict[str, Any]:
        return {"status": "done", "cached": cached, **result,
                "seconds": _r(result["seconds"]), "plan_warnings": validation["warnings"],
                "caption_warnings": caption_warnings,
                "next_step": "Look at the issues with grab_frames(path, at_seconds=[...]) or "
                             "read_video(path); fix with edit_plan and render again. When it is "
                             "right: export_fcpxml(plan_id)."}

    if not refresh and target.is_file():
        try:
            saved = json.loads(note.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if saved.get("key") == key:
            return finish(saved["result"], cached=True)

    def work(report) -> dict[str, Any]:
        result = _render_and_review(plan, target, height, cues, report)
        tmp = note.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"key": key, "result": result}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(note)
        return result

    job = _jobs.run(f"preview-{plan_id}-{key}-{refresh}", work, wait_seconds)
    if job["status"] == "running":
        done, total = job["progress"] or [0, len(plan.clips)]
        return {"status": "running", "elapsed_seconds": job["elapsed_seconds"],
                "pieces_done": done, "pieces": total,
                "next_step": "Call render_preview again with the same arguments."}
    return finish(job["result"], cached=False)


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


_PROPERTY_HELP = (
    "Inspector values by key. Numbers: ZoomX, ZoomY (0-100, 1 = 100%), Pan, Tilt (pixels, "
    "documented range +-4x the timeline width/height), RotationAngle (-360..360), AnchorPointX, "
    "AnchorPointY, Pitch, Yaw (-1.5..1.5), CropLeft, CropRight, CropTop, CropBottom (pixels, >= 0), "
    "CropSoftness (-100..100), Distortion (-1..1), Opacity (0-100). Booleans: ZoomGang, FlipX, "
    "FlipY, CropRetain. Names (or the integer value): Scaling (use_project, crop, fit, fill, "
    "stretch), CompositeMode (normal, screen, multiply, add, overlay, ...), DynamicZoomEase "
    "(linear, in, out, in_and_out), RetimeProcess (use_project, nearest, frame_blend, "
    "optical_flow), MotionEstimation (use_project, standard_faster, ..., speed_warp_better), "
    "ResizeFilter (use_project, sharper, smoother, lanczos, ...).")


@tool(_hints(read_only=False, idempotent=True), title="Set clip properties")
def set_clip_properties(
    track_type: Literal["video", "audio"] = "video",
    track: Annotated[int, Field(ge=1, description="Track number (V1 = 1).")] = 1,
    indices: Annotated[list[Annotated[int, Field(ge=0)]] | None, Field(min_length=1, description=(
        "0-based clip indices as get_resolve_state(include_clips=true) lists them "
        "(transitions skipped). Omit for every clip on the track."))] = None,
    properties: Annotated[dict[str, Any] | None, Field(description=_PROPERTY_HELP)] = None,
    enabled: Annotated[bool | None, Field(description="Enable (true) or disable (false) the clips.")] = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Change Inspector properties of timeline clips (zoom, position, rotation, crop, flip,
    opacity, composite mode, scaling, retime) and enable/disable them, with read-back.

    Each clip comes back as {index, name, requested, actual, ok, mismatched}: `ok` is what
    Resolve reports *after* the write (GetProperty / GetClipEnabled), not the setter's
    return value, which can be true for a value that did not stick (`contradicted`).
    9:16 reframe of 16:9 footage on a 1080x1920 timeline: properties={"Scaling": "fill"}
    fills the frame (crops the sides); then move the subject into view with Pan, or
    ZoomX/ZoomY above 1 to punch in. Pan's unit and sign are not verified yet: check the
    picture with timeline_frames and adjust. See get_workflow_guide('rough-cut').
    """
    rows = lua_lib.normalize_clip_properties(properties)  # fail fast, before any channel
    if not rows and enabled is None:
        raise ValueError("Nothing to change: pass properties and/or enabled.")
    args = dict(track_type=track_type, track=track, indices=indices, properties=properties,
                enabled=enabled)
    return control.perform(lua_lib.snippet_set_clip_properties(**args),
                           lambda: _studio().set_clip_properties(**args), channel)


class TrackChange(BaseModel):
    track_type: Literal["video", "audio", "subtitle"]
    index: int = Field(ge=1, description="Track number (V1 / A1 = 1).")
    name: str | None = None
    enabled: bool | None = Field(None, description="false hides a video track / mutes an audio track.")
    locked: bool | None = None


class TrackAdd(BaseModel):
    track_type: Literal["video", "audio", "subtitle"]
    sub_type: str | None = Field(None, description=(
        "Audio only: mono, stereo, 5.1, 5.1film, 7.1, 7.1film, adaptive1-adaptive24."))


@tool(EDIT_RESOLVE, title="Set tracks")
def set_tracks(
    changes: Annotated[list[TrackChange] | None, Field(description=(
        "Rename, enable/disable or lock/unlock existing tracks."))] = None,
    add: Annotated[list[TrackAdd] | None, Field(description=(
        "Tracks to append (done before `changes`, so a change can name a track added here)."))] = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Add tracks and rename, enable/disable (mute a B-roll or music track) or lock tracks of
    the current timeline.

    Every change is read back from Resolve: each comes back as {requested, actual, ok,
    mismatched}, and added tracks report the track count before and after.
    """
    args = dict(changes=[c.model_dump(exclude_none=True) for c in changes or []],
                add=[a.model_dump(exclude_none=True) for a in add or []])
    lua_lib.normalize_track_changes(**args)  # fail fast, before any channel
    return control.perform(lua_lib.snippet_set_tracks(**args),
                           lambda: _studio().set_tracks(**args), channel)


def _export_candidates(path: Path) -> list[Path]:
    """Where an export can land: the path itself, or a bundle folder next to it (Resolve
    has been reported to write FCPXML 1.10 as a .fcpxmld bundle)."""
    out = [path]
    if path.suffix.lower() == ".fcpxml":
        out += [path.with_suffix(".fcpxmld"), Path(str(path) + "d")]
    return list(dict.fromkeys(out))


def _snapshot(path: Path) -> tuple[int, int] | None:
    try:
        if path.is_dir():
            files = [f for f in path.rglob("*") if f.is_file()]
            return sum(f.stat().st_size for f in files), max((f.stat().st_mtime_ns for f in files), default=0)
        st = path.stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


def _verify_export(path: Path, before: dict[Path, tuple[int, int] | None]) -> dict[str, Any]:
    """Check on disk that Resolve really wrote the export (non-empty, new or rewritten)."""
    for cand in _export_candidates(path):
        snap = _snapshot(cand)
        if snap is None:
            continue
        size, _ = snap
        out: dict[str, Any] = {"path": str(cand), "size_bytes": size, "is_bundle": cand.is_dir()}
        if cand.is_dir():
            out["bundle_files"] = sorted(str(f.relative_to(cand)) for f in cand.rglob("*") if f.is_file())[:20]
        if size == 0:
            out.update(verified=False, error=f"Resolve wrote {cand}, but it is empty.")
        elif before.get(cand) == snap:
            out.update(verified=False, error=f"{cand} already existed and was not rewritten.")
        else:
            out["verified"] = True
        return out
    return {"verified": False, "error": f"Nothing was written at {path}."}


@tool(DESTRUCTIVE, title="Export timeline")
def export_timeline(
    format: Annotated[Literal[tuple(lua_lib.EXPORT_FORMATS)], Field(description=(  # type: ignore[valid-type]
        "fcpxml (= 1.10, Final Cut / Premiere via FCPXML), fcpxml_1_9, fcpxml_1_8, fcp7_xml, "
        "edl (+ edl_cdl, edl_sdl, edl_missing_clips), aaf (new) / aaf_existing (Pro Tools, Avid), "
        "otio (OpenTimelineIO), drt (Resolve's own timeline file, the best backup), "
        "csv / tab (edit list as text), ale / ale_cdl."))] = "fcpxml",
    out_path: Annotated[str | None, Field(description=(
        "Defaults to <timeline name>_<date-time>.<ext> in resolve-pilot's exports folder."))] = None,
    timeline: Annotated[str | None, Field(description="Timeline name; defaults to the current one.")] = None,
    overwrite: Overwrite = False,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Export a Resolve timeline as FCPXML, EDL, AAF, OTIO, DRT, CSV and more.

    Use it to hand a cut to another NLE or a sound editor, to read back the real cut, or to
    back up a timeline (drt) before large edits; import_timeline loads it back. The file is
    checked on disk afterwards: `ok` needs a non-empty file (or bundle folder) that Resolve
    actually wrote, and `size_bytes` says how big it is.
    """
    import time
    export_type, subtype, _ext = lua_lib.EXPORT_FORMATS[format]
    if out_path:
        target: Path | None = Path(out_path).expanduser().resolve()
        existing = [c for c in _export_candidates(target) if c.exists()]
        if existing and not overwrite:
            raise ValueError(f"{existing[0]} already exists. Pass overwrite=true to replace it, "
                             "or another out_path.")
        target.parent.mkdir(parents=True, exist_ok=True)
        args = dict(out_path=str(target), export_type=export_type, export_subtype=subtype,
                    timeline_name=timeline)
    else:
        target = None
        args = dict(export_type=export_type, export_subtype=subtype, timeline_name=timeline,
                    out_dir=str(state_dir("exports")),
                    # Unique per call: two exports in the same second (parallel calls,
                    # edl + edl_cdl) must not land on, and overwrite, the same file.
                    stamp=f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}")
    before = {c: _snapshot(c) for c in _export_candidates(target)} if target else {}
    out = control.perform(lua_lib.snippet_export_timeline(**args),
                          lambda: _studio().export_timeline(**args), channel, timeout=180.0)
    out["format"] = format
    if out.get("channel") == "manual" or not out.get("path"):
        return out
    check = _verify_export(Path(out["path"]), before)
    resolve_said, resolve_error = bool(out.get("ok")), out.pop("error", None)
    out.update(check)
    out["ok"] = bool(check["verified"])
    if out["ok"] and not resolve_said:
        out["note"] = "Resolve reported failure, but a fresh non-empty file is there."
    elif not out["ok"] and resolve_said:
        out["error"] = "Resolve reported success, but: " + check["error"]
    elif not out["ok"]:
        out["error"] = resolve_error or check["error"]
    if out["ok"]:
        out["next_step"] = f"import_timeline(path={out['path']!r}) loads it back into Resolve."
    return out


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
    preset: Annotated[str | None, Field(description=(
        "Render preset loaded first; a wrong name returns available_presets. None keeps the "
        "current Deliver settings."))] = "H.264 Master",
    target_dir: str = "~/Desktop/Renders",
    custom_name: Annotated[str | None, Field(description="Output file name without extension.")] = None,
    start: Annotated[bool, Field(description="Start rendering right away (then render_status).")] = False,
    format: Annotated[str | None, Field(description="Container, e.g. mp4, mov (with codec).")] = None,
    codec: Annotated[str | None, Field(description="e.g. H264, H265, ProRes422HQ; a wrong one returns available_codecs.")] = None,
    width: Annotated[int | None, Field(ge=16, le=16384, description="Output width, e.g. 1080 for 9:16.")] = None,
    height: Annotated[int | None, Field(ge=16, le=16384, description="Output height, e.g. 1920 for 9:16.")] = None,
    start_seconds: Annotated[float | None, Field(ge=0, description="Render only from here (timeline seconds).")] = None,
    end_seconds: Annotated[float | None, Field(gt=0, description="...to here.")] = None,
    export_audio: bool | None = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Add the current timeline (or a part of it) to the render queue, optionally starting it.

    Social delivery: make_format_version(aspect='9:16') first, then queue_render(format='mp4',
    codec='H264', width=1080, height=1920). A 16:9 timeline rendered at 9:16 is letterboxed,
    not cropped. The job's real settings come back in `settings`.
    """
    args = (preset, target_dir, custom_name, start, format, codec, width, height,
            start_seconds, end_seconds, export_audio)
    simple = all(v is None for v in args[4:])
    return control.perform(
        lua_lib.snippet_render_current_timeline(*args),
        (lambda: _studio().render_current_timeline(preset, target_dir, custom_name, start)) if simple else None,
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
    out = bridge.exec_lua(lua, timeout=timeout_seconds).to_dict()
    hints = traps.hints_for_lua(lua) if code is not None else []
    if hints:
        out["known_traps"] = hints
    return out


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
    if not isinstance(out, dict):
        return {"ok": True}
    if out.get("ok") is False and "Studio" in str(out.get("error", "")):
        out = {**out, "studio_only": True,
               "hint": "This needs DaVinci Resolve Studio. Tell the user, or do it another way."}
    return out


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


_SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def _srt_times(path: str) -> list[tuple[float, float]]:
    def sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return [(sec(*m.groups()[:4]), sec(*m.groups()[4:])) for m in _SRT_TIME.finditer(text)]


@tool(EDIT_RESOLVE, title="Add subtitles to the timeline")
def add_subtitles(
    srt_path: Annotated[str | None, Field(description="An .srt file (e.g. from export_srt).")] = None,
    transcript_id: Annotated[str | None, Field(description=(
        "Or a transcript from transcribe_audio: written as SRT next to its source first."))] = None,
    replace: Annotated[bool, Field(description=(
        "Swap out captions already on subtitle track 1; otherwise that is refused."))] = False,
    max_chars_per_line: Annotated[int, Field(ge=0, description="With transcript_id / plan_id: wrap width (0 = off).")] = 42,
    max_lines: Annotated[int, Field(ge=1, le=3)] = 2,
    channel: Channel = "auto",
    plan_id: Annotated[str | None, Field(description=(
        "Or the plan this timeline was imported from: captions follow its cut (see export_srt)."))] = None,
) -> dict[str, Any]:
    """Put subtitles on the current timeline's subtitle track, timed from the timeline start.

    With transcript_id alone the captions keep the source's own times: right only when
    the timeline plays the transcribed file from its start. After a rough cut pass plan_id,
    and the captions land where each line is spoken in the cut (a plan that does not know
    its transcript: add transcript_id, or export_srt(plan_id, transcript_ids) and pass the
    file). Reads the captions back and warns when they do not match the file.
    """
    written: dict[str, Any] = {}
    if plan_id or transcript_id:
        if srt_path:
            raise ValueError("Pass srt_path, or plan_id / transcript_id to have the file written; not both.")
        written = export_srt(transcript_id=transcript_id, plan_id=plan_id,
                             max_chars_per_line=max_chars_per_line, max_lines=max_lines)
        srt_path = written["path"]
    if not srt_path:
        raise ValueError("Pass srt_path, transcript_id or plan_id.")
    full = str(Path(srt_path).expanduser().resolve())
    if not Path(full).is_file():
        raise FileNotFoundError(full)
    expected = _srt_times(full)
    if not expected:
        raise ValueError(f"No SRT cues found in {full}.")
    out = control.perform(lua_lib.snippet_add_subtitles(full, replace),
                          lambda: _studio().add_subtitles(full, replace), channel)
    out["srt_path"] = full
    caption_warnings = list(written.get("warnings") or [])
    if out.get("channel") == "manual" or not out.get("ok"):
        if caption_warnings:
            out["warnings"] = caption_warnings + list(out.get("warnings") or [])
        return out
    cues = out.pop("cues")
    fps = out.get("fps") or 24
    warnings = caption_warnings
    if len(cues) != len(expected):
        warnings.append(f"the file has {len(expected)} cues but {len(cues)} landed on the track")
    elif abs(cues[0]["start_seconds"] - expected[0][0]) > 1.5 / fps:
        warnings.append(f"captions start at {cues[0]['start_seconds']:.2f}s, the file says "
                        f"{expected[0][0]:.2f}s; check with get_resolve_state or timeline_frames")
    out.update(cue_count=len(cues), first=cues[0], last=cues[-1], warnings=warnings)
    return out


@tool(EDIT_RESOLVE, title="Add a title")
def add_title(
    text: Annotated[str, Field(min_length=1, description="Title text (any language; line breaks allowed).")],
    start_seconds: Annotated[float, Field(ge=0, description="Where it starts, from the timeline start.")],
    duration_seconds: Annotated[float, Field(gt=0, le=600)] = 3.0,
    track: Annotated[int | None, Field(ge=2, description=(
        "Video track for the title; omit to use the lowest free track above V1 (added if needed)."))] = None,
    size: Annotated[float | None, Field(gt=0, le=1, description="Text height as a fraction of the frame (Text+ default 0.08).")] = None,
    position: Annotated[list[float] | None, Field(min_length=2, max_length=2, description=(
        "[x, y] in 0-1, origin bottom-left; [0.5, 0.15] = lower third. Default centre."))] = None,
    color: Annotated[list[float] | None, Field(min_length=3, max_length=3, description="[r, g, b], each 0-1.")] = None,
    font: Annotated[str | None, Field(description=(
        "Font family name as Fusion lists it, e.g. 'Heiti TC' for Traditional Chinese on macOS. "
        "The default font has no CJK glyphs, so set one for Chinese/Japanese/Korean text. "
        "An unknown family is refused with similar_fonts."))] = None,
    style: Annotated[str | None, Field(description=(
        "Style of that family, e.g. 'Medium' or 'W3'. Default: one the family has "
        "(Regular, Medium, ... in that order). An unknown style is refused with the family's styles."))] = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Overlay a Text+ title on the current timeline without moving or cutting anything else.

    Resolve's own InsertFusionTitleIntoTimeline is an insert edit on V1 (it splits and
    ripples the edit), so the title is built aside and placed on a free track above V1.
    It lands in the media pool's "Titles" bin as a compound clip the user can open to
    restyle. Check it with timeline_frames. Needs the bridge (or paste-able Lua).
    """
    return control.perform(
        lua_lib.snippet_add_title(text, start_seconds, duration_seconds, track, size,
                                  tuple(position) if position else None,
                                  tuple(color) if color else None, font, style),
        None, channel, timeout=60.0)


@tool(EDIT_RESOLVE, title="Checkpoints and project backup")
def checkpoint(
    action: Annotated[Literal["save", "list", "restore", "export_project"], Field(description=(
        "save: copy the current timeline as a checkpoint (before risky edits). list: the "
        "checkpoints. restore: continue on a fresh copy of a checkpoint. export_project: write "
        "the whole project as a .drp backup file."))] = "save",
    label: Annotated[str | None, Field(description="save: checkpoint label; default date and time.")] = None,
    name: Annotated[str | None, Field(description="restore: the checkpoint's timeline name from list.")] = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Undo for agents: Resolve's scripting API has no undo, so checkpoint before big changes.

    Checkpoints are timeline copies named "<timeline> · checkpoint <label>" in a
    "Checkpoints" bin; your current timeline stays current. restore never overwrites:
    it makes a "(restored)" copy of the checkpoint and switches to it.
    """
    import datetime as _dt
    stamp = _dt.datetime.now()
    backup = None
    if action == "save" and not label:
        label = stamp.strftime("%Y-%m-%d %H:%M:%S")
    if action == "export_project":
        backup = str(state_dir("backups") / f"project_{stamp.strftime('%Y%m%d-%H%M%S')}.drp")
    out = control.perform(lua_lib.snippet_checkpoint(action, label, name, backup), None, channel,
                          timeout=120.0)
    if action == "export_project" and out.get("channel") != "manual":
        f = Path(backup)
        out["size_bytes"] = f.stat().st_size if f.is_file() else 0
        out["ok"] = bool(out.get("ok")) and out["size_bytes"] > 0
        if not out["ok"]:
            out["error"] = out.get("error") or "Resolve reported success but no backup file was written."
    return out


def _mean_rgb(path: str) -> list[float] | None:
    """Average colour of an image, 0-255 per channel (ffmpeg area downscale to 1x1)."""
    try:
        proc = subprocess.run([frame_grab._ffmpeg_bin(), "-loglevel", "error", "-i", path, "-vf",
                               "scale=1:1:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                              capture_output=True, timeout=60)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    return [float(b) for b in proc.stdout[:3]] if proc.returncode == 0 and len(proc.stdout) >= 3 else None


RGB = Annotated[list[float] | float | None, Field(description="One number for all channels, or [r, g, b].")]


@tool(EDIT_RESOLVE, title="Grade clips (CDL)")
def grade_clips(
    track: Annotated[int, Field(ge=1)] = 1,
    indices: Annotated[list[int] | None, Field(description=(
        "Clips as get_resolve_state(include_clips=true) lists them; omit for every clip."))] = None,
    slope: RGB = None,
    offset: RGB = None,
    power: RGB = None,
    saturation: Annotated[float | None, Field(ge=0, le=4, description="1 = unchanged.")] = None,
    node: Annotated[int, Field(ge=1, description="Color node that receives the CDL.")] = 1,
    copy_from: Annotated[int | None, Field(ge=0, description=(
        "Instead of numbers: copy this clip's grade (same track) onto the others."))] = None,
    verify: Annotated[bool, Field(description=(
        "Export each clip's middle frame before and after and report the colour change."))] = True,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Primary colour by numbers (ASC CDL: slope = gain, offset = lift, power = gamma,
    saturation) on one colour node, or copy one clip's grade to others ("match these").

    Warmer: slope [1.06, 1, 0.94]; more contrast: power 1.1; punchier: saturation 1.15;
    a little brighter: offset 0.02. Resolve cannot read a CDL back, so `verify` compares
    the actual frames: `visible_change` false means nothing happened on screen.
    """
    if copy_from is None and all(v is None for v in (slope, offset, power, saturation)):
        raise ValueError("Give slope / offset / power / saturation, or copy_from.")
    with tempfile.TemporaryDirectory(prefix="grade-", dir=state_dir("stills")) as tmp:
        out = control.perform(
            lua_lib.snippet_grade_clips(track, indices, slope, offset, power, saturation, node,
                                        copy_from, tmp if verify else None),
            None, channel, timeout=60.0)
        if out.get("channel") == "manual" or not out.get("ok"):
            return out
        for row in out.get("clips", []):
            b = _mean_rgb(row.pop("before")) if row.get("before") else None
            a = _mean_rgb(row.pop("after")) if row.get("after") else None
            if b and a:
                row["mean_rgb_before"], row["mean_rgb_after"] = [round(x, 1) for x in b], [round(x, 1) for x in a]
                row["visible_change"] = max(abs(x - y) for x, y in zip(a, b)) >= 0.5
            elif verify:
                row["visible_change"] = None
                row["note"] = "could not export frames to verify; check with timeline_frames"
    rows = out.get("clips", [])
    out["applied"] = sum(1 for r in rows if r.get("applied"))
    if verify and rows and not any(r.get("visible_change") for r in rows):
        out["warning"] = ("No clip looks different after the grade: it may already have had these "
                          "values, or the node index is wrong. Check with timeline_frames.")
    return out


@tool(EDIT_RESOLVE, title="Make a format version (9:16, 1:1, 4:5)")
def make_format_version(
    aspect: Annotated[Literal[tuple(lua_lib.ASPECTS)], Field(description=(  # type: ignore[valid-type]
        "9:16 Shorts/Reels/TikTok (1080x1920), 4:5 feed (1080x1350), 1:1 (1080x1080), 16:9."))] = "9:16",
    name: Annotated[str | None, Field(description="New timeline name; default '<timeline> 9x16'.")] = None,
    fill: Annotated[bool, Field(description=(
        "True: scale clips to fill the frame (centre crop). False: fit (letterbox)."))] = True,
    width: Annotated[int | None, Field(ge=16, le=16384, description="Custom size instead of the aspect preset.")] = None,
    height: Annotated[int | None, Field(ge=16, le=16384)] = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Copy the current timeline at another frame size and switch to the copy; the original
    is untouched.

    Clips are centre-cropped to fill (fill=True). Then: look with timeline_frames, move
    subjects into frame per clip with set_clip_properties (Pan / ZoomX / ZoomY), redo titles
    listed in `recheck_graphics` with add_title (they were laid out for the old frame), and
    render with queue_render(format='mp4', codec='H264', width=..., height=...).
    """
    return control.perform(lua_lib.snippet_make_format_version(aspect, name, fill, width, height),
                           None, channel, timeout=60.0)


@tool(EDIT_RESOLVE, title="Timelines: list, open, rename, duplicate")
def manage_timelines(
    action: Literal["list", "open", "rename", "duplicate"] = "list",
    name: Annotated[str | None, Field(description="The timeline to act on (from list).")] = None,
    new_name: Annotated[str | None, Field(description="rename / duplicate: the new name.")] = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Every timeline in the open project (duration, fps, size, which is current), and
    switching to, renaming or duplicating one. duplicate keeps you on the current timeline."""
    return control.perform(lua_lib.snippet_manage_timelines(action, name, new_name), None, channel)


@tool(EDIT_RESOLVE, title="Markers: list, delete, update")
def edit_markers(
    action: Literal["list", "delete", "update"] = "list",
    frames: Annotated[list[int] | None, Field(description="Marker positions, frames from the timeline start.")] = None,
    seconds: Annotated[list[float] | None, Field(description="...or seconds from the timeline start.")] = None,
    color: Annotated[str | None, Field(description="Only markers of this color.")] = None,
    custom_data_prefix: Annotated[str | None, Field(description="Only markers whose customData starts with this.")] = None,
    name: Annotated[str | None, Field(description="update: new name.")] = None,
    note: Annotated[str | None, Field(description="update: new note.")] = None,
    new_color: Annotated[str | None, Field(description="update: new color.")] = None,
    channel: Channel = "auto",
) -> dict[str, Any]:
    """Read and tidy the current timeline's markers (add them with add_markers).

    Selectors combine (frames/seconds AND color AND prefix). delete needs a selector, so
    it never clears everything by accident. update rewrites name / note / color in place.
    """
    return control.perform(lua_lib.snippet_edit_markers(action, frames, seconds, color, custom_data_prefix,
                                                        name, note, new_color), None, channel)


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
# Resources: read-only context an agent can load without a tool call
# ────────────────────────────────────────────────────────────────────────────

_LIVE = ["studio", "bridge", "manual"]
# How each tool reaches Resolve, where the `channel` parameter does not say it all.
# Tools with a `channel` parameter default to _LIVE; tools without one need no Resolve.
_TOOL_CHANNELS: dict[str, list[str]] = {
    "run_lua": ["bridge"],
    "timeline_frames": ["studio", "bridge"],
    "add_title": ["bridge", "manual"],  # Lua only, no Studio Python path
    "checkpoint": ["bridge", "manual"],
    "grade_clips": ["bridge", "manual"],
    "make_format_version": ["bridge", "manual"],
    "manage_timelines": ["bridge", "manual"],
    "edit_markers": ["bridge", "manual"],
    "open_page": _LIVE + ["macos_ui"],
    "screenshot_resolve": ["macos_ui"],
    "send_keystroke": ["macos_ui"],
    "menu_pick": ["macos_ui"],
    "open_ai_feature": ["macos_ui"],
}
_MACOS_ONLY = {"screenshot_resolve", "send_keystroke", "menu_pick", "open_ai_feature"}

_CHANNEL_NOTES = {
    "studio": "Resolve Studio's Python scripting API: direct control. Studio only.",
    "bridge": "The resolve-pilot Lua bridge running inside Resolve (Free and Studio). The user "
              "starts it once per session (setup_bridge installs it into Workspace > Scripts).",
    "manual": "Nothing live: the tool returns lua_code + instructions for the user to paste into "
              "Workspace > Console. Nothing changes until they do.",
    "macos_ui": "macOS Accessibility / screen capture of the Resolve window. macOS only; needs "
                "the permissions below.",
}

_EDITION_NOTES = {
    "free": "No external scripting API. resolve-pilot reaches Free through the Lua bridge "
            "(bridge channel) or paste-able Lua (manual channel). Studio-only features (some AI "
            "tools) may answer with an upgrade dialog, which blocks script calls until closed.",
    "studio": "The Python scripting API (studio channel) is used first; run_lua still needs the "
              "bridge.",
    "resolve_21_1_free": "From Resolve 21.1, Python scripting (and Resolve's own MCP server) are "
                         "Studio-only, Free's Console Lua loses io, os.execute, package and debug, "
                         "and Workspace > Scripts entries do nothing. Reported per patch release: "
                         "on 21.1.1 Free the bridge starts when setup_bridge's console_command "
                         "(dofile) is pasted into Workspace > Console; on 21.1.2 Free dofile, io "
                         "and ffi are gone too and the bridge cannot start. Use Resolve 20.3.x "
                         "Free for live control (the bridge is verified there); FCPXML export and "
                         "the analysis tools work on any version.",
    "verified": "Bridge tools verified live on Resolve 20.3 Free (macOS); see resolve://traps for "
                "what each measurement turned up.",
}

_PLATFORM_NOTES = {
    "macos_only_tools": sorted(_MACOS_ONLY),
    "windows_linux": "No UI automation; the bridge covers live control on every OS.",
    "macos_permissions": "Accessibility (send_keystroke, menu_pick, open_ai_feature, page "
                         "shortcuts) and Screen Recording (screenshot_resolve). macOS checks them "
                         "against the app responsible for this server: under Claude Desktop that is "
                         "the Python interpreter (Python.app), not Claude. The tools' errors name "
                         "the app to allow.",
}


def capabilities() -> dict[str, Any]:
    """Which tool works on which channel, plus edition and OS notes (resolve://capabilities)."""

    tools: dict[str, Any] = {}
    for name, (fn, ann) in _TOOLS.items():
        hints = ann.model_dump(by_alias=True)
        if name in _TOOL_CHANNELS:
            channels = _TOOL_CHANNELS[name]
        else:
            channels = list(_LIVE) if "channel" in inspect.signature(fn).parameters else []
        tools[name] = {
            "channels": channels,
            "needs_resolve": bool(channels),
            "macos_only": name in _MACOS_ONLY,
            "read_only": bool(hints.get("readOnlyHint")),
            "destructive": bool(hints.get("destructiveHint")),
            "verified_live": verified.entry(name),
        }
    return {
        "server_version": __version__,
        "platform": platform.system(),
        "channel_order": "studio, then bridge, then manual (channel='auto'); "
                         "resolve_pilot_status says which one is live",
        "channels": _CHANNEL_NOTES,
        "editions": _EDITION_NOTES,
        "platforms": _PLATFORM_NOTES,
        "tools": tools,
        "verification": "verified_live says where a tool was run in a real Resolve (version, "
                        "edition, OS, channel) and what that covered; null means not yet. "
                        + verified.UNTESTED,
        "known_traps": "resolve://traps",
    }


STATE_TIMEOUT_SECONDS = 5.0  # the bridge / Studio call itself
STATE_DEADLINE_SECONDS = 8.0  # the whole read, channel probing included


def _state_snapshot(timeout: float = STATE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """get_resolve_state() without clips, or why there is nothing live; bounded by `timeout`."""
    not_connected = {
        "connected": False,
        "next_steps": [
            "Call resolve_pilot_status for the full picture.",
            "For live control (works in Resolve Free): setup_bridge(action='install'), then ask the "
            "user to click Workspace > Scripts > ResolvePilotBridge in Resolve.",
            "Without it, Resolve tools return Lua for the user to paste into Workspace > Console.",
        ],
    }
    try:
        channel = control.choose_channel("auto")
        if channel == "manual":
            return {**not_connected, "channel": "manual", "bridge": bridge.status().detail}
        args = dict(include_clips=False, track_type="video", track_index=1, include_markers=False)
        out = control.perform(lua_lib.snippet_get_state(**args),
                              lambda: _studio().resolve_state(**args), channel, timeout=timeout)
        return {"connected": True, **out}
    except Exception as e:
        return {**not_connected, "error": _explain(e)}


async def _bounded_state() -> str:
    import asyncio

    try:
        data = await asyncio.wait_for(asyncio.to_thread(_state_snapshot), STATE_DEADLINE_SECONDS)
    except asyncio.TimeoutError:
        data = {"connected": False,
                "error": f"Resolve did not answer within {STATE_DEADLINE_SECONDS:g}s. "
                         + bridge.MODAL_HINT,
                "next_steps": ["Call resolve_pilot_status."]}
    return json.dumps(data, ensure_ascii=False, indent=2)


def _register_resources() -> None:
    mcp.resource("resolve://traps", name="known-traps", title="Known Resolve traps",
                 description="Resolve behaviour that surprises scripts (measured or reported) and "
                             "what resolve-pilot does about each. Read before risky edits.",
                 mime_type="text/markdown")(traps.to_markdown)
    mcp.resource("resolve://capabilities", name="capabilities", title="Capability matrix",
                 description="Which tools work on which channel (studio / bridge / manual / "
                             "macos_ui), Free vs Studio and Resolve 21.1+ notes, OS and "
                             "permission notes.",
                 mime_type="application/json")(
        lambda: json.dumps(capabilities(), ensure_ascii=False, indent=2))
    mcp.resource("resolve://state", name="resolve-state", title="Resolve state (live)",
                 description="The open project and current timeline (get_resolve_state without "
                             "clips) when a channel is live; otherwise how to connect. Answers "
                             "within a few seconds.",
                 mime_type="application/json")(_bounded_state)

    for wf in list_workflows():
        if wf["name"] == "traps":  # already resolve://traps
            continue

        short = wf["name"].removeprefix("resolve-")

        def make(name: str):
            return lambda: get_workflow(name)
        mcp.resource(f"resolve://guide/{short}", name=f"guide-{short}",
                     title=f"Guide: {short}", description=wf["description"],
                     mime_type="text/markdown")(make(wf["name"]))

    def guide(name: str) -> str:
        return get_workflow(name)
    mcp.resource("resolve://guide/{name}", name="guide",
                 title="Workflow guide",
                 description="A workflow guide by name (short names work: rough-cut, subtitle, "
                             "live-control, traps, ...).",
                 mime_type="text/markdown")(guide)


_register_resources()


# ────────────────────────────────────────────────────────────────────────────
# Prompts: each workflow guide as a ready-to-use prompt
# ────────────────────────────────────────────────────────────────────────────

def _register_prompts() -> None:
    for wf in list_workflows():
        if wf["name"] == "traps":  # reference, not a workflow; served as resolve://traps
            continue
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
