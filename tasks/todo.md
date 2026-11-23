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

# One request, a folder of clips (one-prompt video, step 5; done before step 4)

Emir went to bed with "handle whatever there is until morning". Step 4 (audio) writes things
into the FCPXML that only a try in Resolve can confirm, and Resolve is his open project, so
this night takes what can be proven here: a vlog is many files, and today every tool takes one.

- [x] `scan_folder(folder)`: the media files in recording order with what an editor needs
      (length, frame, rotation, sound), and with `transcribe=true` their transcripts started in
      the background and followed by calling again. A client with only MCP tools cannot list a
      folder at all
- [x] `plan_from_transcript(append_to=plan_id)`: add a file's cleaned cut to an existing plan,
      which remembers every source's transcript (captions and the preview already take several)
- [x] Guide `resolve-one-prompt-edit`: the whole run, from the folder to the preview the user
      watches, with what to decide and what to report
- [x] Tests; red without the protection
- [x] Real run through the tools only: all vlog clips -> one plan -> preview with captions
- [x] Instructions, README, CHANGELOG; full pytest + ruff
- [x] Added on the way: `edit_plan` `turn` (see the review)

## Review

- Real run (`scan_folder` -> `scan_speech` / `plan_from_transcript(append_to)` per file ->
  `edit_plan turn` -> `render_preview` -> `export_srt`), sources read only: 11 files, 787 s;
  10 used (one has 6 words), 201 clips, 382.6 s, 177 captions, 78 real cuts; rendered in 184 s.
  Issues listed: 10 `cut_in_sound`, 19 `level_jump`, 5 `flash_clip`, 6 `fast_caption`. The run
  made only the mechanical choices and did not work through the issues: an agent would.
- Looking at one frame per file showed that six of the eleven clips play on their side (tagged
  90 / -90, phone held sideways), two tagged -90 are true vertical shots and two tagged -180 are
  fine. Nothing could put that right, so `turn` was added: a property of the source file, kept
  in the plan, shown by the preview. My first guess for one file's direction was wrong
  (upside down) and the next preview showed it, which is the loop working. FCPXML does not
  carry the turn; `export_fcpxml` lists the clips to turn in Resolve. That half is not tried.
- `rotated_source` was listed for the 180-degree tags too, three false alarms in this run; it
  is now for quarter turns only.
- Whisper called one 28-word clip Russian and the recitation Arabic (language left to it in
  this run). The guide tells the agent to pass the language when the user names it.
- The first full run kept six lines nobody says ("Altyazı M.K." and the like), captions
  included. Measured on the 350 real segments: their word probability (0.87-0.94) and the level
  under them look like speech, so neither can flag them, and a mean word probability under 0.5 would flag
  56 segments, one of them invented. `scan_speech` now lists them by wording (`likely_invented`): 6 of 350, all six
  the invented ones.
- Second full run with the invented lines left out: 198 clips, 380.0 s, 176 captions, none of
  the six lines in them. Rendering the same length took 184 s, 331 s (tests running beside
  it) and 400 s on three runs: say "3 to 7 minutes", not "3".
- 14 mutations over the new code: 13 red at once; "camera time although one file has none"
  stayed green because the test's two orders agreed. The test now uses orders that differ.
- Not done: step 4 (audio: fades at cuts, loudness per clip, ducked bed) and the Resolve side of
  `turn`. Both write things only a try in Resolve confirms, which needs Emir and a test project.

# A preview without Resolve, checked automatically (one-prompt video, step 3)

Today nobody sees a plan until it is imported into Resolve, and nothing looks at the result. An
agent working from one prompt needs to watch its own cut and be told what is wrong with it.

- [x] Room in the tool list: the advertised schemas are 105 characters under their 50,000
      budget. Optional top-level parameters are advertised as `anyOf [type, null]` + `default
      null`; advertise the plain type (pydantic still accepts null). Nested models keep theirs
      (`audio_bed: null` means "remove")
- [x] `exporters/preview.py`: render a plan to a small MP4 with ffmpeg alone. Every clip gets
      exactly the frames `fcpxml.layout` gives it (so times in the preview are timeline times,
      and captions fit), PCM audio in the intermediates (AAC padding made a 45-clip preview
      1.6 s too long), fade gaps as black, the audio bed mixed in, sources shown as players show
      them (rotation tag applied) inside the plan's frame. Dissolves are shown as cuts and said
      so. Captions as a subtitle track when the plan's transcripts are known
