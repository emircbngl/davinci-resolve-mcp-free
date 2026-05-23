---
name: resolve-subtitle
description: Generate burned-in or imported subtitles for a video. Transcribes audio with mlx-whisper, writes an SRT, and tells the user how to drag it onto a Resolve subtitle track. Use for any "add captions" / "burn subtitles" / "add subtitles to my video" request.
---

# resolve-subtitle

Transcribes a video and produces an SRT subtitle file ready to drop into Resolve's subtitle track.

## When to use

- "Add subtitles / captions to this video"
- "Burn-in subtitles for social media"
- "I need an SRT for my talk"
- "Translate this video to English subtitles"

## Workflow

1. **Probe** the media (`probe_media_tool`) — confirm duration and that it has audio.
2. **Pick a model**:
   - `mlx-community/whisper-large-v3-turbo` — best quality, ~realtime on M-series (default).
   - `mlx-community/whisper-large-v3` — slightly better quality, slower.
   - `mlx-community/whisper-tiny` — fast preview, lower accuracy.
3. **Transcribe** (`transcribe_audio`). Pass `language` if the user specified one ("subtitle in Turkish" → `language='tr'`).
4. **For translation**, transcribe with `language` of the source language, then have Claude translate the segments before writing SRT.
5. **Write SRT** (`export_srt_from_transcript`). Default path: alongside source with `.srt` suffix.
6. **Tell the user the import method**:
   - In Resolve: drag the `.srt` from Finder directly onto an empty subtitle track in the timeline, OR `File > Import > Subtitle`.
   - Style is applied in Inspector → Caption.

## Subtitle quality tips

- For social/short-form: max 2 lines, max 32 chars per line. Re-segment if Whisper's output exceeds.
- For accessibility: keep cues ≥ 1 s and ≤ 7 s; never split a single word across cues.
- Punctuate sentences. Whisper occasionally drops terminal punctuation.

## Example

User: *"Add Turkish subtitles to `~/Footage/talk.mp4`."*

You:
1. Probe → 1080p30, 22 min, has audio.
2. Transcribe with `language='tr'`, model `large-v3-turbo`.
3. Inspect segments — if any > 7 s, split them by sentence boundary in the text.
4. Write SRT to `~/Footage/talk.tr.srt`.
5. Reply: "Drag `talk.tr.srt` onto a subtitle track in Resolve. 47 cues, avg 3.1 s each."

## Burning in (versus soft subtitles)

If the user wants the subtitles *baked into* the video pixels:
1. Generate the SRT as above.
2. Tell them to add it to a subtitle track, style it (Inspector), then render the timeline as MP4. The subtitle layer renders as part of the picture.
3. Alternatively, use ffmpeg: `ffmpeg -i source.mp4 -vf subtitles=source.srt out.mp4` — works without Resolve.

## Failure modes

- If `probe_media` shows no audio stream, stop and tell the user.
- If duration > 1 hr, ask before running.
