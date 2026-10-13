# Changelog

## Unreleased

Focus: the agent can *look* — at footage, and at Resolve itself — and find problem stretches.

Verified live on Resolve 20.3 (Free, macOS): screenshot_resolve, grab_frames, scene_frames, read_video, analyze_quality and add_quality_markers (including re-runs replacing only their own markers).

### Added
- **Seeing footage** — results are images the model looks at, on mcp 1.x and 2.x:
  - `grab_frames`: frames at given times, separately or as one contact sheet (`layout="grid"`).
  - `scene_frames`: one representative frame per detected scene (spread evenly over long videos).
  - `read_video`: each stretch of speech next to the frame on screen while it is said, with the transcript segment indices `plan_from_transcript` takes. Reuses the transcript cache (`path` or `transcript_id`).
  - Timestamps that cannot be served (past the end, over `max_frames`) are reported in `dropped` / `missing_frames` with a reason instead of failing or silently disappearing.
- `analyze_quality`: silence, black, frozen picture, and audio that is loud or quiet *for its surroundings* (rolling local median), with kind / length filters.
- `add_quality_markers`: those spans as colored timeline markers, through the same Studio API → bridge → paste-Lua routing as `add_markers`, mapped through the clip's timeline position and source in/out. Re-running replaces only the markers it placed for that file. `add_markers` rows (and the `add_markers` snippet) accept `duration_seconds`, `custom_data` and a `replace_owned_prefix`.
- `screenshot_resolve` (macOS): the Resolve window as an image. Missing Screen Recording permission, minimized windows or a missing Quartz module (`.[ui]` extra, optional) give `ok: false` with what to do — never a blank or stale picture.
- `resolve_pilot_status` reports `see_footage` and `resolve_screenshots` capabilities.

### Fixed
- macOS UI automation no longer types or clicks into another app: `send_keystroke`, `menu_pick` and the page shortcuts act only after verifying Resolve is frontmost, keystrokes are scoped to Resolve's process, unknown modifier names are rejected (`opt` / `ctl` accepted) and menu labels with quotes are escaped. Dialog-opening AI features report `completed: false` plus the step the user still has to take.
- `probe_media` can no longer hang forever on an unreadable file.

### Tests
- Static check that the bridge and every snippet read no undefined Lua globals (a deleted variable still referenced) and leak none; a stale stop file must not end a freshly started bridge.
- Frames, contact sheets, scene sampling, read_video pairing and quality detectors against real ffmpeg on generated media; marker replacement through the live bridge; images over a real MCP client session.

### Added
- `timeline_frames`: see the edit as Resolve renders it — grades, LUTs, titles, transitions — on any OS. Exports stills of the current timeline at given seconds or frames (drop-frame aware), returns them like `grab_frames` (separate or as a contact sheet), and puts the playhead and page back. Verified live on Resolve 20.3 Free.
- `render_status`: every render job's status, percent and time left, and whether Resolve is rendering; `wait_seconds` polls until it is done.

