---
name: resolve-one-prompt-edit
description: Turn a folder of talking footage (a vlog, a trip, a day of clips) into one cut from a single request — intake, clean each clip, assemble in order, watch and check the result without Resolve, captions, deliver. Use for "make a video from this folder", "edit my vlog", "cut these clips together".
---

# resolve-one-prompt-edit

One request in, one watched and checked cut out. The user should not have to steer between the steps: decide, check your own work, and report what you decided and what you could not verify.

## When to use

- "Make a vlog from the clips in this folder"
- "Cut these together and tidy up the talking"
- "Edit my trip footage, add captions"

For one long file (an interview, a talk) use `resolve-rough-cut`; for footage cut to music, `resolve-music-video`.

## Before you start

Read the request for what it fixes: target length, what must be in or out, language, captions or not, horizontal or vertical. Ask only when a wrong guess would waste the whole run (which folder; a hard length limit). Everything else: decide, and say what you decided in the reply.

## Workflow

1. **`resolve_pilot_status`** once. The whole run up to the preview needs no Resolve.
2. **Intake: `scan_folder(folder, transcribe=true, language=...)`** — every file in recording order, with length, frame, rotation tag and its transcript. On `next_step: still transcribing` call it again with the same arguments. Note files that are `failed` or `unreadable` and say so in the reply.
3. **Look before you cut.** For each file: `transcribe_audio(path)` returns the cached transcript with numbered segments; `grab_frames(path, at_seconds=[...])` or `scene_frames(path)` shows what is in the picture.
   - A file with a `rotation` tag: look at one frame. If the picture is on its side (a phone held sideways but tagged upright; common in selfie shots), remember it for step 6.
   - Files with no speech worth keeping (a few words, wind, a song playing) are B-roll or nothing: leave them out of the talking cut and say so.
4. **Decide per file** what stays. Complete thoughts, the better of two takes, the moment something happens. `scan_speech(transcript_id)` lists the mechanical candidates: `retakes` (keep one of the two segments), `repeats` and `fillers` (each with a `drop` value), `long_pauses`. Candidates, not decisions: a repeated word can be emphasis. Whisper sometimes invents a line in silence ("Thanks for watching", "Subtitles by…", often as the last segment): leave those segments out.
5. **Assemble in playing order** (recording order unless the story wants another):
   - first file: `plan_from_transcript(transcript_id, segment_indices=[...], drop_words=[...], max_pause_seconds=0.5, title=...)` → `plan_id`
   - every next file: the same call with `append_to=plan_id`
   - Each reply has a `cut_report`; `tight` cuts had no pause to sit in.
   - The plan's frame and frame rate are the first file's. If most of the footage is vertical, `edit_plan [{"op": "set", "width": 1080, "height": 1920}]`.
6. **Turn what is on its side**: `edit_plan [{"op": "turn", "source": 3, "degrees": 270}]` — `source` is the index in the plan's `sources`, degrees are clockwise (heads pointing right: 270; pointing left: 90). The preview shows it; check the next render.
7. **Watch and check: `render_preview(plan_id)`** (call again while `status: running`). Then work through `issues`:
   - `cut_in_sound`: `trim` the clip a little at that side, or drop the neighbouring word instead.
   - `flash_clip`: remove it, or keep it if it is a real one-word answer.
   - `level_jump`: usually another speaker or distance; note it for the user (levels are not evened out yet).
   - `silence`, `black_picture`, `frozen_picture`: remove or trim the clip.
   - `rotated_source`: look at a frame of that clip, then step 6.
   - Look at a few cuts yourself: `grab_frames(preview_path, at_seconds=[...])`. The preview's times are timeline times.
   - Fix with `edit_plan`, render again, until what is left is what you chose to keep. Stop after three rounds and report what remains.
8. **Captions** when asked: they are already in the preview as a subtitle track (the plan knows its transcripts). `export_srt(plan_id=...)` writes the file.
9. **Deliver**
   - Always: the preview `path` for the user to watch, before anything touches Resolve.
   - Into Resolve when it is live (studio / bridge) and the user wants it: `checkpoint(action="save")` if a timeline is open, `export_fcpxml(plan_id)`, `import_timeline(path)`, verify with `get_resolve_state(include_clips=true)`, then `add_subtitles(plan_id=...)`. Read `export_fcpxml`'s `warnings`: turned sources have to be turned in Resolve too (`set_clip_properties`), check with `timeline_frames`.
   - Not live: give the `.fcpxml` path and "File › Import › Timeline".
10. **Reply** short: what the video is now (length, how many clips from how many files), what you cut and why in one or two lines, what you left out, the issues that remain, and what you did not verify (nobody listened to the cuts; Resolve import not done). Then the preview path.

## Rules

- Never compute cut points from Whisper's word times yourself; `plan_from_transcript` measures the pauses in the audio.
- Do not rebuild a plan to change it; `edit_plan`.
- The preview is the evidence. Do not tell the user a cut is clean because the plan has no warnings: say what `issues` listed and what you looked at.
- Nothing destructive in Resolve without asking; importing a timeline is not destructive, removing clips is.

## Example

User: *"Make a vlog out of ~/Trip/day2, Turkish captions."*

1. `scan_folder("~/Trip/day2", transcribe=true, language="tr")` → 11 files, 14 min; again → all transcribed, 1 file has 4 words.
2. Frames of the 8 files with a rotation tag: 6 are on their side.
3. Per file: `scan_speech`, choose segments; first `plan_from_transcript(...)`, then 9 more with `append_to` → 310 clips, 9:12.
4. `edit_plan` with six `turn` operations.
5. `render_preview` → 9:12, issues: 16 `cut_in_sound`, 30 `level_jump`, 11 `flash_clip`. Trim 9 cuts, remove 6 flash clips, render again → 4 `cut_in_sound` left.
6. Reply: 9:04 from 10 files, fillers and retakes removed, one file left out (4 words), levels differ between speakers, 4 cuts worth a listen, not imported into Resolve. Preview: `…/previews/day2-….mp4`.

## Failure modes

- "No Whisper engine installed" → `pip install -e '.[transcribe]'` (Apple Silicon) or `'.[transcribe-cpu]'`.
- `render_preview` fails naming a clip → that source cannot be read at that range; `edit_plan` the clip or leave the file out.
- A plan error "shorter than one frame" → remove that clip.
