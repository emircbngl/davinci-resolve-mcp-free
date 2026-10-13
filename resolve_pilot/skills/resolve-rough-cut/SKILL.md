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
5. **Draft** with `plan_from_transcript(transcript_id, segment_indices=[12, 3, 4, 40], title=...)` — indices in playback order. Consecutive segments join seamlessly; no words are repeated.
6. **Refine with `edit_plan`** — never rebuild the plan for small changes:
   ```json
   [{"op": "trim", "index": 2, "head_seconds": 0.15},
    {"op": "move", "index": 5, "to": 0},
    {"op": "update", "index": 3, "rationale": "best answer to 'why now'"},
    {"op": "set", "fade_in_seconds": 0.5, "fade_out_seconds": 1.0}]
   ```
   Fix everything in `errors`; read every `warnings` entry (clip past the end of its source, dissolve without handles, reused footage).
7. **`export_fcpxml(plan_id)`** → `path`.
8. **Deliver**
   - studio / bridge: `import_timeline(path, timeline_name="...")`, then `get_resolve_state(include_clips=true)` and check clip count and duration against the plan.
   - manual: "In Resolve: File › Import › Timeline… and pick `<path>`."
9. **Iterate live** (studio / bridge) on the imported timeline instead of exporting again, which would create another timeline: `append_clips` adds a missed line or a B-roll shot (`track=2`, `record_seconds=...`), `remove_clips` drops one (read the indices with `get_resolve_state(include_clips=true)` and ask first). Keep the plan in step with what you changed if you may export again.
10. **Reply** with a short table — #, source in–out, duration, rationale — and the total length.

## Conventions

- Always fill `rationale`; it is how the user audits your choices.
- `pad_seconds=0.2` keeps natural breaths; use 0.05 for punchy social cuts.
- The timeline fps follows the source unless the user asks otherwise.
- Vertical / social: `edit_plan` with `{"op": "set", "width": 1080, "height": 1920}` and tell the user the reframing happens in Resolve (Smart Reframe in Studio, or manual).
- Ask before transcribing anything over an hour and quote the expected time (turbo runs many times faster than real time on Apple Silicon, slower on CPU).
- Multi-camera or several files: `create_plan` takes clips from any number of sources.

## Example

User: *"I have a 25-min interview at `~/Footage/jane.mp4`. Give me a 4-minute highlight cut of her best lines."*

1. `probe_media` → 1080p30, 25:13, AAC audio.
2. `transcribe_audio` → `status: running`; call again → 212 segments (`transcript_id: t-…`).
3. Read the segments, choose 15 (≈240 s total).
4. `plan_from_transcript(transcript_id, segment_indices=[…], title="Jane — 4 min")`.
5. `edit_plan`: trims on two clips, `set` fades. Warnings: none.
6. `export_fcpxml` → `~/Footage/Jane_4_min.fcpxml`; `import_timeline` (bridge) → 15 clips, 4:02.
7. Reply with the table and the timeline name.

## Failure modes

- Warning "reuse … of the same footage": two clips overlap in the source. Intentional? Otherwise trim.
- `probe_media` shows `fps: null` (audio-only or unusual container): pass `fps` explicitly and tell the user.
- `import_timeline` returns `ok: false`: a timeline with that name probably exists; pass another `timeline_name`.
- Media offline after import: the FCPXML points at absolute paths; files moved since export need relinking (`relink_media` snippet).
