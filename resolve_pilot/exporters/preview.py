"""Render a rough cut to a small MP4 with ffmpeg alone, so a plan can be watched (and
checked) without Resolve.

Times in the preview are timeline times: every clip gets exactly the frames
`fcpxml.layout` gives it, and audio is cut on the matching sample, so frame N of the
preview is frame N of the timeline Resolve imports and captions made for the plan fit.
The pieces are encoded with PCM audio first because AAC pads every file it writes: 45
clips joined as AAC came out 1.6 s longer than their plan.

Not shown: cross dissolves (played as cuts) and the fades themselves (their black gap
is there, so the length is right). A source is shown as players show it (its rotation
tag applied), then turned by the plan's `source_turns`.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from resolve_pilot.analyzers.media import probe_media_cached
from resolve_pilot.editorial.roughcut import RoughCutClip, RoughCutPlan, plan_to_timeline_spec
from resolve_pilot.exporters.fcpxml import _fps_rational, layout
from resolve_pilot.exporters.srt import SrtCue, write_srt

SAMPLE_RATE = 48000
_AUDIO = f"aresample={SAMPLE_RATE},aformat=sample_fmts=s16:channel_layouts=stereo"
_WORKERS = 3  # pieces encoded at once; each ffmpeg is itself multi-threaded


def preview_size(plan: RoughCutPlan, height: int) -> tuple[int, int]:
    """The plan's frame scaled to `height` (never up), both sides even for H.264."""
    h = max(2, min(height, plan.height) // 2 * 2)
    return max(2, round(plan.width * h / plan.height / 2) * 2), h


def _run(args: list[str], what: str, timeout: float = 1800.0) -> None:
    done = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
    if done.returncode != 0:
        tail = " | ".join((done.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"ffmpeg could not render {what}: {tail or 'no output'}")


_TURN = {90: "transpose=1,", 180: "hflip,vflip,", 270: "transpose=2,"}


def _piece(ffmpeg: str, clip: RoughCutClip | None, frames: int, samples: int,
           size: tuple[int, int], rate: str, out: Path, turn: int = 0) -> list[str]:
    """Command for one clip (or, with clip=None, a black and silent gap) of exactly
    `frames` frames and `samples` audio samples."""
    w, h = size
    info = probe_media_cached(clip.source_path) if clip else None
    has_video = clip is not None and (info is None or info.video_codec is not None)
    has_audio = clip is not None and (info is None or info.audio_codec is not None)
    args = [ffmpeg, "-nostdin", "-loglevel", "error", "-y"]
    if has_video or has_audio:
        num, den = (int(x) for x in rate.split("/"))
        args += ["-ss", f"{clip.in_seconds:.6f}", "-t", f"{frames * den / num + 1.0:.6f}",
                 "-i", str(Path(clip.source_path).expanduser())]
    if has_video:  # held on its last frame if the source ends early
        video = (f"[0:v:0]fps={rate},{_TURN.get(turn % 360, '')}scale={w}:{h}:force_original_aspect_ratio=decrease,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,tpad=stop_mode=clone:stop=-1")
    else:
        video = f"color=c=black:s={w}x{h}:r={rate},format=yuv420p"
    audio = f"[0:a:0]{_AUDIO},apad" if has_audio else f"anullsrc=r={SAMPLE_RATE}:cl=stereo,{_AUDIO}"
    graph = (f"{video},trim=end_frame={frames},setpts=PTS-STARTPTS[v];"
             f"{audio},atrim=end_sample={samples},asetpts=PTS-STARTPTS[a]")
    return args + ["-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-bf", "0",
                   "-c:a", "pcm_s16le", str(out)]


def _count_frames(path: Path) -> int:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH. Install with: brew install ffmpeg")
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=600.0, check=True).stdout
    return int(out.strip().split(",")[0])


def render(plan: RoughCutPlan, out_path: str | Path, height: int = 540,
           cues: list[SrtCue] | None = None,
           on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Write the plan's preview to `out_path` (.mp4) and return what was written.

    `cues` (timeline times, e.g. from captions.plan_captions) become a subtitle track.
    Raises when the result does not have exactly the plan's frames.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH. Install with: brew install ffmpeg")
    if not plan.clips:
        raise ValueError("the plan has no clips")
    lay = layout(plan_to_timeline_spec(plan))
    num, den = _fps_rational(plan.fps)
    rate, size = f"{num}/{den}", preview_size(plan, height)

    def sample(frame: int) -> int:  # cumulative, so audio never drifts from the picture
        return round(frame * den * SAMPLE_RATE / num)

    # (clip or None for a gap, first frame, end frame)
    parts: list[tuple[RoughCutClip | None, int, int]] = []
    if lay.bounds[0] > 0:
        parts.append((None, 0, lay.bounds[0]))
    parts += [(c, lay.bounds[i], lay.bounds[i + 1]) for i, c in enumerate(plan.clips)]
    if lay.total_frames > lay.bounds[-1]:
        parts.append((None, lay.bounds[-1], lay.total_frames))
    parts = [p for p in parts if p[2] > p[1]]

    target = Path(out_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="preview-", dir=target.parent) as tmp:
        work = Path(tmp)
        done = 0

        def encode(n: int) -> None:
            nonlocal done
            clip, a, b = parts[n]
            what = "a gap" if clip is None else f"clip {plan.clips.index(clip)} ({Path(clip.source_path).name})"
            turn = plan.source_turns.get(clip.source_path, 0) if clip else 0
            _run(_piece(ffmpeg, clip, b - a, sample(b) - sample(a), size, rate, work / f"{n:05d}.mkv", turn), what)
            done += 1
            if on_progress:
                on_progress(done, len(parts))

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            list(pool.map(encode, range(len(parts))))

        listing = work / "pieces.txt"
        listing.write_text("".join(f"file '{n:05d}.mkv'\n" for n in range(len(parts))), encoding="utf-8")
        args = [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
        audio_map, graph, inputs = "0:a:0", None, 1
        bed = plan.audio_bed
        if bed and lay.bed_fits:
            length = min(lay.bed_frames, lay.total_frames - lay.bed_offset)
            args += ["-ss", f"{bed.in_seconds:.6f}", "-t", f"{length * den / num:.6f}",
                     "-i", str(Path(bed.source_path).expanduser())]
            graph = (f"[{inputs}:a:0]{_AUDIO},adelay={sample(lay.bed_offset)}S:all=1[bed];"
                     f"[0:a:0][bed]amix=inputs=2:duration=first:normalize=0[a]")
            audio_map, inputs = "[a]", inputs + 1
        if cues:
            subs = write_srt(cues, work / "captions.srt")
            args += ["-i", str(subs)]
        if graph:
            args += ["-filter_complex", graph]
        args += ["-map", "0:v:0", "-map", audio_map]
        if cues:
            args += ["-map", f"{inputs}:s:0", "-c:s", "mov_text"]
        partial = work / "preview.mp4"
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(partial)]
        _run(args, "the joined preview")
        frames = _count_frames(partial)
        if frames != lay.total_frames:
            raise RuntimeError(f"the preview has {frames} frames, the plan {lay.total_frames}; "
                               "it was not written")
        partial.replace(target)

    not_shown = []
    if any(lay.edges[1:-1]):
        not_shown.append("cross dissolves are played as cuts")
    if lay.edges[0] or lay.edges[-1]:
        not_shown.append("fades are a black gap and a cut")
    return {"path": str(target), "frames": lay.total_frames, "fps": plan.fps,
            "seconds": lay.total_frames * den / num, "width": size[0], "height": size[1],
            "captions": len(cues) if cues else 0, "not_shown": not_shown}
