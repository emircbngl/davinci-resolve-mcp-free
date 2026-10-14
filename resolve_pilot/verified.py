"""Which tools have been run against a real DaVinci Resolve, where, and how.

The test suite runs the bridge against a mock Resolve, so a green suite says
nothing about how Resolve itself behaves. This ledger records what was run live:
the Resolve version and edition, the OS, the channel that reached Resolve, and
what the run covered. ``resolve://capabilities`` and the matrix in README.md are
built from it. Add a tool to ``LIVE`` only after running it in Resolve, and put
every other tool that needs Resolve in ``NOT_YET`` (a test enforces this, so a
new tool cannot slip into the matrix as silently "unknown").

Regenerate the README matrix with ``python -m resolve_pilot.verified README.md``.
"""
from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LiveCheck:
    resolve: str   # version and edition, e.g. "20.3 Free"
    os: str
    channel: str   # what reached Resolve: "bridge", "macos_ui", or "file import"
    scope: str = ""  # what the run covered, or what it left out


def _r20(channel: str, scope: str = "") -> LiveCheck:
    return LiveCheck(resolve="20.3 Free", os="macOS", channel=channel, scope=scope)


LIVE: dict[str, LiveCheck] = {
    "resolve_pilot_status": _r20("bridge", "channel switches from manual to bridge"),
    "setup_bridge": _r20("bridge", "install into Workspace › Scripts, status"),
    "run_lua": _r20("bridge", "results, print output, errors; `return _G` does not hang"),
    "get_resolve_state": _r20("bridge"),
    "import_timeline": _r20("bridge", "FCPXML from export_fcpxml; paths with spaces, # and non-ASCII"),
    "add_markers": _r20("bridge", "seconds and frames on a 24 fps timeline; a second marker on "
                                  "the same frame is skipped. 23.976 / 29.97 DF not yet"),
    "add_quality_markers": _r20("bridge", "re-runs replace only their own markers"),
    "set_clip_color": _r20("bridge", "that run found transitions being colored too; fixed since, "
                                      "not re-run yet"),
    "apply_lut": _r20("bridge", "that run found LUTs outside Resolve's LUT folder rejected; "
                            "they are copied in since, not re-run yet"),
    "grade_clips": _r20("bridge", "CDL grade, copy, reset; change measured on exported frames"),
    "queue_render": _r20("bridge", "known preset queued; an unknown one lists the presets"),
    "render_status": _r20("bridge"),
    "open_page": _r20("bridge", "all seven pages"),
    "timeline_frames": _r20("bridge", "playhead and page put back"),
    "add_subtitles": _r20("bridge", "timelines starting at 0 and at 01:00:00:00"),
    "add_title": _r20("bridge", "overlay above V1, the edit left alone"),
    "checkpoint": _r20("bridge", "save, list, restore, .drp backup"),
    "import_media": _r20("bridge", "bins, files already in the bin reused"),
    "append_clips": _r20("bridge", "new timeline, exact source ranges, 23.976 into 24, audio on "
                                   "A2 at a record time"),
    "remove_clips": _r20("bridge", "ripple delete"),
    "set_clip_properties": _r20("bridge", "zoom, pan, opacity, flip, scaling, clip enable; read "
                                          "back and checked with timeline_frames"),
    "set_tracks": _r20("bridge", "rename, lock, add"),
    "export_timeline": _r20("bridge", "FCPXML, EDL, OTIO, DRT, AAF, CSV"),
    "screenshot_resolve": _r20("macos_ui"),
    "menu_pick": _r20("macos_ui", "Workspace › Scripts › ResolvePilotBridge"),
    "export_fcpxml": _r20("file import", "imported with import_timeline: clips, dissolves, the "
                                         "music bed on its own audio track"),
    "export_srt": _r20("file import", "non-ASCII captions"),
}

# Tools that need Resolve but have not been run in it yet, with what to check.
NOT_YET: dict[str, str] = {
    "send_keystroke": "keystrokes reach Resolve only when it is frontmost",
    "open_ai_feature": "Studio-only features in Free answer ok: false and leave no dialog open",
}

UNTESTED = ("Nothing has been run through Resolve Studio's Python API (the studio channel), on "
            "Windows or Linux, or on Resolve 21.x yet; the bridge's Resolve 21.1+ Free file "
            "access is only emulated in tests.")


def entry(name: str) -> dict[str, Any] | None:
    """The live check for a tool as plain data, or None."""
    check = LIVE.get(name)
    return asdict(check) if check else None


_CHANNEL_LABEL = {"studio": "Studio API", "bridge": "bridge", "manual": "paste Lua",
                  "macos_ui": "macOS UI"}

# For tools that reach Resolve some other way than a channel.
_VIA = {"resolve_pilot_status": "bridge heartbeat, Resolve's process",
        "setup_bridge": "Resolve's Scripts folder",
        "export_fcpxml": "a file you import", "export_srt": "a file you import"}

START, END = "<!-- verified-matrix:start -->", "<!-- verified-matrix:end -->"


def markdown_table(tools: dict[str, dict[str, Any]]) -> str:
    """The README matrix, from resolve://capabilities' per-tool entries."""
    rows = ["| Tool | Reaches Resolve through | Run in real Resolve | Covered |",
            "|---|---|---|---|"]
    for name, info in tools.items():
        if not info.get("needs_resolve") and name not in LIVE:
            continue
        via = " · ".join(_CHANNEL_LABEL.get(c, c) for c in info.get("channels", [])) or _VIA.get(name, "")
        check = LIVE.get(name)
        if check:
            ran = f"✅ {check.resolve}, {check.os} ({_CHANNEL_LABEL.get(check.channel, check.channel)})"
            covered = check.scope
        else:
            ran, covered = "not yet", NOT_YET.get(name, "")
        rows.append(f"| `{name}` | {via} | {ran} | {covered} |")
    return "\n".join(rows)


def readme_block(tools: dict[str, dict[str, Any]]) -> str:
    return f"{START}\n{markdown_table(tools)}\n\n{UNTESTED}\n{END}"


def update_readme(path: Path, tools: dict[str, dict[str, Any]]) -> bool:
    """Rewrite the matrix between the markers; returns whether the file changed."""
    text = path.read_text(encoding="utf-8")
    start, end = text.index(START), text.index(END) + len(END)
    new = text[:start] + readme_block(tools) + text[end:]
    if new != text:
        path.write_text(new, encoding="utf-8")
    return new != text


if __name__ == "__main__":
    from resolve_pilot import mcp_server

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "README.md")
    changed = update_readme(target, mcp_server.capabilities()["tools"])
    print(f"{target}: {'updated' if changed else 'already up to date'}")
