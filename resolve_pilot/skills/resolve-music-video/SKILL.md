---
name: resolve-music-video
description: Cut a music video to the beat. Detects beats in the audio track, picks clips from a footage pool, builds a beat-synced FCPXML, and exports markers for visual reference. Use for "edit this music video", "cut to the beat", "sync clips to BPM".
---

# resolve-music-video

Cuts a music video by aligning clip changes with detected beats in the audio.

## When to use

- "Cut this footage to the beat of `song.mp3`"
- "Make a 1-minute hype reel synced to drops"
- "Sync my B-roll to the music"

## Inputs

1. **The song** — path to an audio or video file with the music.
2. **The footage pool** — one or more video files, or a folder of clips.
3. **Optional**: desired output duration, vertical/horizontal, energy preference (every beat vs every 4th vs only drops).

## Workflow

### Step 1: analyze the music
```python
probe_media_tool(song_path)   # confirm format, duration
detect_beats_tool(song_path, min_seconds_between=0.18)
```
This returns a list of `{pts_seconds, strength}`. Strength 0..1 — keep the strongest 30–60% for a "drops only" feel, all of them for a snappy cut.

### Step 2: pick the cut grid
- **Every beat** (fast, energetic): keep all beats with strength > 0.3.
- **Half-time** (smoother): keep every other beat.
- **Drops only** (cinematic): keep beats with strength > 0.65.

Filter the beats list accordingly, building a list of cut points in seconds.

### Step 3: analyze the footage
For each footage file:
```python
probe_media_tool(file)
detect_scenes_tool(file, threshold=15.0)  # find "best moments" within each
```
Build a **shot library**: each shot is `(source_path, in_seconds, out_seconds, score)`. Score = scene cut score (higher = more visually distinct). Use scene boundaries to avoid mid-action cuts.

### Step 4: assemble the cut

For each beat in your filtered grid:
- The clip should run from `beat[i]` to `beat[i+1]` on the music timeline.
- Pick the shot whose duration is closest to that interval, preferring high-score shots.
- Mark used shots so you don't repeat unless you have to.

Build a `RoughCutPlan` with the music as the audio bed (a single full-duration audio clip on its own track) and the visual clips on the video track, lengths matching beat intervals.

### Step 5: export FCPXML
```python
export_fcpxml(plan_json, "~/Music/cut.fcpxml")
```
Tell the user to import and verify sync.

### Step 6 (optional): drop beat markers in Resolve
After the FCPXML is imported, generate a Lua snippet that places markers on every beat. The user pastes it in Workspace → Console:
```python
fps = plan.fps
frames = [int(round(b.pts_seconds * fps)) for b in beats]
render_lua_snippet(
    snippet="batch_markers",
    args_json=json.dumps({"frames": frames, "color": "Purple", "name_prefix": "Beat"})
)
```

## Pacing rules of thumb

- **EDM/Pop** (120–140 BPM): every beat works for fast sections, every 2nd for verses.
- **Hip-hop** (80–95 BPM): every 2nd beat or kick-pattern only — every beat feels jittery.
- **Cinematic/orchestral**: only "hit" moments — strength > 0.7 typically.

## Failure modes

- If beat strength varies wildly (loud chorus, quiet verse), the threshold needs to be relative — compute per-section if needed.
- Beats detected on speech can be wrong; tell the user if `detect_beats` returns < 10 beats in a 1-min song — usually means a noise floor issue.
- Always verify FPS match: if music is 44.1k audio and footage is 23.976, FCPXML must declare 23.976 to keep visual sync.
