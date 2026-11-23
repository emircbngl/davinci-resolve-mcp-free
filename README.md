# DaVinci Resolve MCP — Works in DaVinci Resolve Free

**Claude-driven video editing that runs in [DaVinci Resolve Free](https://www.blackmagicdesign.com/products/davinciresolve) — the no-cost edition that Blackmagic ships at $0.** No Studio license required, no API key required.

> ℹ️ **What "free" means here:** DaVinci Resolve ships in two editions — **Free** (no cost) and **Studio** (~$295 one-time license). Studio exposes a Python scripting API for automation; Free does not — and since **Resolve 21.1, Python scripting (and Resolve's own MCP server) are Studio-only**, so MCP servers that drive Free through Python stop working there. This project gets Claude-driven editing working in **Resolve Free too**, through a Lua bridge that runs inside Resolve (verified on 20.3 Free; reported working from the Console on 21.1.1 Free). **On 21.1.2+ Free the bridge cannot start**: its Console Lua has no file access any more (measured on 21.2.0 Free). There, on macOS, the server uses Resolve's own Console window instead: it types each tool's Lua at the prompt and reads the printed answer (the `console` channel, measured on 21.2.0 Free; needs Accessibility permission and brings Resolve to the front for a moment on every call). Plus FCPXML and UI automation:

| Channel | What it does | Free | Studio |
|---|---|:---:|:---:|
| **Live Lua bridge** | Claude reads and changes the open project directly: import media, build and change timelines clip by clip, import timelines, add markers, color clips, apply LUTs, queue renders, run any Lua | ✅ | ✅ |
| **FCPXML export** | Build a rough cut outside Resolve, import via `File > Import > Timeline` (or automatically through the bridge) | ✅ | ✅ |
| **Lua snippets** | Ready-to-paste Console snippets when nothing live is connected | ✅ | ✅ |
| **UI automation** (macOS) | Page switching, menu items, Magic Mask & other AI tools via the Accessibility API; screenshots of the Resolve window | ✅ | ✅ |
| **Python scripting API** | Direct DaVinciResolveScript control | ❌ | ✅ |

Every Resolve action picks the best channel by itself — Studio API, then the live bridge, then paste-able Lua — and tells Claude which one it used.

If you only have the free Blackmagic download — this is for you.

## Installation

```bash
git clone https://github.com/emircbngl/davinci-resolve-mcp-free.git
cd davinci-resolve-mcp-free
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[transcribe,ui]"  # Apple Silicon
# pip install -e ".[transcribe-cpu]"   # Windows / Linux / Intel Mac (faster-whisper)
```

Requirements that should already be on the machine:
- Python 3.11+
- `ffmpeg` + `ffprobe` (`brew install ffmpeg` · `winget install ffmpeg` · `apt install ffmpeg`)
- DaVinci Resolve (Free or Studio) — the download from blackmagicdesign.com. The Mac App Store build is sandboxed and cannot talk to the bridge.
- macOS 12+ for the UI-automation tools; everything else also runs on Windows and Linux.

## Connecting Claude

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "resolve-pilot": {
      "command": "/path/to/davinci-resolve-mcp-free/.venv/bin/resolve-pilot"
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add resolve-pilot -- /path/to/davinci-resolve-mcp-free/.venv/bin/resolve-pilot
```

Restart the client. The `resolve-pilot` server should appear in the tool list.

## Live control in Resolve Free (one click per session)

Ask Claude to *"set up the Resolve bridge"*. It installs a small Lua script into Resolve's Scripts menu. Then, in Resolve:

**Workspace › Scripts › ResolvePilotBridge**

(Restart Resolve once if it isn't listed yet.) From now on Claude can see the open project and act on it — import the cut it just built, check that every clip landed, add markers, and so on. The bridge stops by itself after an hour without requests. No install wanted? Claude can also hand you the script to paste into **Workspace › Console** (Lua tab).

> 🔒 While the bridge runs, anything that can write to `~/.resolve-pilot/bridge` can run Lua inside Resolve — the same trust level as your user account. `run_lua` is marked destructive, so clients ask before using it; save your project before letting Claude make large changes.

## What Claude can do

After connecting, ask Claude things like:

- *"Probe this video and tell me its codec, fps, and how many scene cuts it has."*
- *"Transcribe `~/Footage/interview.mp4` and give me a 4-minute cut of her best answers — put it straight into Resolve."*
- *"Add Turkish subtitles to this talk."*
- *"Cut these clips to the beat of `song.mp3`, with the song underneath, and drop a marker on every beat."*
- *"Color-code V2 orange and apply `Kodak2383.cube` to V1."*
- *"What's on my current timeline? Give every source file its own clip color."*
- *"Make a vertical 9:16 rough cut from this 30-min talk."*
- *"Show me what's in this B-roll folder before we pick shots."* (Claude looks at a frame per scene)
- *"Find the dead air and black frames in this recording and mark them on the timeline."*
- *"Drop the drone shot from 0:12 to 0:17 on V2 at the 30-second mark."* (straight into the open timeline)

## Tools

| Group | Tools |
|---|---|
| Session | `resolve_pilot_status` · `get_workflow_guide` · `setup_bridge` (install, or `start`: Resolve and the bridge up as far as the machine allows) · `manage_projects` (list / open / create) |
| Analyze | `scan_folder` (a shoot's files in recording order, transcribed in the background) · `probe_media` · `detect_scenes` · `detect_beats` · `transcribe_audio` (cached, background jobs, word timings) · `scan_speech` (pauses, fillers, repeats, retakes to cut) · `analyze_quality` (silence, black, freeze, loud/quiet) |
| See | `grab_frames` · `scene_frames` · `read_video` (speech + frames) · `screenshot_resolve` (macOS) · `timeline_frames` (the edit as Resolve renders it, any OS) — images Claude looks at |
| Plan | `plan_from_transcript` (cuts placed in the real pauses between words; `drop_words`, `max_pause_seconds`; `append_to` cuts several files into one plan) · `plan_from_scenes` · `create_plan` · `get_plan` · `edit_plan` |
| Export | `export_fcpxml` · `export_srt` (a transcript's own times, or with `plan_id` captions that follow the rough cut) · `render_preview` (the plan as a small MP4 without Resolve, checked automatically: cuts in sound, level jumps, flash clips, dead stretches) |
| Resolve (auto-routed) | `get_resolve_state` · `import_timeline` (with `plan_id`: the timeline is compared with the plan) · `add_markers` · `edit_markers` (list / delete / update) · `manage_timelines` (list / open / rename / duplicate) · `add_quality_markers` · `set_clip_color` · `apply_lut` (a LUT file, or the built-in `hlg-to-rec709` for pale phone HDR clips) · `grade_clips` (CDL by numbers or copy a grade, verified on the frames) · `queue_render` (preset, or format / codec / size / range) · `make_format_version` (9:16, 4:5, 1:1 copy, centre-cropped) · `render_status` · `add_subtitles` · `add_title` · `checkpoint` (save / restore / .drp backup) · `open_page` |
| Build timelines (auto-routed) | `import_media` (into bins, no duplicates) · `append_clips` (source ranges onto the current, a named or a new timeline, any track) · `remove_clips` (by index, name-checked) |
| Inspector & tracks (auto-routed) | `set_clip_properties` (zoom, pan, crop, opacity, scaling, enable; read back) · `set_tracks` (add, rename, mute/hide, lock) · `export_timeline` (FCPXML, EDL, AAF, OTIO, DRT, CSV) |
| Lua | `run_lua` (live) · `list_lua_snippets` · `render_lua_snippet` (paste) |
| macOS UI | `send_keystroke` · `menu_pick` · `open_ai_feature` |

Designed for agents:
- **Plans live on the server.** Claude gets a `plan_id` and refines the cut with small operations (`keep`, `trim`, `move`, `insert`, `set`, …) instead of re-sending a whole timeline as JSON.
- **Every plan is validated** — clips past the end of their source, dissolves without handles, reused footage, fps mismatches, a music bed that ends early — and the warnings come back with each response.
- **Transcripts are cached** and numbered; Claude picks segments by index. Long transcriptions keep running in the background instead of timing out.
- **Edit the open timeline live.** `append_clips` adds a shot or B-roll where it belongs instead of re-importing a whole FCPXML as a new timeline, and returns the items that actually landed, with a warning wherever Resolve did something other than asked.
- **Read → act → verify.** `get_resolve_state` shows what actually landed in Resolve; `timeline_frames` shows the edit as rendered (grades, titles, transitions); on macOS `screenshot_resolve` shows what the user sees.
- **Claude can look at footage.** Frames come back as images (one per timestamp, or a single contact sheet to save tokens), so shot choices are made on what is actually in the picture.
- **Errors explain themselves** — what failed and what to do next.
- **Workflow guides** (rough cut, subtitles, music video, color grade, markers, live control + a Resolve Lua API cheat sheet) are served over MCP, as a tool and as prompts.
- **Known traps, written down.** Every Resolve quirk we hit (subtitles landing at the playhead, `SetLUT` refusing LUTs outside Resolve's folder, transitions listed as clips, a modal dialog blocking the API, ...) is in one ledger with what resolve-pilot does about it, and results point at the trap when it applies.

### Resources

Read-only context a client can load without a tool call: `resolve://traps` (the known-traps ledger), `resolve://capabilities` (which tool works on which channel — studio, bridge, Console window, paste-Lua, macOS UI — plus Free / Studio / Resolve 21.1+ and permission notes), `resolve://state` (the open project and timeline when live, else how to connect; answers within seconds) and `resolve://guide/{name}` (the workflow guides).

## Architecture

```
resolve_pilot/
├── analyzers/        ffprobe, scene + beat detection, frames + contact sheets, quality spans
├── transcribe/       Whisper (mlx / faster-whisper) + transcript cache
├── editorial/        rough-cut plans, edit operations, validation, plan store
├── exporters/        FCPXML 1.10 + SRT writers
├── bridge/           live Lua bridge: bridge.lua (runs in Resolve) + Python client
├── lua_snippets/     parameterized Lua for the Resolve Console
├── studio_api/       Resolve Studio's Python API
├── ui_automation/    osascript + macOS Accessibility, Resolve window capture
├── control.py        channel routing: Studio API → bridge → paste-able Lua
├── skills/           workflow guides (served over MCP)
└── mcp_server.py     MCP entry point (mcp SDK 1.x and 2.x)
```

State (plans, transcripts, bridge files) lives in `~/.resolve-pilot`; set `RESOLVE_PILOT_HOME` to move it, or `RESOLVE_PILOT_BRIDGE_DIR` to move only the bridge folder (re-run the bridge setup afterwards). On Windows, if your user folder has non-ASCII characters, the bridge reaches its folder through the folder's short (8.3) name, because Lua inside Resolve cannot open such paths; where short names are turned off, setup asks you to set `RESOLVE_PILOT_BRIDGE_DIR` to an ASCII folder of your own.

## Development

```bash
pip install -e ".[dev]"
ruff check . && pytest
```

The bridge tests run the real `bridge.lua` under LuaJIT (the Lua inside Resolve) against a mock Resolve API (`tests/lua/mock_resolve.lua`); install `luajit` to run them. `tests/test_server_e2e.py` drives the server through a real MCP client. CI covers mcp SDK 1.x and 2.x.

## Status

The test suite runs against a mock of Resolve's API, so this table records what has been run in a real Resolve, and what that covered. Agents read the same data from `resolve://capabilities` (`verified_live`).

<!-- verified-matrix:start -->
| Tool | Reaches Resolve through | Run in real Resolve | Covered |
|---|---|---|---|
| `resolve_pilot_status` | bridge heartbeat, Resolve's process | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | channel switches from manual to bridge<br>21.2.0: channel is console |
| `setup_bridge` | Resolve's Scripts folder | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | install into Workspace › Scripts, status; start with the bridge already running and from a closed Resolve (the menu click: tests only)<br>21.2.0: start with Resolve open: the Console answers, no bridge |
| `export_fcpxml` | a file you import | ✅ 20.3 Free, macOS (file import) | imported with import_timeline: clips, dissolves, the music bed on its own audio track; a 198-clip cut of 10 phone clips frame for frame, turned sources arriving turned and zoomed to fit |
| `export_srt` | a file you import | ✅ 20.3 Free, macOS (file import) | non-ASCII captions |
| `get_resolve_state` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | <br>21.2.0: 198 clips with their sources; markers |
| `import_timeline` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | FCPXML from export_fcpxml; paths with spaces, # and non-ASCII; with plan_id a 198-clip cut passes, a wrong frame rate is named<br>21.2.0: the 198-clip cut with plan_id: every clip on the plan's frames |
| `import_media` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | bins, files already in the bin reused<br>21.2.0: two files into a new bin |
| `append_clips` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | new timeline, exact source ranges, 23.976 into 24, audio on A2 at a record time<br>21.2.0: a new timeline from three source ranges |
| `remove_clips` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | ripple delete<br>21.2.0: ripple delete of one clip |
| `add_markers` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | seconds and frames on a 24 fps timeline; a second marker on the same frame is skipped. 23.976 / 29.97 DF not yet<br>21.2.0: seconds; a note with Turkish letters read back |
| `add_quality_markers` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | re-runs replace only their own markers<br>21.2.0: markers from a file's silent stretch |
| `set_clip_color` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | that run found transitions being colored too; fixed since, not re-run yet<br>21.2.0: three clips |
| `set_clip_properties` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | zoom, pan, opacity, flip, scaling, clip enable; read back and checked with timeline_frames<br>21.2.0: zoom and opacity, read back and seen in a frame |
| `set_tracks` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | rename, lock, add<br>21.2.0: rename, add a video track |
| `export_timeline` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | FCPXML, EDL, OTIO, DRT, AAF, CSV<br>21.2.0: FCPXML, EDL |
| `apply_lut` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | a LUT from outside Resolve's LUT folder, copied in; the built-in hlg-to-rec709 on 198 phone HDR clips (37-41 dB against Apple's own conversion), an SDR timeline left alone<br>21.2.0: the built-in hlg-to-rec709 on 198 clips, looked at with timeline_frames |
| `queue_render` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | known preset queued, an unknown one lists the presets; format / codec / size / range: a 3 s 1080×1920 H.264 MP4<br>21.2.0: a 2 s H.264 MP4, rendered and probed |
| `open_page` | Studio API · bridge · Console window · paste Lua · macOS UI | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | all seven pages<br>21.2.0: color, edit, deliver, media |
| `run_lua` | bridge · Console window | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | results and errors; `return _G` does not hang. print capture and request globals were reworked since, not re-run yet<br>21.2.0: values, print output, Lua errors; a 370 KB answer in seven parts |
| `send_keystroke` | macOS UI | not yet | keystrokes reach Resolve only when it is frontmost |
| `menu_pick` | macOS UI | ✅ 20.3 Free, macOS (macOS UI) | Workspace › Scripts › ResolvePilotBridge |
| `add_subtitles` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | timelines starting at 0 and at 01:00:00:00; a plan's 176 captions, each within a frame of the file<br>21.2.0: a plan's 176 captions, then replaced |
| `add_title` | bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | overlay above V1, the edit left alone<br>21.2.0: Turkish letters, seen in a frame |
| `checkpoint` | bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | save, list, restore, .drp backup<br>21.2.0: save, list, restore, .drp backup |
| `grade_clips` | bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | CDL grade, copy, reset; change measured on exported frames<br>21.2.0: saturation 0, measured on exported frames and seen |
| `make_format_version` | bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | 9:16 copy of a 16:9 edit, rendered filled edge to edge<br>21.2.0: 9:16 copy |
| `manage_projects` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | list; create a scratch project (listed once saved); open the test project again with its current timeline; wrong names<br>21.2.0: list; create an empty project; open the first one again; a wrong name |
| `manage_timelines` | bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | <br>21.2.0: list, open |
| `edit_markers` | bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | <br>21.2.0: list, update, delete |
| `timeline_frames` | Studio API · bridge · Console window | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | playhead and page put back<br>21.2.0: three positions from four pages; from Deliver the stills are taken on Edit |
| `render_status` | Studio API · bridge · Console window · paste Lua | ✅ 20.3 Free, macOS (bridge)<br>✅ 21.2.0 Free, macOS (Console window) | <br>21.2.0: an empty queue; a job until it is complete |
| `screenshot_resolve` | macOS UI | ✅ 20.3 Free, macOS (macOS UI) |  |
| `open_ai_feature` | macOS UI | not yet | Studio-only features in Free answer ok: false and leave no dialog open |

No tool has been run by the maintainers through Resolve Studio's Python API (the studio channel) or on Windows or Linux (some API behaviour in resolve://traps was measured with Python on 21.0.4). On Resolve 21.2.0 Free (macOS) the maintainers measured that the bridge cannot start (the Scripts menu starts nothing; the Console's Lua has no file access); the tools marked "Console window" were run there through the console channel, the others not yet. Users report the bridge starting from the Console on 21.1.1 Free and not on 21.1.2 Free. On Windows and Linux, Resolve Free 21.1.2+ is left with pasting Lua by hand.
<!-- verified-matrix:end -->

Tools that don't touch Resolve (analysis, frames from files, plans) run in CI on Linux and macOS; transcription runs there only against a stand-in engine (mlx-whisper and faster-whisper are not exercised in CI). Reports from other Resolve versions, Studio, Windows and Linux are very welcome. Linux/Windows UI automation is not planned — the live bridge covers those platforms.

## License

MIT.
