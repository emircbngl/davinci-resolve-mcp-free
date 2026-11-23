---
name: resolve-phone-hdr
description: Make phone HDR footage (iPhone HLG clips) look right in Resolve and in the preview — recognise it, convert it with the built-in hlg-to-rec709 LUT on exactly the HDR clips, and check the picture. Use for "the colours look pale / washed out / grey", "my iPhone clips look flat in Resolve", "fix the HDR footage", or whenever `scan_folder`, `probe_media` or `export_fcpxml` report `hdr`.
---

# resolve-phone-hdr

An iPhone records HDR by default: HLG with BT.2020 colour. A Resolve project without colour management shows those numbers as if they were Rec.709, so the picture is bright, flat and grey (a blue sky turns pale grey-blue). The clips are fine; they are being read wrongly.

## When to use

- "My clips look pale / washed out in Resolve"
- `scan_folder` or `probe_media` shows `hdr: "hlg"` on a file
- `export_fcpxml` returns `hdr_sources` and names `apply_lut` in `next_step`

## Steps

1. **Know what you have**: `scan_folder(folder)` or `probe_media(path)`. `hdr: "hlg"` is phone HDR (this guide). `hdr: "pq"` is HDR10 (some Android phones, cameras): there is no built-in conversion for it, say so and stop here for those files. No `hdr`: ordinary footage, leave it alone.
2. **Cut as usual.** `render_preview` already shows HLG sources converted (`hlg_sources_converted` in its result), so what the user watches is what they will get. If `not_shown` says HLG sources are not converted, this machine's ffmpeg has no `lut3d` filter: tell the user the preview is paler than the result will be.
3. **After `import_timeline`**: `apply_lut(lut_path="hlg-to-rec709")`.
   - It puts the built-in LUT on node 1 of the clips Resolve itself tags `Rec.2100 HLG`, and nothing else: `applied` is how many got it, `skipped` how many are not HLG. Mixed timelines (phone + camera) are safe.
   - `applied: 0` with a `note`: no clip on that track is HLG; nothing changed.
   - More video tracks (B-roll on V2): call it again with `track=2`.
4. **Look**: `timeline_frames` at a few moments, one per source. Skin should look like skin and a sky blue. Before / after on the same frame is the honest check.
5. **Then grade, if asked** (`grade_clips`, a look LUT on node 2). The conversion is the starting point, not the look. A LUT on node 1 replaces the one already there, so put a creative LUT on another node.

## What the LUT does, and how it was measured

It undoes the HLG curve, converts BT.2020 colour to BT.709, encodes with a power of 0.40 and adds 14% saturation. The two numbers were fitted to Apple's own SDR conversion of the same files, on five frames of three clips, and checked on three other frames. Against Apple's frames (PSNR, higher is closer):

| | dB |
|---|---|
| as an unmanaged project shows the clips | 17-20 |
| Resolve colour management (HLG in, Rec.709 out, DaVinci tone mapping) | 13-14 |
| one CDL (slope 0.90, power 1.22, saturation 1.62) | 29-30 |
| the built-in LUT, applied in Resolve 20.3 | 37-41 |

It was measured on daylight footage from one iPhone (14 Pro). Not tried: night footage, other phones, Dolby Vision trims beyond what the HLG base layer holds.

## Do not

- Do not turn on Resolve's colour management for this without asking: it changes every timeline in the project, and with tone mapping it came out brighter and flatter than the phone shows the clips.
- Do not apply `hlg-to-rec709` in a project that *is* colour managed (Project Settings › Color Management › DaVinci YRGB Color Managed): Resolve already converts there, and the LUT would convert a second time. Ask the user which they want.
- Do not use it on `pq` footage or on ordinary clips by passing a copy of the file as `lut_path`: the filter that keeps it off other clips only applies to the built-in name.

## Example

User: *"The vlog looks washed out."*

1. `scan_folder("~/Trip/day2")` → 10 files, all `hdr: "hlg"`.
2. `apply_lut(lut_path="hlg-to-rec709")` → `applied: 198, skipped: 0`.
3. `timeline_frames` at 10 s, 112 s, 300 s → skin and sky look natural.
4. Reply: the clips are iPhone HDR and were shown without conversion; converted on all 198 clips, nothing else touched; say what you looked at.

## Failure modes

- `ok: false`, "Resolve rejected the LUT for every clip": the node index does not exist on the clips, or Resolve's LUT folder could not be written (the error says how to copy the file by hand).
- The picture did not change: the clips are not tagged HLG in Resolve (`skipped` equals `total`). Check `probe_media`; a transcoded copy may have lost its tags.
