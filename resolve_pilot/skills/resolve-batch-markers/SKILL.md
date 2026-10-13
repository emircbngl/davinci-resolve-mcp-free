---
name: resolve-batch-markers
description: Place markers on a Resolve timeline in bulk — from beats, scene cuts, transcript keywords or a user list — with add_markers, which runs live through the Studio API or the bridge and falls back to paste-able Lua in the Free edition. Use for any "add markers at X" or "tag the timeline" request.
---

# resolve-batch-markers

Places markers on the current timeline in DaVinci Resolve, Free or Studio.

## When to use

- "Mark every beat in this music track"
- "Add a marker on every scene change"
- "Flag every time the speaker says 'um'"
- "Put a marker every 30 seconds for chapters"

## Workflow

1. **Know the timeline.** If live, `get_resolve_state(include_clips=true)` gives the current timeline, its fps and where each clip sits (`start_seconds`, `source_in_frame`).
2. **Compute positions in timeline seconds** (offsets from the timeline start):
   - Analysis of a *source file* (scene cuts, transcript, beats) is in **source time**. If the timeline is that file placed once at the start, the times are the same. Otherwise map them: `timeline_s = clip.start_seconds + (source_s − clip.source_in_frame / fps)` for the clip that contains the moment.
   - Chapters every N seconds: `0, N, 2N, …` up to `duration_seconds`.
   - Timecodes `HH:MM:SS:FF`: `(HH×3600 + MM×60 + SS) + FF / fps`, minus the timeline start timecode (usually 01:00:00:00).
3. **Add them**:
   ```
   add_markers(markers=[
     {"seconds": 12.5, "color": "Blue", "name": "Chapter 2", "note": "intro ends"},
     {"seconds": 47.0, "color": "Yellow", "name": "Check audio"}
   ])
   ```
   Use `frame` instead of `seconds` when you already have frame offsets. `duration` is in frames.
4. **Relay the result.**
   - `channel` studio / bridge: report `added` / `requested`. `skipped_frames` already had a marker (or were out of range).
   - `channel` manual: give the user `lua_code` and `instructions` verbatim ("Workspace › Console › Lua tab, paste, Enter"). The Console prints `[resolve-pilot] Added N / N markers`.
5. **Verify** when live: `get_resolve_state(include_markers=true)`.

## Quality markers

To flag problems in a recording (silence, black, frozen picture, loud or quiet audio):
```
add_quality_markers(path="/abs/interview.mov", kinds=["silence", "black", "freeze"], min_seconds=1.0)
```
It analyzes the file (or takes `spans` you already got from `analyze_quality`) and places colored
markers with their length: silence Blue, black Cocoa, freeze Lavender, loud Red, quiet Sky. The
spans are file times; when the clip does not start the timeline at its first frame, pass
`timeline_start_seconds`, `source_in_seconds` (and `source_out_seconds`) from
`get_resolve_state(include_clips=true)`. Re-running replaces the markers it placed for that file
earlier and never touches other markers (`replace_previous=true`).

## Marker colors

Blue, Cyan, Green, Yellow, Red, Pink, Purple, Fuchsia, Rose, Lavender, Sky, Mint, Lemon, Sand, Cocoa, Cream.

Suggested convention:
- **Blue**: structure (chapter starts)
- **Green**: keepers / good takes
- **Yellow**: needs human review
- **Red**: problem (fix audio, cut)
- **Purple**: music sync points

## Failure modes

- `ok: false, error: "No active timeline"` → ask the user to open the timeline on the Edit page.
- Every marker skipped → markers already exist at those frames (re-run) or positions are past the end of the timeline; compare with `duration_seconds`.
- Drop-frame timecode (29.97 / 59.94 DF): timecode math is approximate; prefer seconds.
