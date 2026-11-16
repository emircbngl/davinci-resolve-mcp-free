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

# Merge the pending `claude/nifty-curie-hiir2l` work (5 commits on top of 64ec2f2)

The branch was pushed on 2026-10-15 without a PR: bridge hardening (locks, PID-aware heartbeat,
sandboxed request globals, result caps), plan/export safety (file locks, no silent overwrite),
detect_beats fixes, the verified-live ledger (`verified.py`) and trimmed tool schemas.

- [x] Fast-forward this worktree onto the branch (no conflicts: it sits on main)
- [x] Review the diff: bridge/locking independently, the rest here
- [x] pytest + ruff on the merged tree (mcp 2.x locally; CI covers 1.x)
- [x] Fix what the review confirms, each fix with a test that fails without it
- [x] Fast-forward local main; nothing pushed (pushing waits for Emir and `github-push`)

## Review

Merged tree: 316 passed (284 before the branch), ruff clean; Python 3.13, mcp 2.3. mcp 1.x was
not run locally.

Fixed (reproduced first):
- A bridge in another PID namespace (Flatpak, container) was reported "gone" when this server
  first saw it mid-request: its pid always looks dead from here, and the server only learned to
  ignore it from an idle heartbeat. Now a dead-looking pid counts only for a bridge whose pid
  this server has seen alive. The updated heartbeat test fails on the branch's code
  (`running=False`) and passes with the fix. Trade-off: if Resolve crashes mid-request and the
  server starts only afterwards, the dead bridge reads as busy for up to 15 minutes, as before
  the branch.
- Windows `_pid_alive` read `GetLastError()` without `use_last_error`, so the access-denied
  answer could be lost. Not run on Windows.

Not fixed, recorded:
- PID reuse: if Resolve dies mid-request and an unrelated process later gets its pid, the bridge
  reads as "busy, possibly stuck" instead of gone; only a new bridge clears it.
- Windows: a heartbeat rename that collides with the server reading the file can drop the
  "busy" marker (older behaviour; the new 10 Hz polling makes it more likely).

# PR #3: add_title picks a font style the family has (dennisiver, 6bcac28)

Text+ keeps "Semibold" when only Font changes; a family without it fails every render. The PR
checks font and style against Fusion's FontManager before building anything.

- [ ] Merge the PR head as is (`--no-ff`, so GitHub sees the contributor's commit as merged)
      and resolve conflicts with the bridge/export work merged above
- [ ] Full pytest + ruff on the merged tree
- [ ] Follow-up: when FontManager cannot be asked, say so in `warnings` instead of leaving
      the Semibold trap silently open; test red without it
- [ ] Fast-forward local main; draft the PR reply for Emir (nothing posted or pushed here)
