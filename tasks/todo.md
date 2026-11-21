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

- [x] Merge the PR head as is (`--no-ff`, so GitHub sees the contributor's commit as merged)
      and resolve conflicts with the bridge/export work merged above
- [x] Full pytest + ruff on the merged tree
- [x] Follow-up: when FontManager cannot be asked, say so in `warnings` instead of leaving
      the Semibold trap silently open; test red without it
- [x] Fast-forward local main; draft the PR reply for Emir (nothing posted or pushed here)

## Review

The merge had no textual conflicts. The PR's tests pass on top of the reworked bridge,
including the 21.1-style sandbox, where request globals live in a bridge-owned table: 318
passed. With the warning follow-up: 320 passed, ruff clean (Python 3.13, mcp 2.3). The new
test fails with `KeyError: 'warnings'` in both bridge modes without the follow-up.

Not run here: the contributor's live check on Resolve 21.1.1 Free. On Emir's 20.3 Free, a CJK
title still needs a live render to confirm.

# Word-level cuts (one-prompt video, step 1)

Goal: cuts that never clip a word, and a way to remove pauses, fillers and repeats inside a
kept line. Today `word_timestamps=true` returns nothing (words are not even cached) and
`plan_from_transcript` cuts at Whisper segment times ± a fixed pad.

Measured first (Turkish TTS with known pauses, mlx large-v3-turbo vs ffmpeg silencedetect):
Whisper word edges are off by up to 460 ms (a word after a pause "starts" inside the pause; a
word before one "ends" up to 290 ms early). Cutting on word times alone would clip words, so
cut points are snapped into the real pause next to the word, and fall back to the word-gap
midpoint only where speech is continuous (reported as a tight cut).

- [x] Words: `Word` on `Segment`, filled by both engines, stored with the transcript; pauses
      (real silences, threshold from the file's own noise floor) stored next to them.
      `transcribe_audio` records words by default and `detail="words"` shows them with
      `segment.word` addresses and pause marks
- [x] `plan_from_transcript`: with words, snap every cut into the neighbouring pause;
      `drop_words` removes word ranges inside kept segments; `max_pause_seconds` tightens long
      pauses inside kept speech. Reports snapped vs tight cuts. Without words: as before
- [x] `scan_speech`: candidates only — long pauses, filler words, immediate repeats, retakes —
      each with the `drop_words` / segment index that would remove it
- [x] Tests: pure snapping and scan logic; words through the store; pause detection on real
      ffmpeg audio; the tools over the server. Red without the change
- [x] Rough-cut guide, README, CHANGELOG; full pytest + ruff; try on speech; local main

## Review

337 passed (321 before), ruff clean; Python 3.13, mcp 2.3. With the pause lookup switched off,
8 of the new tests fail (cuts leave the pauses, the clipped-word case comes back).

Run for real (mlx large-v3-turbo + ffmpeg, a 17.6 s Turkish TTS recording with planted fillers,
repeats and a retake): scan_speech found the retake, both repeats and both fillers; the cleaned
cut is 10.9 s; the 6 cuts in a pause measure -87 to -90 dB at the cut point, the 2 tight ones
(a word dropped in continuous speech) sit in sound at about -15 dB and are reported as tight.

Design notes:
- Segments without words count as one word, so transcripts without word timings and inline
  segments take the same path; the old numbers are unchanged (existing tests untouched).
- Pause threshold = noise floor + 6 dB. Measured against +9 / +12 on 40 seeds of pink noise
  under a tone and on noisy speech: +6 misses part of a pause in 3 of 40 (safe side: the cut
  lands deeper in the pause) and reaches 36 ms into speech; +9 misses none but reaches 74 ms.
- A click splitting one pause in two is merged (seen with seeded noise: 3.0-3.188 / 3.188-3.4).

Not verified / known limits:
- Real human speech: only TTS was measured. Whisper's edge errors and room tone differ on a real
  recording; the first real video should be checked by ear.
- faster-whisper words are covered by a unit test of the conversion only, not a real run.
- Tight cuts still use Whisper's word times; snapping them to the local energy minimum is a
  possible next step.
- Whisper tends to leave "um" / "uh" out of the text, so those cannot be found from the
  transcript (said in the tool description). Untranscribed sound between words was left out on
  purpose: with edges this far off, it would flag ordinary speech.

# Issue #4: the bridge does not run on Resolve 21.1.2 Free

Reported (Mitch, issue #4, macOS Apple Silicon): on 21.1.2 Free the Scripts-menu entry does
nothing, `dofile` is nil and the pasted script stops with "neither io nor ffi". On 21.1.1 Free
the Console `dofile` still worked (PR #3's author). Every place that says "written for 21.1, not
yet tried" now says what was reported, per patch release, and points at 20.3.x.

- [x] `resolve_pilot_status` (manual channel) and `setup_bridge(install)`: on 21.1.2+ Free say the
      bridge cannot start there and what still works; 21.1.0-21.1.1 keep the console_command
      note. Needs the edition (Free vs Studio app bundle) and a patch-level version compare
- [x] `resolve://capabilities` edition note, the `resolve-21-1-free-lua-sandbox` trap, the
      `verified.UNTESTED` note (regenerate the README matrix), README intro, CHANGELOG
- [x] Tests: version compare with patch; status / setup_bridge notes for 21.1.1 vs 21.1.2 Free
      vs 21.1.2 Studio; red without the change
- [x] Full pytest + ruff; fast-forward local main; draft the issue reply (Emir approves posting)

## Review

321 passed, ruff clean. The new status test fails when the 21.1.2 branch is removed (the
install suggestion comes back on 21.1.2 Free). Every 21.1 statement is a user report, not a
maintainer measurement: nobody here has a 21.1 install. Whether pasted Lua (the manual
channel) still works on 21.1.2 Free is unknown, so the notes do not claim it.
