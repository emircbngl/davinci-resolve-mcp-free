# DaVinci Resolve MCP — Works in DaVinci Resolve Free

**Claude-driven video editing that runs in [DaVinci Resolve Free](https://www.blackmagicdesign.com/products/davinciresolve) — the no-cost edition that Blackmagic ships at $0.** No Studio license required, no API key required.

> ℹ️ **What "free" means here:** DaVinci Resolve ships in two editions — **Free** (no cost) and **Studio** (~$295 one-time license). Studio exposes a Python scripting API for automation; Free does not. Every existing DaVinci MCP server out there ([samuelgursky/davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp), barckley75, apvlv, Tooflex) **requires Resolve Studio** for that reason. This project gets Claude-driven editing working in **Resolve Free too**, by routing around the missing Python API:

| Channel | What it does | Free | Studio |
|---|---|:---:|:---:|
| **Live Lua bridge** | Claude reads and changes the open project directly: import media, build and change timelines clip by clip, import timelines, add markers, color clips, apply LUTs, queue renders, run any Lua | ✅ | ✅ |
| **FCPXML export** | Build a rough cut outside Resolve, import via `File > Import > Timeline` (or automatically through the bridge) | ✅ | ✅ |
| **Lua snippets** | Ready-to-paste Console snippets when nothing live is connected | ✅ | ✅ |
| **UI automation** (macOS) | Page switching, menu items, Magic Mask & other AI tools via the Accessibility API; screenshots of the Resolve window | ✅ | ✅ |
| **Python scripting API** | Direct DaVinciResolveScript control | ❌ | ✅ |

Every Resolve action picks the best channel by itself — Studio API, then the live bridge, then paste-able Lua — and tells Claude which one it used.

If you've been turned away by other DaVinci MCP servers because you only have the free Blackmagic download — this is for you.

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
| Session | `resolve_pilot_status` · `get_workflow_guide` · `setup_bridge` |
| Analyze | `probe_media` · `detect_scenes` · `detect_beats` · `transcribe_audio` (cached, background jobs) · `analyze_quality` (silence, black, freeze, loud/quiet) |
| See | `grab_frames` · `scene_frames` · `read_video` (speech + frames) · `screenshot_resolve` (macOS) · `timeline_frames` (the edit as Resolve renders it, any OS) — images Claude looks at |
| Plan | `plan_from_transcript` · `plan_from_scenes` · `create_plan` · `get_plan` · `edit_plan` |
| Export | `export_fcpxml` · `export_srt` |
| Resolve (auto-routed) | `get_resolve_state` · `import_timeline` · `add_markers` · `add_quality_markers` · `set_clip_color` · `apply_lut` · `queue_render` · `render_status` · `open_page` |
| Build timelines (auto-routed) | `import_media` (into bins, no duplicates) · `append_clips` (source ranges onto the current, a named or a new timeline, any track) · `remove_clips` (by index, name-checked) |
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

State (plans, transcripts, bridge files) lives in `~/.resolve-pilot`; set `RESOLVE_PILOT_HOME` to move it, or `RESOLVE_PILOT_BRIDGE_DIR` to move only the bridge folder (re-run the bridge setup afterwards). On Windows, if your user folder has non-ASCII characters, the bridge uses `%PUBLIC%\resolve-pilot\bridge`, because Lua inside Resolve cannot open such paths.

## Development

```bash
pip install -e ".[dev]"
ruff check . && pytest
```

The bridge tests run the real `bridge.lua` under LuaJIT (the Lua inside Resolve) against a mock Resolve API (`tests/lua/mock_resolve.lua`); install `luajit` to run them. `tests/test_server_e2e.py` drives the server through a real MCP client. CI covers mcp SDK 1.x and 2.x.

## Status

Works end-to-end for the documented flows; Resolve-side behavior is mocked in tests, so reports from real Resolve versions (Free and Studio, any OS) are very welcome. Linux/Windows UI automation is not planned — the live bridge covers those platforms.

## License

MIT.
