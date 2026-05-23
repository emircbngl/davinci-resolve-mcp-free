# DaVinci Resolve MCP — Works in DaVinci Resolve Free

**Claude-driven video editing that runs in [DaVinci Resolve Free](https://www.blackmagicdesign.com/products/davinciresolve) — the no-cost edition that Blackmagic ships at $0.** No Studio license required, no API key required.

> ℹ️ **What "free" means here:** DaVinci Resolve ships in two editions — **Free** (no cost) and **Studio** (~$295 one-time license). Studio exposes a Python scripting API for automation; Free does not. Every existing DaVinci MCP server out there ([samuelgursky/davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp), barckley75, apvlv, Tooflex) **requires Resolve Studio** for that reason. This project gets Claude-driven editing working in **Resolve Free too**, by routing around the missing Python API through three channels:

| Channel | What it does | Free | Studio |
|---|---|:---:|:---:|
| **FCPXML/OTIO export** | Build a rough cut outside Resolve, import via `File > Import > Timeline` | ✅ | ✅ |
| **Lua snippets** | Generate ready-to-paste Console snippets for batch markers, LUT apply, render queue, etc. | ✅ | ✅ |
| **UI automation** (macOS) | Drive Magic Mask, Smart Reframe, Voice Isolation, page switching via Accessibility API | ✅ | ✅ |
| Python scripting API | Direct DaVinciResolveScript control | ❌ | ✅ (planned) |

If you've been turned away by other DaVinci MCP servers because you only have the free Blackmagic download — this is for you.

## Installation

```bash
git clone https://github.com/emircbngl/davinci-resolve-mcp-free.git
cd davinci-resolve-mcp-free
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[transcribe,ui]"
```

Requirements that should already be on the machine:
- macOS 12+ (Apple Silicon recommended for local Whisper)
- Python 3.11+
- `ffmpeg` + `ffprobe` (`brew install ffmpeg`)
- DaVinci Resolve (Free or Studio)

## Connecting Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "resolve-pilot": {
      "command": "/path/to/davinci-resolve-mcp-free/.venv/bin/resolve-pilot"
    }
  }
}
```

Restart Claude Desktop. The `resolve-pilot` server should appear in the tool list.

## What Claude can do

After connecting, ask Claude things like:

- *"Probe this video and tell me its codec, fps, and how many scene cuts it has."*
- *"Transcribe the audio of `~/Footage/interview.mp4` and write an SRT next to it."*
- *"Build a rough cut from the transcript — keep only the segments where speaker 1 sounds confident."*
- *"Give me a Lua snippet that color-codes every clip on V2 orange."*
- *"Open the Color page and apply this LUT to all clips on V1."*
- *"Make a vertical 9:16 rough cut from this 30-min talk."*

Claude calls the tools, returns the FCPXML / Lua / SRT, and you paste / import in Resolve.

## Architecture

```
resolve_pilot/
├── analyzers/        ffprobe + scene detection
├── transcribe/       mlx-whisper local transcription
├── exporters/        FCPXML 1.10 + SRT writers
├── lua_snippets/     parameterized Lua for Resolve Console
├── ui_automation/    osascript + macOS Accessibility
├── editorial/        rough cut planner (data layer for Claude's decisions)
└── mcp_server.py     FastMCP entry point
```

## Status

Early scaffolding — works end-to-end for the documented flow. Studio Python
API integration is planned. Linux/Windows UI automation is planned (currently
macOS-only for UI tools).

## License

MIT.
