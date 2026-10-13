---
name: resolve-music-video
description: Cut a music video to the beat — detect beats in the song, pick shots from a footage pool, build a plan whose cuts land on beats with the song as a music bed, export FCPXML, import it and drop beat markers. Use for "edit this music video", "cut to the beat", "sync clips to BPM".
---

# resolve-music-video

Cuts footage so clip changes land on the beats of a song, with the song laid under the picture.

## When to use

- "Cut this footage to the beat of `song.mp3`"
- "Make a 1-minute hype reel synced to the drops"
- "Sync my B-roll to the music"

## Inputs

1. **The song** — audio or video file with the music.
2. **The footage pool** — one or more video files.
3. **Optional**: target length, orientation, energy (every beat / every 2nd / drops only).

## Workflow

### 1. Analyze the music
```
probe_media(song)
detect_beats(song, min_seconds_between=0.18)
```
`beats` is a list of `[seconds, strength]`; `tempo_bpm_estimate` is a rough guide.

### 2. Pick the cut grid
- **Every beat** (fast, energetic): strength > 0.3.
- **Half-time** (smoother): every other beat.
- **Drops only** (cinematic): strength > 0.65 (or pass `min_strength`).

**Snap the grid to frames first** to avoid drift: with timeline fps `F`, `f_i = round(t_i × F)`; each clip lasts `(f_{i+1} − f_i) / F` seconds.

### 3. Analyze the footage
For each file: `probe_media(file)` and `detect_scenes(file, threshold=15)`; `scene_frames(file, threshold=15)` shows what each scene looks like. Build a shot list of `(source_path, in, out)` ranges between scene cuts so you never cut mid-action. Prefer high-score scenes (more visually distinct).

### 4. Assemble
For each interval of the grid, pick a shot at least as long as the interval (prefer unused shots), and take `duration_seconds = interval` from a strong point in it. Then:
```
create_plan(
  title="Song — beat cut",
  fps=<footage fps>,
  clips=[{"source_path": ..., "in_seconds": ..., "duration_seconds": ..., "rationale": "beat 12, drop"}, ...],
  audio_bed={"source_path": song, "in_seconds": <grid start in the song>}
)
```
The first clip starts at the first grid beat, so set the bed's `in_seconds` to that beat time. The bed becomes its own audio track under the picture.

With a fade-in, the first clip starts `fade_in_seconds` into the timeline, so also set the bed's `offset_seconds` to `fade_in_seconds`; otherwise every cut lands that much off the beat. (`offset_seconds` is the timeline position, `in_seconds` the position inside the song.)

Check `warnings`: "reuse … of the same footage" means repeated shots; "audio bed … ends early" means the song is shorter than the cut.

### 5. Export and deliver
`export_fcpxml(plan_id)` → `import_timeline(path)` when live (studio / bridge), otherwise File › Import › Timeline. The footage's own audio sits on A1 — suggest muting A1 if it should not play.

### 6. Beat markers (optional)
After importing, mark the grid on the timeline (timeline time = song time − bed `in_seconds` + bed `offset_seconds`):
```
add_markers(markers=[{"seconds": 0.0, "color": "Purple", "name": "Beat 1"}, ...])
```
Live channels add them directly; `manual` returns Lua for the user to paste.

## Pacing rules of thumb

- **EDM / pop** (120–140 BPM): every beat in choruses, every 2nd in verses.
- **Hip-hop** (80–95 BPM): every 2nd beat or kicks only; every beat feels jittery.
- **Cinematic / orchestral**: only the hits (strength > 0.7).

## Failure modes

- Fewer than ~10 beats in a minute of music: noisy or very soft track; lower `energy_threshold_db` (e.g. −35) or ask the user for the BPM and compute the grid (`60 / bpm` seconds per beat).
- Footage fps differs from the plan fps: validation warns; keep the plan at the footage fps.
- Not enough distinct shots for the grid: use half-time or let shots repeat on purpose (say so in `rationale`).