- [x] `editorial/review.py` (pure): issues from the plan and from the rendered preview, each with
      the clip and the timeline time: plan errors / warnings, flash clips, silence / black /
      frozen stretches (analyze_quality on the preview, mapped to clips), level jumps across
      cuts, captions too fast to read, length differing from the plan, rotated sources
- [x] Tool `render_preview(plan_id, ...)`: background job like transcribe_audio (`status:
      running`, call again), a stable file per plan, result = path + issues + how to look at it
      (grab_frames / read_video take the path)
- [x] Tests: frame-exact length over many short clips, fades, bed, a source without audio, each
      issue kind found on synthetic footage; red without the protection
- [x] Real check: the vlog cuts; read the issues against what is in the footage
- [x] Guide (rough cut), instructions, README, CHANGELOG; full pytest + ruff

## Review

- The test footage says where it is: frame N is grey of brightness 20 + 4N and the sound is one
  click, so a test reads back which source frame every preview frame shows and when the click
  is heard. That is how "frame-exact" is asserted, not by counting frames alone.
- Real check through the tools (10 vlog cuts, 310 clips, 116 real cuts): 552.6 s of preview in
  199 s, all with exactly the plan's frames, captions on every one. Issues: 16 `cut_in_sound`,
  30 `level_jump`, 11 `flash_clip`, 7 `fast_caption`, 8 `rotated_source`; no silence, black or
  frozen stretch. The level jumps were read against one clip's levels and transcript: lines of
  a far speaker (-35 dB) next to the one holding the phone (-21 dB), so they are real, and they
  are what step 4 (loudness per clip) has to even out. The cuts in sound are not confirmed:
  nobody listened.
- `cut_in_sound` is stricter than step 1's `tight`: 16 of 116 cuts against 29 of 38 cut points
  tight on one clip. `tight` says "no pause here", this says "not even a 6 dB dip at the cut".
- Rotation: 8 of the 10 clips are tagged 90 degrees. On the one looked at (IMG_8139) the
  picture is stored upright, so players (and the preview) show it on its side inside a
  landscape frame. Reported, not corrected: the plan's
  frame still comes from the stored size, and what Resolve does with the tag has not been tried
  (needs a test project).
- Every protection removed in turn (27 mutations over preview, review, the tool, probe, jobs
  and the schema change): the tests went red each time.
- Not done: dissolves and fades in the preview (cuts and black gaps), burned-in captions (this
  ffmpeg has no subtitles / drawtext filter), a check that re-transcribes the preview to find
  lost words (tried in step 1 as a measure: too noisy to be a per-cut verdict).

# Captions that follow the cut (one-prompt video, step 2)

Today captions only fit a timeline that plays the transcribed file from its start:
`export_srt` / `add_subtitles` write the source's own times. After a rough cut (segments
picked, reordered, words dropped) every caption is in the wrong place, and the only way out is
to render the cut and transcribe it again.

- [x] A plan remembers the transcript it was cut from (`transcript_ids`, per source file)
- [x] `editorial/captions.py` (pure): each clip takes the transcript words whose middle lies in
      its source range and moves them to the clip's place on the timeline, on the exporter's
      own frame positions (`fcpxml.layout`), so captions match the imported timeline. Words
      become cues: break at the line limit, at sentence ends and at gaps; short cues get a
      minimum time; no overlaps. Transcripts without words fall back to whole segments
- [x] `export_srt(plan_id=...)` and `add_subtitles(plan_id=...)`; extra `transcript_ids` for
      plans built with create_plan. Clips whose source has no transcript are reported, and it
      is an error when no clip has one
- [x] Tests: reorder, drop, fade-in offset, frame positions, segment fallback, missing
      transcripts, edit_plan then export again; red without the mapping
- [x] Real check on the vlog cut: transcribe the rendered cut and compare word times with the
      captions
- [x] Guides (subtitle, rough cut), instructions, README, CHANGELOG; full pytest + ruff

## Review

- Which clip plays a word is decided by the word's middle. Checked against the words the cut
  really kept on the 10 vlog clips: 1,219 of 1,221 right; the other two were Whisper words with
  no length (start = end) stacked 5 ms before a cut. Such a word now goes with its neighbour:
  1,221 of 1,221.