- **Live timeline building**, without an FCPXML round trip (which always creates a new timeline), through the same Studio API → bridge → paste-Lua routing:
  - `import_media(paths, bin)`: files into the media pool, into a bin (`"B-roll/Day 1"`, created when missing). Files already in that bin are reused, not imported twice. Returns each item's name, `media_pool_id` and `already_in_pool`.
  - `append_clips(clips, timeline | new_timeline, new_timeline_fps)`: source ranges in source seconds (converted with the clip's own fps) onto the current, a named or a new empty timeline (a name clash is refused), on any track (missing tracks are added), video, audio or both, optionally at a record position. Files not in the pool are imported on the fly. Returns the timeline items that landed, with `warnings` where the length, position or track differ from the request.
  - `remove_clips(indices, track, names, ripple)`: destructive; deletes by the indices `get_resolve_state` lists, refusing when the `names` read beforehand no longer match.
  - Based on API behaviour measured with Python on Resolve 21.0.4 Free (plain path strings for ImportMedia, exclusive endFrame, DeleteClips only on the Edit page); not yet verified from Lua on a real install.

### Resolve 21.1+ Free
- The bridge no longer needs `io`, `os.execute`, `package` or `debug`, which Resolve 21.1+ Free removed from its Lua. When `io` is missing it reads and writes its files through LuaJIT's `ffi` (a global in Resolve), takes the path separator from the MCP server, and the server creates the bridge folder. `rp.file_access` says which one is in use. Tests run the bridge in an emulated 21.1 sandbox. Not yet tried on a real 21.1 install.
- `setup_bridge(install)` returns `console_command`, a one-line `dofile(...)` to paste into the Console when the Scripts menu entry does nothing, and lists older resolve-pilot bridge scripts left in the same menu.
- `resolve_pilot_status` reports `resolve_version` (macOS) and, on 21.1+, how to start the bridge from the Console.

### Permissions (macOS)
- Accessibility and Screen Recording hints now name the app macOS actually checks. Claude Desktop starts MCP servers through a helper that makes the Python interpreter (Python.app) responsible, so "allow Claude" never worked for `screenshot_resolve`.

### Fixed (found in the live run)
- `run_lua`: request code could not see the `rp` helpers and its `print()` output was lost, because Resolve runs the bridge script in its own environment while `loadstring` compiles against the real globals. Requests now get an environment with both; globals they set still persist between requests. Tests now run the bridge in a separate script environment like Resolve does.
- `analyze_quality` flagged sub-dB wobble on a steady tone as "loud": loud/quiet now also need a 6 dB departure from the local level.
- Runs of the same kind were split when a span of another kind sat between them; merging is now per kind.
- Freeze spans inside black and quiet spans inside silence are dropped (they repeat the stronger finding).
- `add_quality_markers`: spans starting on the same frame become one marker ("Black + Loud") instead of Resolve skipping the second.
- `read_video` on an audio-only file failed with an ffmpeg error per segment; it now returns the speech with a note that there are no frames.

## 0.2.0

Focus: an agent connected to resolve-pilot can now *see* Resolve, act on it, verify the result and iterate — in the Free edition too — while spending fewer tokens.

### Fixed
- **Fresh installs crashed on start.** `mcp>=1.27` now resolves to mcp 2.x, which removed `mcp.server.fastmcp`. The server supports both 1.x and 2.x (`mcp>=1.27,<3`).
- With mcp 2.x, tool errors reached the agent as a bare "Error executing tool X". Every tool now reports what went wrong and what to do next.
- `detect_beats` failed on every file: piped WAV output has no valid size header. Audio is now decoded as raw PCM (and the onset pass is faster).
- `detect_scenes` reported "0 cuts" when ffmpeg could not read the file; it now raises the ffmpeg error.
- Lua snippets embedded paths and names unescaped, so a quote or `]]` broke the Lua (or injected code). All values are now encoded safely, and `~` is expanded before reaching Resolve.
- Transcript rough cuts repeated words: padding made neighbouring clips overlap in the source. Boundaries are now placed between segments.
- FCPXML: clip boundaries are rounded to frames cumulatively (no one-frame gaps or drift over many short cuts); asset URIs are proper `file://` URIs (spaces, `#`, non-ASCII, Windows paths); audio-only assets are declared as such.
- Verified live on Resolve 20.3 (Free, macOS) through the bridge, which turned up:
  - `transition_out_seconds: 0` on a clip was ignored when the plan had a default dissolve, so beat cuts could not be hard cuts. Omitted now means "plan default" and `0` means a hard cut.
  - Timelines read back with transitions counted as clips (`GetItemListInTrack` returns dissolves as items), and `set_clip_color` / `apply_lut` touched them. Transitions are now filtered out, and `get_resolve_state` reports `transition_count`.
  - `apply_lut` applied nothing: Resolve only accepts LUTs inside its LUT folder and returns false for any other path. LUTs are now copied into `<LUT folder>/resolve-pilot/` first and the LUT list is refreshed; if Resolve still rejects every clip the tool returns `ok: false`.
  - Lists from the Resolve API (e.g. `available_presets`) reached the agent as `{"1": ..., "__flags": ...}` objects; the bridge now encodes them as JSON arrays.
  - Plan validation warns when a dissolve leads into a clip without audio: Resolve imports that clip wrapped in a compound clip.

### Added
- **Live Lua bridge** (`resolve_pilot/bridge`): a small Lua loop started from Workspace › Scripts (installed by `setup_bridge`) or pasted into the Console. Request ids, JSON results, captured `print` output, Lua tracebacks, idle timeout, stop command, "newest bridge wins", `rp.*` helpers. Works on macOS, Windows and Linux.
- **Auto-routed Resolve actions** — `get_resolve_state`, `import_timeline`, `add_markers` (seconds or frames), `set_clip_color`, `apply_lut`, `queue_render`, `open_page` — via Studio API → bridge → paste-able Lua, reporting the `channel` used. `run_lua` runs any Lua (or a library snippet) live.
- **Server-side plans**: `create_plan` (multi-source, music bed), `get_plan`, `edit_plan` with `keep / remove / move / trim / update / insert / set` operations (all-or-nothing). Every plan response includes validation `errors` and `warnings`.
- **Music bed** in plans and FCPXML (connected clip on its own audio track) — the music-video workflow needed it.
- **Transcripts**: cached per file/model/language, numbered compact segments with paging, background jobs that survive client timeouts, `plan_from_transcript(transcript_id, segment_indices=[…])`, and a faster-whisper engine for Windows / Linux / Intel Macs (`.[transcribe-cpu]`).
- `export_srt` wraps lines and splits long cues (`max_chars_per_line`, `max_lines`).
- `detect_beats` returns a tempo estimate; `detect_scenes` can cap results to the strongest cuts.
- Server instructions for agents, tool annotations (read-only / destructive hints) and titles, typed parameters instead of JSON-in-a-string.
- Workflow guides served over MCP (`get_workflow_guide` + one prompt per guide); new **resolve-live-control** guide with a Resolve Lua API cheat sheet. All guides updated to the new tools.
- `list_lua_snippets` lists each snippet's parameters; new snippets `add_markers`, `get_state`, `import_timeline`, `render_current_timeline`, `open_page`. Snippets `return` structured results.
- Tests: the real bridge under LuaJIT against a mock Resolve, an end-to-end MCP client session, plans, validation, exporters. CI on Linux (Python 3.11/3.13 × mcp 1.x/2.x) and macOS.

### Changed — tool names
Tools were renamed and consolidated (fewer, clearer tools help agents choose):

| 0.1 | 0.2 |
|---|---|
| `probe_media_tool`, `detect_scenes_tool`, `detect_beats_tool` | `probe_media`, `detect_scenes`, `detect_beats` |
| `plan_roughcut_from_transcript`, `plan_roughcut_from_scenes` | `plan_from_transcript`, `plan_from_scenes` |
| `apply_default_transitions` | `edit_plan` op `set` |
| `export_fcpxml(plan_json, …)` | `export_fcpxml(plan_id, …)` |
| `export_srt_from_transcript` | `export_srt` |
| `render_lua_snippet(args_json=…)` | `render_lua_snippet(args={…})`, or `run_lua(snippet=…)` live |
| `studio_probe`, `is_resolve_running_tool` | `resolve_pilot_status` |
| `studio_project_summary`, `studio_list_timelines`, `studio_timeline_clips` | `get_resolve_state` |
| `studio_add_marker` | `add_markers` |
| `studio_set_clip_color`, `studio_apply_lut`, `studio_queue_render` | `set_clip_color`, `apply_lut`, `queue_render` |
| `open_page_tool` | `open_page` (bridge / Studio API, or macOS shortcut) |
| `send_keystroke_tool`, `menu_pick_tool`, `apply_video_transition_tool` | `send_keystroke`, `menu_pick` (Cmd+T via `send_keystroke`) |
| `focus_resolve_tool` | dropped (the UI tools focus Resolve themselves) |
| `trigger_magic_mask`, `trigger_*_tool` | `open_ai_feature(feature=…)` |

## 0.1.0

Initial release: FCPXML export, Lua console snippets, macOS UI automation, local transcription, scene detection.
