---
name: resolve-color-grade
description: Apply a consistent color grade across a Resolve timeline — LUT-based, saved-grade (.drx) based, or matched to a reference image. Uses apply_lut (live or paste-able Lua) and guides the user through what scripting cannot reach. Use for "apply this LUT to all clips", "match the look of X", "grade this in the style of Y".
---

# resolve-color-grade

Applies color treatments across a timeline: LUT files, saved grades (`.drx`), and reference-driven style matching.

## When to use

- "Apply `Kodak2383.cube` to every clip on V1"
- "Match the color of `reference.jpg` across this timeline"
- "Grade this footage like a moody noir film"
- "Copy the grade from this clip to the rest"

## Three modes

### 1. LUT (deterministic, fastest)

```
apply_lut(lut_path="/abs/path/Kodak2383.cube", track=1, node_index=1)
```
- Live (studio / bridge): applied immediately; the result has `applied` / `total`.
- Manual: hand the user `lua_code` + `instructions` (Workspace › Console › Lua tab).
- `node_index` is the color node that receives the LUT; it replaces any LUT already on that node, so confirm with the user if the clips are graded.

### 2. Saved grade (.drx)

Live: `run_lua(snippet="grade_from_drx", args={"drx_path": "/abs/grade.drx", "track": 1, "mode": 0})`.
Manual: `render_lua_snippet("grade_from_drx", {...})` and relay the code.
`mode`: 0 no keyframes, 1 source timecode aligned, 2 start frames aligned.

### 3. Style match from a reference image

1. Get the reference image (path or attachment) and look at it.
2. Describe it: color cast (warm / cool / teal-orange), contrast (lifted or crushed blacks), saturation, skin tones, grain / halation.
3. Translate into concrete Resolve adjustments, because the scripting API does not expose the primary wheels:
   ```
   Color page → V1 clip 1 → Node 01:
   - Lift:  B +0.05 (cool shadows)
   - Gamma: R −0.03, B +0.02
   - Gain:  R +0.08, G +0.04 (warm highlights)
   - Saturation 75 · Contrast 1.15 · Pivot 0.42
   Then: right-click the clip → Apply Grade → All Clips In Same Track (or save a still and apply it)
   ```
4. `open_page(page="color")` brings the user to the Color page.
5. If the look should be reusable, have the user export a LUT (right-click the clip → Generate LUT) and apply it with mode 1.

## Quality conventions

- Node order: input transform → balance → creative look → output transform.
- Skin check: after grading, ask the user to look at a face shot; skin must not go magenta or green.
- Match with scopes: ask for Waveform + Vectorscope and say what to look for.

## Failure modes

- `applied: 0` → the track is empty or the number is wrong; check `get_resolve_state(include_clips=true, track_index=N)`.
- LUT paths must be absolute and readable by Resolve on the same machine.
- `.drx` import works in Free; `.drx` export may be restricted — test before promising.
