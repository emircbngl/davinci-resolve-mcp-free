# Changelog

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
