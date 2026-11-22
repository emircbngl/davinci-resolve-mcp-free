---
name: resolve-rough-cut
description: Build a rough cut from raw footage — transcribe, pick the best segments or scenes, refine a stored plan, export FCPXML and (when live) import it into DaVinci Resolve Free or Studio. Use for "first cut", "rough cut", "highlight reel", "make a 4-minute version" requests.
---

# resolve-rough-cut

Builds a first-pass timeline from raw footage, deciding what to keep from the transcript and the scene structure, and delivers it to Resolve as a timeline.

## When to use

- The user has raw footage and asks for a "first cut" / "rough cut" / "draft edit" / "highlights"
- The user wants you to make editorial decisions, not just process clips

## Workflow

1. **`resolve_pilot_status`** once per session. Note `channel` (studio / bridge / manual).
2. **`probe_media(path)`** — fps, resolution, duration, whether there is audio.
3. **Pick the backbone**
   - Talking head, interview, talk → `transcribe_audio(path, language=...)`. If it returns `status: "running"`, call again with the same arguments. When `next_offset` is present, page with `offset` (cached calls are instant).
   - B-roll, montage → `plan_from_scenes(source_path=path)` (it detects scenes itself), or `detect_scenes` first if you want to inspect scores.
   - **Look before you choose.** `scene_frames(path)` shows one frame per scene as a contact sheet; `read_video(transcript_id=...)` puts each stretch of speech next to the frame on screen while it is said (labels carry the segment indices); `grab_frames(path, at_seconds=[...])` checks specific moments.
   - `analyze_quality(path, kinds=["silence", "black", "freeze"])` lists dead air and black / frozen picture to leave out.
4. **Decide editorially.** Read the numbered transcript (`[i] start-end text`):
   - keep complete thoughts and the strongest delivery; drop false starts, repeats, filler, dead air
   - pacing: 3–5 s clips for energetic edits, 8–15 s for documentary
   - keep a running total against the target length
   - **`scan_speech(transcript_id)`** lists the mechanical candidates: `retakes` (a line started twice: keep one of the two segments, usually the later), `repeats` and `fillers` (each with a `drop` value), `long_pauses`, and `likely_invented` (lines Whisper writes over music or silence, such as "Thanks for watching": leave those segments out). They are candidates: read each against the transcript before using it. `transcribe_audio(..., detail="words")` shows every word with its index (`12.3` = segment 12, word 3) when you want to cut something the scan did not list.
5. **Draft** with `plan_from_transcript(transcript_id, segment_indices=[12, 3, 4, 40], drop_words=["12.0", "40.3-5"], title=...)` — indices in playback order. Consecutive segments join seamlessly; no words are repeated.
   - Cuts are placed in the real pause next to each word (measured in the audio), never inside a word. Whisper's own word times are up to half a second off around pauses, so do not compute cut points from them yourself.
   - `max_pause_seconds=0.4` also tightens long pauses inside the lines you keep (talking-head jump cuts); leave it out for interviews and anything that should breathe.
   - Read `cut_report`: `tight` cuts had no pause to sit in (continuous speech, typically where you dropped a word mid-sentence). They can clip a syllable. Prefer dropping from pause to pause, or check those clips by ear / in Resolve.
6. **Refine with `edit_plan`** — never rebuild the plan for small changes:
   ```json
   [{"op": "trim", "index": 2, "head_seconds": 0.15},
    {"op": "move", "index": 5, "to": 0},
    {"op": "update", "index": 3, "rationale": "best answer to 'why now'"},
    {"op": "set", "fade_in_seconds": 0.5, "fade_out_seconds": 1.0}]
   ```
   Fix everything in `errors`; read every `warnings` entry (clip past the end of its source, dissolve without handles, reused footage).
7. **Watch and check it: `render_preview(plan_id)`** — the cut as a small MP4, without Resolve (on `status: running` call again). Its frames are the timeline's frames, so every time in it is a timeline time.
   - Read `issues`; each has its clip and timeline second. `cut_in_sound`: no dip in the sound at the cut, a clipped syllable or a click is likely (move the cut with `trim`, or drop from pause to pause). `level_jump`: the voice level steps across the cut (another speaker, another distance). `flash_clip`: under 0.4 s. `silence`, `black_picture`, `frozen_picture`: dead stretches. `fast_caption`: nobody reads that. `rotated_source`: the file is tagged as turned; look at a frame.
   - They are candidates, not verdicts. Look at the moments yourself: `grab_frames(path, at_seconds=[...])`, or `read_video(path)` for speech next to its frames. Fix with `edit_plan` and render again: one file per plan, replaced.
   - Tell the user the `path` so they can watch the cut before anything touches Resolve. `not_shown` lists what a preview leaves out (dissolves play as cuts).
