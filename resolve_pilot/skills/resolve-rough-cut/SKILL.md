---
name: resolve-rough-cut
description: Build a rough cut from raw footage. Transcribes audio, detects scenes, drafts a timeline, and exports an FCPXML that DaVinci Resolve (Free & Studio) imports cleanly. Use when the user has source video and wants Claude to make editorial decisions before opening Resolve.
---

# resolve-rough-cut

Builds a first-pass timeline from raw footage, deciding what to keep based on transcript content and scene structure, then writes FCPXML for `File > Import > Timeline` in Resolve.

## When to use

- User has raw footage and asks for a "first cut" / "rough cut" / "draft edit"
- User wants Claude to make editorial decisions (not just process clips)
- User wants to save manual scrubbing time

## Prerequisites

1. `resolve-pilot` MCP server connected to Claude (see project README).
2. `mlx-whisper` installed if transcription is needed: `pip install 'resolve-pilot[transcribe]'`.
3. Source media path is accessible from the MCP server host.

## Workflow

1. **Call `probe_media_tool(path)`** — learn fps, resolution, duration. Use this to set the timeline format.
2. **Call `detect_scenes_tool(path, threshold=10.0)`** — get scene boundaries. Lower threshold → more cuts.
3. **If the footage has dialogue, call `transcribe_audio(path)`** — get spoken segments.
4. **Decide editorially.** Look at transcript + scenes together:
   - Drop dead air, redundant lines, weak takes.
   - Tighten by trimming 0.1–0.3 s off each clip head/tail.
   - For interview/talking-head: prefer transcript-segment cuts.
   - For B-roll/montage: prefer scene cuts.
   - Pacing rule of thumb: aim for ~3–5 s clips for energetic edits, 8–15 s for documentary.
5. **Build a `RoughCutPlan` JSON** with `clips: [{source_path, in_seconds, duration_seconds, rationale, name}]`. The `rationale` field is for the user to review what you chose.
6. **Call `export_fcpxml(plan_json, out_path)`** — writes FCPXML to disk.
7. **Tell the user:** "Open Resolve → File → Import → Timeline → pick `{out_path}`."

## Important conventions

- Always include `rationale` on every clip — this is how the user audits your choices.
- Use `pad_seconds=0.2` on transcript cuts to keep natural breath/sync.
- If duration > 30 min, ask the user before transcribing (large model takes time).
- Match the timeline `fps` to source fps unless the user requested otherwise.
- For vertical/social formats, set `width=1080, height=1920` and warn the user that reframing happens in Resolve (Smart Reframe or manual).

## Example

User: *"I have a 25-min interview at `~/Footage/jane.mp4`. Give me a 4-minute highlight cut focused on her best lines."*

You:
1. Probe → 1080p30 H.264, 25:13 duration.
2. Transcribe (large-v3-turbo, ~1 min on M-series).
3. Read the 200+ segments. Pick the 15 strongest (most quotable, complete thoughts, no false starts).
4. Total target: 240 s. Adjust by trimming pad if over budget.
5. Build plan with rationale per clip ("ans to 'what surprised you' — strong delivery").
6. Export to `~/Footage/jane_4min_roughcut.fcpxml`.
7. Reply with: 15-clip summary table + import instruction.

## Failure modes to avoid

- Don't call `transcribe_audio` on files > 1 hr without warning — quote the user the expected time.
- Don't pick clips that overlap (sort by `in_seconds` and check).
- Don't set durations that exceed the source file's length.
- If `probe_media` returns `fps=None`, default to 24 and tell the user.
