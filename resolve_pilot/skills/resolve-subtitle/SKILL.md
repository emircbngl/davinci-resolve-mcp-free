---
name: resolve-subtitle
description: Generate subtitles for a video — transcribe locally with Whisper, write a properly wrapped SRT (optionally translated), and get it onto a Resolve subtitle track. Use for any "add captions" / "burn subtitles" / "subtitle this in Turkish" request.
---

# resolve-subtitle

Transcribes a video and produces an SRT ready for Resolve's subtitle track.

## When to use

- "Add subtitles / captions to this video"
- "Burn-in subtitles for social media"
- "I need an SRT for my talk"
- "Translate this video to English subtitles"

## Workflow

1. **`probe_media`** — confirm the duration and that `audio_codec` is not null.
2. **Pick a model** (`model` parameter): `large-v3-turbo` (default, best speed/quality), `large-v3` (slightly better, slower), `small` / `tiny` (quick preview).
3. **`transcribe_audio(path, language=...)`** — pass `language` when the user names it ("Turkish subtitles" → `"tr"`). On `status: "running"`, call again with the same arguments.
4. **Write the SRT** with `export_srt(transcript_id=..., max_chars_per_line=42, max_lines=2)`.
   - Vertical / social video: `max_chars_per_line=32`.
   - Default output: next to the source as `<name>.<language>.srt`.
5. **Translation**: read the segments, translate each one yourself keeping `start`/`end`, then `export_srt(segments=[{start, end, text}, ...], out_path=".../name.en.srt")`. An existing file is never replaced unless you pass `overwrite=true` — ask the user first, it may be their own subtitles.
6. **Into Resolve**: `add_subtitles(srt_path=...)` (or `transcript_id=...`) puts the captions on the current timeline's subtitle track at the SRT's own times and reads them back; it refuses to stack on existing captions unless `replace=true`. The SRT times count from the timeline start, so use it when the timeline plays the transcribed source from its start; for a rough cut, transcribe the cut itself. Without the bridge or Studio: tell the user to drag the `.srt` onto the timeline, or File › Import › Subtitle. Styling: Inspector › Captions.

## Quality rules

- At most 2 lines per cue; `export_srt` wraps lines and splits long cues, giving each part time proportional to its text.
- Cues between ~1 and 7 s read best; the result reports `average_cue_seconds` and `longest_cue_seconds` — if the longest is over 7 s, pass a smaller `max_chars_per_line`.
- Whisper sometimes drops end punctuation; fix it when translating or tidying.

## Example

User: *"Add Turkish subtitles to `~/Footage/talk.mp4`."*

1. `probe_media` → 22 min, has audio.
2. `transcribe_audio(path, language="tr")` → `status: running` → call again → 318 segments.
3. `export_srt(transcript_id, max_chars_per_line=42)` → `~/Footage/talk.tr.srt`, 401 cues, average 3.1 s.
4. `add_subtitles(srt_path="~/Footage/talk.tr.srt")` → 401 captions on the subtitle track, read back OK.

## Burning in

- In Resolve: put the SRT on a subtitle track, style it, and in Deliver › Subtitle Settings choose "Burn into video".
- Without Resolve: `ffmpeg -i talk.mp4 -vf subtitles=talk.tr.srt out.mp4`.

## Failure modes

- `audio_codec: null` → there is nothing to transcribe; tell the user.
- "No Whisper engine installed" → in the resolve-pilot checkout: `pip install -e '.[transcribe]'` on Apple Silicon, `pip install -e '.[transcribe-cpu]'` on Windows/Linux/Intel.
- Over an hour of audio → ask before starting and quote the expected time.