8. **`export_fcpxml(plan_id)`** → `path`.
9. **Deliver**
   - studio / bridge: `import_timeline(path, timeline_name="...")`, then `get_resolve_state(include_clips=true)` and check clip count and duration against the plan.
   - manual: "In Resolve: File › Import › Timeline… and pick `<path>`."
   - Captions for the cut: `add_subtitles(plan_id=...)` (or `export_srt(plan_id=...)` for a file). They follow the plan's cut; never use the transcript's own times on a cut timeline.
10. **Iterate live** (studio / bridge) on the imported timeline instead of exporting again, which would create another timeline: `append_clips` adds a missed line or a B-roll shot (`track=2`, `record_seconds=...`), `remove_clips` drops one (read the indices with `get_resolve_state(include_clips=true)` and ask first). Keep the plan in step with what you changed if you may export again.
11. **Reply** with a short table — #, source in–out, duration, rationale — and the total length.

## Conventions

- Always fill `rationale`; it is how the user audits your choices.
- `pad_seconds=0.2` keeps natural breaths; use 0.05 for punchy social cuts. It is the pause left on each side of a cut, so two clips are never more than twice that apart.
- The timeline fps follows the source unless the user asks otherwise.
- Vertical / social: `edit_plan` with `{"op": "set", "width": 1080, "height": 1920}`, then reframe in Resolve (below; Smart Reframe in Studio does it automatically).
- Ask before transcribing anything over an hour and quote the expected time (turbo runs many times faster than real time on Apple Silicon, slower on CPU).
- Multi-camera or several files: `create_plan` takes clips from any number of sources.

## Vertical 9:16 delivery (reframe live)

After importing a 1080x1920 timeline built from 16:9 footage, the picture is letterboxed (Resolve fits the whole image by default). With a live channel:

1. `get_resolve_state(include_clips=true)` for the clip indices.
2. `set_clip_properties(properties={"Scaling": "fill"})` fills the frame and crops the sides, on every clip of V1 (or pass `indices`).
3. Put the subject in frame per clip: `set_clip_properties(indices=[2], properties={"Pan": -300})`; punch in with `{"ZoomX": 1.2, "ZoomY": 1.2}`.
4. `timeline_frames` at each changed clip and look; adjust and repeat. Report what you checked.

What is verified and what is not: keys, ranges and value kinds come from Resolve's scripting documentation, and every write is read back, so `ok: false` / `mismatched` is trustworthy. How a value *looks* is not verified yet: Pan/Tilt are documented in pixels of the timeline, but their sign (which way positive moves the picture), whether ZoomX alone moves ZoomY when ZoomGang is on, and how Scaling interacts with zoom are assumptions. Always confirm with `timeline_frames`.

Before big changes to an existing timeline, `export_timeline(format="drt")` makes a backup the user can bring back with `import_timeline`.

## Example

User: *"I have a 25-min interview at `~/Footage/jane.mp4`. Give me a 4-minute highlight cut of her best lines."*

1. `probe_media` → 1080p30, 25:13, AAC audio.
2. `transcribe_audio` → `status: running`; call again → 212 segments (`transcript_id: t-…`).
3. Read the segments, choose 15 (≈240 s total). `scan_speech` → 2 retakes (keep the later takes), 6 repeats, 4 fillers.
4. `plan_from_transcript(transcript_id, segment_indices=[…], drop_words=[…], title="Jane — 4 min")` → `cut_report`: 38 cuts, 36 in a pause, 2 tight.
5. `edit_plan`: trims on two clips, `set` fades. Warnings: none.
6. `render_preview` → `…/previews/jane-4-min-….mp4`, 4:02, issues: 1 `cut_in_sound` at 1:12 (clip 5), 2 `level_jump`. `grab_frames` there, `trim` clip 5 by 0.1 s, render again: clean.
7. `export_fcpxml` → `~/Footage/Jane_4_min.fcpxml`; `import_timeline` (bridge) → 15 clips, 4:02.
   Captions asked for: `add_subtitles(plan_id=...)` → 58 captions on the cut's own times.
8. Reply with the table, the preview path and the timeline name.

## Failure modes

- Warning "reuse … of the same footage": two clips overlap in the source. Intentional? Otherwise trim.
- `probe_media` shows `fps: null` (audio-only or unusual container): pass `fps` explicitly and tell the user.
- `import_timeline` returns `ok: false`: a timeline with that name probably exists; pass another `timeline_name`.
- Media offline after import: the FCPXML points at absolute paths; files moved since export need relinking (`relink_media` snippet).