- Real check (9 clips, 311 pieces, 1,217 caption words): the cut audio rendered and transcribed
  again, 940 words paired. Within 0.3 s of where the word is heard: 870 (93%), within 0.5 s: 899
  (96%), median per clip 0.01-0.03 s. At the source's own times: 45 (5%). The pairing itself depends on
  Whisper hearing the same word twice, so the unpaired 277 say nothing either way.
- The first cues were not readable everywhere, which the unit tests had not shown: one caption
  stayed up 20.5 s (slow recitation, no punctuation), 8 of 215 over 7 s, 4 under 0.7 s. Added:
  6 s maximum, a break at a pause once a line is full, a sentence that flashes by shares a cue
  with the next, and a hold for reading speed. After: 0 over 7 s, 1 under 0.7 s (a Whisper
  "subtitles by" hallucination in the last 0.3 s of a clip), cues faster than 20 characters a
  second 57 -> 31.
- Found while rendering a preview of a real cut: Whisper had closed the file with an empty
  17 ms segment, and step 1 made a clip of it (under one frame; `validate_plan` already called
  it an error, so the export would have been refused, but the plan should not contain it).
  `transcript_cut` now leaves such segments out and lists them in the cut report. Test red
  without the fix.
- Every protection removed in turn (13 mutations): the caption tests went red each time.
- Not done: tried on a timeline in Resolve (needs a test project; only the SRT is new). Caption
  starts use Whisper's word times, which are up to ~0.5 s off around pauses; they are clamped to
  their clip but not corrected with the level curve.
- The advertised schemas were 142 characters over the 50,000 budget after the new parameters;
  their descriptions were shortened (49,895 now). The budget is nearly used up: step 6 (schema
  slimming) is due before more parameters.

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

# Tried in Resolve: the one-request cut on a real timeline (20.3 Free, project resolve-pilot-test)

The vlog plan (198 clips, 10 phone sources, 6 of them turned) was exported, imported and read
back. What Resolve did, and what follows from it:

- The plan's fps was the first source's *average* rate, 23.9989 (phones record a variable rate).
  Resolve made a 24 fps timeline of it, put the audio on two alternating tracks (99 + 99) and
  left a one-frame hole after clip 137. The same plan at exactly 24: one audio track, 198 of 198
  clips where the plan puts them.
- Resolve shows rotation-tagged clips as players do (the six sideways ones on their side).
- `<adjust-transform rotation scale>` inside an FCPXML clip arrives as RotationAngle / ZoomX /
  ZoomY (135 of 135 clips, values read back), so the file itself can carry a plan's turns.
  Rotation is counter-clockwise; a quarter turn of 16:9 footage on a 16:9 timeline needs 16/9 zoom.
- The plan's captions: 176 of 176 cues landed, none more than a frame (worst 0.020 s) from the file.

- [x] `probe_media`: `fps` is the file's nominal rate when the average is within 5% of it
- [x] `validate_plan`: warn about a frame rate Resolve timelines do not have (older plans)
- [x] FCPXML carries the turns: `Clip.turn_degrees`, `<adjust-transform>`, zoom from the source's
      shown size and the timeline's; `export_fcpxml` no longer asks for a manual step
- [x] `timeline_frames`: the positions' shape in the description (a client dropped the item schema)
- [x] Tests for each, red without the change; a 180° turn and a turned first clip with a music
      bed and fade-in tried in Resolve
- [x] Guides, README, CHANGELOG, the verified matrix; full pytest + ruff; commit

## Review

