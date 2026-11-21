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
- (Dropped, see Review 2) Pause threshold = noise floor + 6 dB, tuned on pink noise under a
  tone. It held up on synthetic audio and found nothing on real vlog footage.

## Review 2: real speech (Emir's vlog footage) changed the design

Run on 12 clips from the Fethiye vlog (17 min; 10 with enough speech, 1,221 words), read only.

- The first design (ffmpeg silencedetect, threshold = noise floor + 6 dB) found **0 pauses** in
  the vlog clips: every cut fell back to Whisper's times. Outdoors the gap between two words is
  only 8-12 dB below the words. Replaced by the voice-band level curve (250-3400 Hz, 10 ms) and
  a valley search between the two words, relative to how loud those words are. In that band a
  real gap is a median 240 ms wide, a dip inside continuous speech 90 ms.
- A valley search across a long gap kept everything in it (a 17 s cut became 35.6 s: waves and
  music between sentences). Words more than 0.65 s apart now get a cut point each, next to the
  word.
- A wide search window (0.5 s into the next word) made one clip worse on the transcribe-again
  check (133 vs 101 words changed). The window is now Whisper's own gap, widened only while the
  sound is still quiet where the word is said to start. Same clip after: 101 vs 101.
- cut_report counted seamless joins between consecutive segments as tight cuts (420 of 626 on
  the vlog). Joins where nothing is cut out are no longer counted.

Final numbers, cleaned cut (retakes, hesitations and repeats from scan_speech dropped,
max_pause 0.5 s) vs the same cut on Whisper's word times:
- cut points in sound (less than 3 dB below the words): 26 of 264 (10%) vs 89 of 288 (31%)
- words lost or changed when the cut audio is transcribed again: 307 of 1,221 (25.1%) vs 342
  (28.0%). Noisy measure: per clip it goes both ways (35 vs 63, 13 vs 17, but 22 vs 15).

My own harness was wrong once: the first "transcribe the cut again" run compared two renders
that were both the whole file (aselect did not shorten the audio), so both methods scored
exactly the same. Caught by the identical numbers; the render now checks its length against
the plan.

1 new test per rule, each red without it (far-apart words, shallow dips, tight cut point,
seamless joins).

Not verified / known limits:
- Nobody has listened to these cuts. The numbers say they land in quieter places than before,
  not that they sound right; the first real edit still needs ears.
- The vlog clips auto-detected as Arabic / Russian in two cases (music, short speech): pass
  `language` when it is known.
- faster-whisper words are covered by a unit test of the conversion only, not a real run.
- Whisper tends to leave "um" / "uh" out of the text, so those cannot be found from the
  transcript (said in the tool description). Untranscribed sound between words was left out on
  purpose: with edges this far off, it would flag ordinary speech.

# Captions that follow the cut (one-prompt video, step 2)

Today captions only fit a timeline that plays the transcribed file from its start:
`export_srt` / `add_subtitles` write the source's own times. After a rough cut (segments
picked, reordered, words dropped) every caption is in the wrong place, and the only way out is
to render the cut and transcribe it again.

- [ ] A plan remembers the transcript it was cut from (`transcript_ids`, per source file)
- [ ] `editorial/captions.py` (pure): each clip takes the transcript words whose middle lies in
      its source range and moves them to the clip's place on the timeline, on the exporter's
      own frame positions (`fcpxml.layout`), so captions match the imported timeline. Words
      become cues: break at the line limit, at sentence ends and at gaps; short cues get a
      minimum time; no overlaps. Transcripts without words fall back to whole segments
- [ ] `export_srt(plan_id=...)` and `add_subtitles(plan_id=...)`; extra `transcript_ids` for
      plans built with create_plan. Clips whose source has no transcript are reported, and it
      is an error when no clip has one
- [ ] Tests: reorder, drop, fade-in offset, frame positions, segment fallback, missing
      transcripts, edit_plan then export again; red without the mapping
- [ ] Real check on the vlog cut: transcribe the rendered cut and compare word times with the
      captions
- [ ] Guides (subtitle, rough cut), instructions, README, CHANGELOG; full pytest + ruff

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
