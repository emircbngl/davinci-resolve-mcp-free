---
name: resolve-color-grade
description: Apply a consistent color grade across a Resolve timeline — LUT-based, DRX-based, or reference-image-based. Generates Lua snippets for batch application and UI automation steps for the parts the API can't reach. Use for "apply this LUT to all clips", "match the look of X", "grade this in the style of Y".
---

# resolve-color-grade

Applies color treatments across a timeline. Handles three approaches: LUT files, saved grades (`.drx`), and Claude-vision-driven style matching.

## When to use

- "Apply `Kodak2383.cube` to every clip on V1"
- "Match the color of `reference.jpg` across this timeline"
- "Grade this footage like a moody noir film"
- "Copy the grade from this clip to the rest"

## Three modes

### 1. LUT application (deterministic, fastest)

Use when the user has a `.cube` or `.dat` LUT.

```python
render_lua_snippet(
    snippet="apply_lut_to_clips",
    args_json=json.dumps({
        "lut_path": "/path/to/lut.cube",
        "track": 1,
        "node_index": 1,  # which color node receives the LUT
    })
)
```

Tell the user: paste into Console (Workspace → Console) with the timeline active.

### 2. Saved grade (.drx) application

Use when the user has a saved grade file from a previous project.

```python
render_lua_snippet(
    snippet="grade_from_drx",
    args_json=json.dumps({
        "drx_path": "/path/to/grade.drx",
        "track": 1,
        "mode": 0,  # NoKeyframes
    })
)
```

### 3. Style-match from reference image (Claude vision)

When the user has a reference frame (movie still, photo) and wants the timeline graded to match:

1. **Ask the user to give you the reference image** (path or attached file).
2. **Look at it.** Read the image with multimodal vision. Describe its color characteristics:
   - Dominant color cast (warm/cool/teal-orange)
   - Contrast curve (lifted blacks? crushed?)
   - Saturation (muted? punchy?)
   - Skin tone treatment
   - Grain/halation
3. **Translate to grade values.** Suggest concrete Resolve adjustments:
   - Lift / Gamma / Gain wheels (R, G, B offsets)
   - Saturation, contrast, pivot
   - Color Warper or Color Space Transform nodes if appropriate
4. **Hand the user a step-by-step**, since the scripting API doesn't expose Primary wheel values cleanly:
   ```
   Open Color page → V1 clip 1 → Node 01:
   - Lift: B +0.05 (cool shadows)
   - Gamma: R -0.03, B +0.02
   - Gain: R +0.08, G +0.04 (warm highlights)
   - Saturation: 75
   - Contrast: 1.15, Pivot: 0.42
   Right-click clip → Apply Grade → All Clips In Same Track
   ```
5. **Optionally** export a starter LUT with these values via DaVinci Resolve (LUT generator) and apply via mode 1.

## Quality conventions

- **Always preview node order**: input transform → primary balance → creative grade → output transform.
- **Skin tone check**: after grading, ask user to scrub to a face shot and confirm skin doesn't go magenta/green.
- **Match scopes, not eyes**: ask the user to enable Waveform + Vectorscope; tell them what to look for.

## Failure modes

- Resolve Free does not allow `.drx` export but accepts `.drx` import (mostly). Test on free copies before promising.
- LUT files must be reachable from the Resolve machine — relative paths in the snippet won't work.
- If `apply_lut_to_clips` returns 0 clips touched, the track may be empty or wrong number.
