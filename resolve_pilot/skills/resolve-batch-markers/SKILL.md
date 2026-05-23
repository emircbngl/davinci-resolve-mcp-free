---
name: resolve-batch-markers
description: Place markers on a Resolve timeline in bulk — from beat detection, scene cuts, transcript keywords, or a user-supplied list. Generates a Lua snippet the user pastes into Resolve's Console. Use for any "add markers at X" or "tag the timeline" request.
---

# resolve-batch-markers

Places markers on the currently active timeline in DaVinci Resolve. Works in **Free** edition because it uses the built-in Console.

## When to use

- "Mark every beat in this music track"
- "Add a marker on every scene change"
- "Flag every time the speaker says 'um'"
- "Put a marker every 30 seconds for chapters"

## Workflow

1. **Figure out frame positions** for the markers. Possible sources:
   - Scene cuts: `detect_scenes_tool(path)` → multiply each `pts_seconds × timeline_fps`.
   - Transcript keyword hits: scan `transcribe_audio` segments for the word, multiply `start × fps`.
   - User-given timecodes: convert `HH:MM:SS:FF` to frames using fps.
   - Music beats: use scene detection on the waveform OR ask the user for BPM and compute `60/bpm × fps` per beat.
2. **Generate the Lua snippet** with `render_lua_snippet`:
   ```
   render_lua_snippet(
     snippet="batch_markers",
     args_json=json.dumps({
       "frames": [24, 120, 240, ...],
       "color": "Blue",
       "name_prefix": "Beat",
       "note": "auto-generated"
     })
   )
   ```
3. **Hand the user the code** with this instruction:
   > "Open DaVinci Resolve, make sure your timeline is the active one, then go to `Workspace → Console`, paste this Lua, and press Enter. You'll see a confirmation like `[resolve-pilot] Added 47 / 47 markers`."

## Marker colors

Available: Blue, Cyan, Green, Yellow, Red, Pink, Purple, Fuchsia, Rose, Lavender, Sky, Mint, Lemon, Sand, Cocoa, Cream.

Convention I follow:
- **Blue**: structural beats (chapter starts)
- **Green**: good takes / keepers
- **Yellow**: review (needs human attention)
- **Red**: problem (cut, fix audio, etc.)
- **Purple**: music sync points

## Frame math reminder

- Frames from seconds: `int(round(seconds * fps))`.
- Timecode `HH:MM:SS:FF` (drop-frame): use `(HH*3600 + MM*60 + SS) * fps + FF` — for NTSC drop-frame (29.97, 59.94), this is approximate and Resolve will snap.
- The current timeline's fps is in the user's project settings; ask if unsure, or have them run the `dump_project_info` snippet first.

## Failure modes

- If the user doesn't have a timeline open, the snippet prints `No active timeline.` — tell them to switch to the Edit page first.
- If markers don't appear: the timeline cursor must be active. Run `dump_project_info` to confirm.
