# v0.2.0 — make the agent work better inside Resolve

Goal: an agent connected to resolve-pilot should be able to *see* Resolve, act on it,
verify the result and iterate — in the Free edition too — while spending fewer tokens
and making fewer blind guesses.

Reference projects studied: samuelgursky/davinci-resolve-mcp (compound tools, tool
annotations, agent docs served over MCP), ahujasid/blender-mcp (in-app bridge +
`get_scene_info` + `execute_code` loop).

## Plan

- [x] **Install is broken today**: `mcp>=1.27` resolves to mcp 2.x, which removed
      `mcp.server.fastmcp` → server crashes on import. Support both 1.x and 2.x.
- [x] mcp 2.x hides plain exception messages from the agent → wrap every tool so
      errors surface as actionable `ToolError` text.
- [x] Live Lua bridge for the Free edition (the existing `bridge.lua` was never wired up):
  - [x] protocol v2: request ids, JSON results, cross-platform file ops, idle timeout,
        stop file, "newest bridge wins", `rp.*` helpers
  - [x] Python client (`resolve_pilot/bridge`) + install into Workspace › Scripts
  - [x] end-to-end tests running the real bridge in LuaJIT against a mock Resolve
- [x] One tool per action with automatic routing Studio API → bridge → paste-able Lua
      (`get_resolve_state`, `import_timeline`, `add_markers`, `set_clip_color`,
      `apply_lut`, `queue_render`, `open_page`) + `run_lua` escape hatch
- [x] Server-side plan store: `plan_id` + `edit_plan` ops instead of resending whole
      plan JSON; `validate` on every response (warnings the agent can fix)
- [x] Transcript cache + numbered compact segments + background job (no client timeout
      loss); `plan_from_transcript(segment_indices=…)`; faster-whisper fallback for
      Windows/Linux/Intel
- [x] Music bed support in plans/FCPXML (the music-video playbook needed it but the
      tooling could not express it); `Path.as_uri()` for asset URIs
- [x] SRT line wrapping / cue splitting (playbook rule, previously manual)
- [x] Server `instructions`, tool annotations, typed params (no `*_json` strings),
      workflow guides exposed via `get_workflow_guide` + MCP prompts
- [x] Lua snippets: safe value escaping (paths/names with quotes broke the Lua),
      parameters listed automatically, structured `return` values
- [x] Playbooks updated to the new tools + new `resolve-live-control` guide with a
      Resolve Lua API cheat sheet
- [x] CI (ruff + pytest, mcp 1.x and 2.x), README, CHANGELOG

Coordinate with open PR #1 (Resolve 21 menu fixes): do not touch `ui_automation/*`;
new UI tools catch `UIAutomationError`, so its `StudioOnlyFeature` lands as `ok: false`.

## Review

Done; 93 tests pass on Python 3.11/3.12 with mcp 1.30 and 2.3 (ruff clean).

Verified:
- Fresh `pip install -e .` pulls mcp 2.x; the server now starts and lists 27 tools + 5 prompts
  on both SDK majors (`tests/test_server_e2e.py` drives it through a real stdio MCP client).
- The bridge runs for real under LuaJIT against `tests/lua/mock_resolve.lua`: JSON results,
  print capture, syntax/runtime errors with tracebacks, stop file, newest-bridge-wins, and every
  snippet (compiled and, for the routed actions, executed).
- Full agent loop over MCP: probe → scenes → beats → create_plan (music bed) → edit_plan →
  export_fcpxml → import_timeline (bridge) → get_resolve_state → add_markers → run_lua.

Bugs found along the way (pre-existing): `detect_beats` failed on every file (piped WAV header);
`detect_scenes` turned ffmpeg failures into "0 cuts"; transcript plans repeated words where
padded segments overlapped; FCPXML per-clip rounding left 1-frame gaps; Lua snippets embedded
unescaped paths. A bug in my own first bridge version (two bridges started in the same second
got the same token) was caught by the newest-bridge-wins test.

Not verified here (no Resolve in this environment): behavior inside a real Resolve —
`bmd.wait` availability and UI responsiveness while the bridge loop runs, Workspace › Scripts
discovery of the installed script on each OS, Resolve's import of the connected-clip music bed.
These need a manual check on a real install (Free and Studio). The faster-whisper engine was
installed and reached model loading, but the model download is blocked in this sandbox, so its
transcription output is untested.