411 passed (379 before), ruff clean. 15 mutations (the average rate used again, the tolerance, a real
average overruled, a broken rate, the plan warning, usual rates flagged, the turn's direction,
the refit, a half turn, an unknown size, the rotation tag, the bed's place and its loss, the
plan's turns not reaching the exporter): the tests went red each time; one stayed green at
first (no case had a real average next to a different nominal rate) and got its case.

In Resolve 20.3 Free, project resolve-pilot-test, through the product code after the change
(timeline "vlog otomatik 4"): 198 of 198 clips on the plan's frames and source ranges, one
audio track, 135 turned clips with the rotation and zoom read back (97 at 90 / 1.7778, 38 at
-90 / 1.7778), 176 of 176 captions within a frame. Two small timelines ("turn test 1 / 2"):
a 180° turn, dissolves, a fade-in, and a music bed on a turned first clip all arrive.

- The zoom assumes the project fits mismatched clips into the frame (`scaleToFit`, Resolve's
  default and this project's setting); a project set to centre-crop was not tried.
- Looked at one frame of each source in Resolve; nobody watched the timeline play, and
  nobody listened to the cuts there.
- Noticed, not handled: the phone clips are HDR (Rec.2100 HLG) and the project is not colour
  managed, so the picture is pale in Resolve. The preview does not tone-map either.
- `timeline_frames` does not draw the subtitle track.
- Left in the test project: "vlog otomatik 1" (the 23.9989 import with the split audio and
  the hole), "vlog otomatik 2 (24 fps)" (one clip turned by hand), "vlog otomatik 3 (donuk)"
  (the hand-edited FCPXML), "vlog otomatik 4", "turn test 1 / 2". Nothing was deleted.
- The connected MCP server still runs the old code; the new tools were driven from Python
  against the same bridge.

# Phone HDR clips look pale in Resolve: a built-in conversion

Emir's vlog clips are iPhone HDR (HLG, BT.2020). In a Resolve project without colour management
their code values are shown as Rec.709: pale, bright, grey sky. Reference for "how it should
look": Apple's own SDR conversion of the same files (`avconvert`), eight frames of three
sources, five to fit on and three held out. Measured against it (PSNR, same frames):

- as Resolve shows them: 17.3-20.2 dB
- Resolve colour managed (RCM v2, HLG input, Rec.709 out, DaVinci tone mapping): 13.1-13.7 dB,
  brighter still; "Saturation Preserving" gets the colour but not the brightness
- one CDL (slope 0.90, power 1.22, saturation 1.62), applied in Resolve: 28.7-29.7 dB
- undo HLG, BT.2020 -> BT.709, power 0.40, saturation 1.14 (what a LUT can hold): 35.4-39.3 dB

- [x] `luts.hlg_to_rec709_cube()`: the conversion as a .cube, written without numpy
- [x] `apply_lut(lut_path="hlg-to-rec709")`: the built-in LUT, only on clips Resolve itself
      tags Rec.2100 HLG; the others are counted as skipped
- [x] `probe_media` / `scan_folder` say `hdr`; `export_fcpxml` says what to do after the import
- [x] `render_preview` shows HLG sources through the same LUT (ffmpeg `lut3d`), or says it does not
- [x] Guide `resolve-phone-hdr`; one-prompt guide step; README, CHANGELOG, verified matrix
- [x] Tests red without each change; measured in Resolve against Apple's frames; full pytest + ruff

## Review

428 passed (411 before), ruff clean. 32 mutations over the probe, the LUT maths, the cube file, the Lua snippet,
the Studio path, the tool, scan_folder / export_fcpxml and the preview: the tests went red each
time. One stayed green at first (an input clamp nothing could reach); the clamp was removed.

Measured against Apple's SDR conversion of the same eight frames (five fitted, three held out):

| | PSNR |
|---|---|
| Resolve 20.3, unmanaged project, untouched | 17.3-20.2 dB |
| Resolve colour managed, DaVinci tone mapping | 13.1-13.7 dB |
| one CDL through grade_clips, in Resolve | 28.7-29.7 dB |
| the built-in LUT through apply_lut, in Resolve | 36.8-40.7 dB |
| the real preview (ffmpeg lut3d), 9,119 frames, rendered in 369 s | 34.4-37.0 dB |

In Resolve through the tool: "vlog otomatik 4" 198 applied, 0 skipped; "rp cut 1" (ordinary
H.264 clips) 0 applied, 9 skipped, with the note. The first preview chain put the LUT before the
downscale and took 623 s (with tests running beside it); after the downscale a 6 s piece takes
2.1 s instead of 5.8 s.

- One iPhone 14 Pro, daylight. Night footage, other phones and Dolby Vision trims not tried.
- A project that is colour managed would convert twice; the guide says so, the tool does not check.
- HDR10 (PQ) is recognised and reported, not converted.
- The test project's colour settings were changed while trying and put back (read back:
  davinciYRGB, Rec.709 (Scene), no DRT). Every clip of "vlog otomatik 4" now has the LUT on node 1.
- Nobody has watched the corrected preview or timeline play; eight stills were compared and
  three before / after / phone rows looked at.
