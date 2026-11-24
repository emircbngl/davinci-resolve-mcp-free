"""Known traps: Resolve behaviour that surprises scripts, and what resolve-pilot does about it.

One source of truth, served three ways: the ``resolve://traps`` MCP resource,
``get_workflow_guide("traps")``, and one-line ``known_trap`` hints pushed into
results where the trap is certain to apply (bridge timeouts, run_lua code that
calls an affected API method).

``source`` says how we know: ``measured`` means resolve-pilot reproduced it on a
real Resolve (the version is in ``measured_on``); ``reported`` means other Resolve
scripting projects describe it and resolve-pilot has not reproduced it yet.
Add a trap only after measuring it, or mark it ``reported``.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Trap:
    id: str
    title: str
    symptom: str
    cause: str
    handling: str  # what resolve-pilot does about it
    source: Literal["measured", "reported"]
    measured_on: str  # Resolve version / edition, or "" when only reported
    tools: tuple[str, ...] = ()
    lua_methods: tuple[str, ...] = field(default=(), repr=False)  # run_lua code calling these gets the hint

    def hint(self) -> str:
        return f"Known trap ({self.id}): {self.title}. {self.handling}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tools"] = list(self.tools)
        d.pop("lua_methods")
        return d


TRAPS: tuple[Trap, ...] = (
    Trap(
        id="modal-dialog-blocks-api",
        title="A modal dialog in Resolve blocks the scripting API",
        symptom="Bridge requests time out or the bridge heartbeat goes stale while Resolve "
                "looks idle.",
        cause="While a modal dialog (an upgrade prompt for a Studio-only feature, a save or "
              "import prompt, an error box) is open, Resolve does not run script calls.",
        handling="Bridge timeouts say a modal dialog may be blocking the scripting API: check "
                 "screenshot_resolve (macOS) or ask the user to close any open dialog in Resolve.",
        source="reported",
        measured_on="",
        tools=("run_lua", "get_resolve_state", "setup_bridge"),
    ),
    Trap(
        id="subtitle-append-frame-range-crash",
        title="Appending a subtitle item with a frame range crashes Resolve",
        symptom="Resolve quits while appending an .srt item to the timeline.",
        cause="AppendToTimeline with a subtitle media pool item plus startFrame / endFrame (or a "
              "mediaType) crashes Resolve.",
        handling="add_subtitles appends subtitle items without a range; append_clips refuses "
                 "subtitle files. In run_lua, append subtitle items as a bare item only.",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS), reproduced twice",
        tools=("add_subtitles", "append_clips", "run_lua"),
        lua_methods=("AppendToTimeline",),
    ),
    Trap(
        id="subtitles-land-at-playhead",
        title="Subtitles are appended at the playhead, not at the timeline start",
        symptom="Captions land seconds late (by wherever the playhead was).",
        cause="Appending a subtitle item places it at the current playhead position.",
        handling="add_subtitles parks the playhead on the first frame for the append, puts it "
                 "back afterwards, and reads the captions back to check their times.",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS)",
        tools=("add_subtitles", "run_lua"),
    ),
    Trap(
        id="setlut-needs-lut-folder",
        title="SetLUT only accepts LUTs inside Resolve's LUT folder",
        symptom="SetLUT returns false and nothing changes.",
        cause="Resolve rejects LUT paths outside its LUT folder.",
        handling="apply_lut copies the LUT into <LUT folder>/resolve-pilot/, refreshes the LUT "
                 "list, and returns ok: false if Resolve still rejects every clip.",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS)",
        tools=("apply_lut", "run_lua"),
        lua_methods=("SetLUT",),
    ),
    Trap(
        id="transitions-listed-as-items",
        title="GetItemListInTrack returns transitions as timeline items",
        symptom="Clip counts include dissolves; clip colors or LUTs are applied to transitions.",
        cause="Transitions (e.g. Cross Dissolves from an FCPXML) come back as items without a "
              "media pool item.",
        handling="Every resolve-pilot action filters transitions out; get_resolve_state reports "
                 "them as transition_count.",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS)",
        tools=("get_resolve_state", "set_clip_color", "apply_lut", "set_clip_properties",
               "remove_clips", "run_lua"),
        lua_methods=("GetItemListInTrack",),
    ),
    Trap(
        id="api-tables-carry-flags",
        title='Tables from the Resolve API carry a "__flags" key',
        symptom='Lists arrive as {"1": ..., "__flags": ...} objects instead of arrays.',
        cause="Resolve tags the tables it returns with an internal __flags entry.",
        handling="The bridge skips __flags and encodes such lists as JSON arrays.",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS)",
        tools=("run_lua",),
    ),
    Trap(
        id="script-env-vs-loadstring-globals",
        title="Resolve runs scripts in their own environment; loadstring compiles against the "
              "real globals",
        symptom="Code run by the bridge could not see the rp helpers and its print() output was "
                "lost.",
        cause="Script-environment globals are invisible to chunks compiled with loadstring.",
        handling="Each run_lua request gets an environment with rp and a capturing print; "
                 "globals it sets still persist between requests, in the bridge's own table "
                 "rather than Resolve's real globals.",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS)",
        tools=("run_lua",),
    ),
    Trap(
        id="bridge-started-from-project-manager",
        title="A bridge started from the Project Manager has no usable Resolve object",
        symptom="Every API call fails with a cryptic error.",
        cause="Started while no project is open, the script gets a Resolve object that cannot "
              "do anything.",
        handling="Bridge errors then say to open a project and start the bridge again.",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS)",
        tools=("setup_bridge", "run_lua"),
    ),
    Trap(
        id="dissolve-into-silent-clip-makes-compound",
        title="A dissolve into a clip without audio makes Resolve wrap it in a compound clip",
        symptom="After an FCPXML import, a clip shows up as a compound clip.",
        cause="Resolve's FCPXML import wraps a silent clip that has a dissolve leading into it.",
        handling="Plan validation warns about it; set transition_out_seconds=0 on the clip "
                 "before it for a plain clip.",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS)",
        tools=("create_plan", "edit_plan", "export_fcpxml", "import_timeline"),
    ),
    Trap(
        id="resolve-21-1-free-lua-sandbox",
        title="Resolve 21.1 Free restricts Console Lua; from 21.1.2 the bridge cannot start "
              "and the Console window takes its place (macOS)",
        symptom="On 21.1 Free, scripts that read or write files or require modules fail and "
                "Workspace > Scripts entries do nothing. On 21.1.2 Free, dofile is nil too and "
                "the pasted bridge stops with \"this Lua has neither io nor ffi\". On 21.2.0 Free "
                "clicking the bridge in Workspace > Scripts starts no script process at all.",
        cause="Resolve 21 takes file access away from scripting in the Free edition, step by "
              "step: io, os.execute, package and debug from 21.1; dofile and ffi from 21.1.2 "
              "(reported on 21.1.1 and 21.1.2 Free, macOS). Measured in the Console of 21.2.0 "
              "Free (macOS): io, ffi, dofile, require, package, os.execute, os.remove, os.rename "
              "and bmd.readfile / writefile / readdir / createdir / getclipboard / setclipboard / "
              "executebg are nil; resolve, fusion, loadstring, os.getenv, bmd.wait, "
              "bmd.fileexists, bmd.readstring and bmd.writestring are there. That is a removal "
              "on purpose, so resolve-pilot does not look for a way around it.",
        handling="The bridge falls back to LuaJIT's ffi for its files when io is missing, and "
                 "setup_bridge returns a console_command (dofile) to paste into the Console; that "
                 "is reported working on 21.1.1. On 21.1.2+ Free resolve_pilot_status and "
                 "setup_bridge say the bridge cannot start. Lua typed at the Console prompt "
                 "still runs with the full Resolve API there, and that is all the console channel "
                 "uses: on macOS the server puts a tool's Lua into the Console's input through "
                 "Accessibility, presses Return and reads the printed answer (measured on 21.2.0 "
                 "Free: a 198-clip import checked against its plan, LUT, markers, subtitles, "
                 "stills). Nothing is written or read by Lua inside Resolve. The Console keeps "
                 "its last 131,072 characters and drops a longer line whole, so answers come in "
                 "parts of 60,000. Resolve stays in the background: System Events does not list "
                 "the Console then (the panel hides), but Accessibility hands it out as Resolve's "
                 "focused window and a Return sent to Resolve's process runs the line. Resolve "
                 "comes to the front only when the Console cannot be reached that way (closed, "
                 "a dialog open; RESOLVE_PILOT_CONSOLE=front asks for it always) and then stays "
                 "there for RESOLVE_PILOT_CONSOLE_HOLD seconds after a call (default 4). On "
                 "Windows and Linux the manual channel is left: give the user lua_code and the "
                 "instructions.",
        source="measured",
        measured_on="21.2.0 Free, macOS (21.1.x: reported)",
        tools=("setup_bridge", "run_lua", "resolve_pilot_status"),
    ),
    Trap(
        id="claude-desktop-python-app-permissions",
        title="Under Claude Desktop, Python.app is the app that needs macOS permissions",
        symptom='Screenshots or UI automation stay blocked after "allow Claude" in System '
                "Settings.",
        cause="Claude Desktop starts MCP servers through a disclaimer helper that makes the "
              "Python interpreter responsible for its own privacy permissions.",
        handling="Permission hints name the app macOS actually checks (often Python.app).",
        source="measured",
        measured_on="macOS with Claude Desktop",
        tools=("screenshot_resolve", "send_keystroke", "menu_pick", "open_ai_feature", "open_page"),
    ),
    Trap(
        id="duplicate-timeline-becomes-current",
        title="DuplicateTimeline makes the copy the current timeline",
        symptom="After duplicating as a backup, later edits land on the copy.",
        cause="DuplicateTimeline silently switches the current timeline to the duplicate.",
        handling="checkpoint(action='save') switches back to the original after copying. In "
                 "run_lua, remember the original and restore it with "
                 "project:SetCurrentTimeline(original).",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS)",
        tools=("checkpoint", "run_lua"),
        lua_methods=("DuplicateTimeline",),
    ),
    Trap(
        id="fusion-title-insert-edit",
        title="InsertFusionTitleIntoTimeline is an insert edit on V1 at the playhead",
        symptom="Adding a title splits the clip under the playhead and ripples everything after "
                "it to the right; with V1 locked nothing is inserted.",
        cause="The call always inserts on V1 at the playhead; locking or disabling V1 does not "
              "redirect it to another track.",
        handling="add_title builds the title on a scratch timeline, wraps it as a compound clip "
                 "and appends it on a free track above V1, leaving the edit alone.",
        source="measured",
        measured_on="Resolve 20.3 Free (macOS)",
        tools=("add_title", "run_lua"),
        lua_methods=("InsertFusionTitleIntoTimeline",),
    ),
    Trap(
        id="textplus-font-keeps-style",
        title="Text+ keeps its Style when only Font changes",
        symptom="A title with a new font looks fine in the timeline, but every render fails with "
                "\"The Fusion composition ... could not be processed\"; ResolveDebug.txt says "
                "\"Font Not Found: Heiti TC Semibold\".",
        cause="Text+ starts as Open Sans Semibold. Setting Font alone leaves Style at Semibold, and "
              "a family without that style (Heiti TC has only Light and Medium) cannot be loaded. "
              "The default font has no CJK glyphs, so Chinese / Japanese / Korean titles need a "
              "font. Some system fonts (PingFang on macOS) are not visible to Fusion at all.",
        handling="add_title checks font and style against Fusion's FontManager:GetFontList() "
                 "before building anything, picks a style the family has when none is given, and "
                 "refuses an unknown family (with similar_fonts) or style (with the family's styles).",
        source="measured",
        measured_on="Resolve 21.1.1 Free (macOS)",
        tools=("add_title", "run_lua"),
    ),
    Trap(
        id="deleteclips-needs-edit-page",
        title="DeleteClips only works on the Edit page",
        symptom="DeleteClips returns false or deletes nothing on other pages.",
        cause="The call is page-gated.",
        handling="remove_clips switches to the Edit page for the delete, switches back, and "
                 "counts the clips that are actually gone.",
        source="measured",
        measured_on="Resolve 21.0.4 Free (Python)",
        tools=("remove_clips", "run_lua"),
        lua_methods=("DeleteClips",),
    ),
    Trap(
        id="append-endframe-exclusive",
        title="AppendToTimeline endFrame is exclusive and counts source frames",
        symptom="Clips come out one frame short or long, or the wrong length with mixed frame "
                "rates.",
        cause="startFrame / endFrame are frames of the source clip at its own frame rate, and "
              "endFrame is not included.",
        handling="append_clips converts source seconds with the clip's own fps, treats the out "
                 "point as exclusive, and warns when the landed length differs.",
        source="measured",
        measured_on="Resolve 21.0.4 Free (Python) and Resolve 20.3 Free (bridge)",
        tools=("append_clips", "run_lua"),
        lua_methods=("AppendToTimeline",),
    ),
)

_BY_ID = {t.id: t for t in TRAPS}


def get(trap_id: str) -> Trap:
    return _BY_ID[trap_id]


def hint(trap_id: str) -> str:
    return _BY_ID[trap_id].hint()


def hints_for_lua(code: str) -> list[str]:
    """One-line hints for the traps whose API methods ``code`` calls."""
    out = []
    for t in TRAPS:
        if any(re.search(rf"[:.]\s*{m}\s*\(", code) for m in t.lua_methods):
            out.append(t.hint())
    return out


def to_markdown() -> str:
    lines = [
        "# Known traps",
        "",
        "Resolve behaviour that surprises scripts, and what resolve-pilot does about it. "
        "Read this before risky edits and before writing run_lua code. *measured*: "
        "reproduced by resolve-pilot on a real Resolve; *reported*: described by other "
        "Resolve scripting projects, not yet reproduced here.",
    ]
    for t in TRAPS:
        where = f"measured on {t.measured_on}" if t.source == "measured" else "reported, not yet measured here"
        lines += [
            "",
            f"## {t.title}",
            f"`{t.id}` · {where}",
            "",
            f"- **Symptom:** {t.symptom}",
            f"- **Cause:** {t.cause}",
            f"- **resolve-pilot:** {t.handling}",
        ]
        if t.tools:
            lines.append("- **Tools:** " + ", ".join(f"`{n}`" for n in t.tools))
    return "\n".join(lines) + "\n"
