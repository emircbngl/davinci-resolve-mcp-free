"""The Resolve-less preview: frame N of the preview is frame N of the timeline."""
import shutil
import subprocess

import pytest

from resolve_pilot.editorial.roughcut import RoughCutAudioBed, RoughCutClip, RoughCutPlan
from resolve_pilot.exporters import preview
from resolve_pilot.exporters.srt import SrtCue
from tests.conftest import FFMPEG

pytestmark = pytest.mark.skipif(not (FFMPEG and shutil.which("ffprobe")), reason="ffmpeg not installed")


@pytest.fixture(scope="session")
def counter(tmp_path_factory):
    """Footage that says where it is: frame N of the 25 fps picture is flat grey of
    brightness 20 + 4N (56 frames), and the sound is silent but for a click at 1.0 s.
    Plus the same picture without any audio, and a 4 s tone to lay under a cut."""
    d = tmp_path_factory.mktemp("counter")
    run = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    picture = ["-f", "lavfi", "-i", "color=c=black:s=160x90:r=25:d=2.24,format=gray,geq=lum='20+4*N',format=yuv420p"]
    clicks = ["-f", "lavfi", "-i", "aevalsrc='if(between(t,1,1.01),0.8*sin(2*PI*1000*t),0)':d=2.24:s=48000"]
    video = ["-c:v", "libx264", "-qp", "0", "-g", "12"]
    subprocess.run(run + picture + clicks + video + ["-c:a", "pcm_s16le", str(d / "counter.mov")], check=True)
    subprocess.run(run + picture + video + [str(d / "mute.mov")], check=True)
    subprocess.run(run + ["-f", "lavfi", "-i", "sine=frequency=300:duration=4", "-ar", "44100", str(d / "tone.wav")], check=True)
    return {"clip": str(d / "counter.mov"), "mute": str(d / "mute.mov"), "tone": str(d / "tone.wav")}


def _shown(path) -> list[int | None]:
    """Which source frame each preview frame shows (None for black)."""
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(path), "-vf", "scale=1:1", "-pix_fmt", "gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return [None if y < 12 else round((y - 20) / 4) for y in raw]


def _sound(path) -> list[float]:
    """Peak level of every 10 ms of the preview's audio (0-1)."""
    raw = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "48000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    values = memoryview(raw).cast("h")
    return [max(abs(v) for v in values[i:i + 480]) / 32768 for i in range(0, len(values) - 479, 480)]


def _plan(*ranges, source, fps=25.0, **kw):
    return RoughCutPlan(title="Cut", fps=fps, width=160, height=90, clips=[
        RoughCutClip(source_path=r[2] if len(r) > 2 else source, in_seconds=r[0], duration_seconds=r[1])
        for r in ranges], **kw)


def test_every_clip_gets_exactly_its_frames(counter, tmp_path):
    # Twenty clips of 1.4 frames each. On the plan's frame grid (boundaries rounded
    # cumulatively: 1.4 -> 1, 2.8 -> 3, 4.2 -> 4, 5.6 -> 6...) they get 1 or 2 frames, 28 in
    # all; rounding every clip on its own would give 20 and drift 0.3 s in one second.
    ranges = [(0.4 + 0.08 * i, 0.056) for i in range(20)]
    out = preview.render(_plan(*ranges, source=counter["clip"]), tmp_path / "p.mp4", height=90)
    assert (out["frames"], out["seconds"], out["width"], out["height"]) == (28, 1.12, 160, 90)
    shown = _shown(out["path"])
    starts = [10 + 2 * i for i in range(20)]                     # the source frame each clip begins on
    lengths = [1, 2, 1, 2, 1, 1, 2, 1, 2, 1, 1, 2, 1, 2, 1, 1, 2, 1, 2, 1]
    assert shown == [starts[i] + k for i in range(20) for k in range(lengths[i])]
    assert out["not_shown"] == [] and out["captions"] == 0


def test_sound_stays_with_the_picture_across_cuts_fades_and_a_bed(counter, tmp_path):
    # 1 s fade-in gap, then source 0.8-1.4 (the click is 0.2 s in), a silent clip, 0.5 s fade-out.
    plan = _plan((0.8, 0.6), (0.4, 0.4, counter["mute"]), source=counter["clip"],
                 fade_in_seconds=1.0, fade_out_seconds=0.5)
    out = preview.render(plan, tmp_path / "p.mp4", height=90)
    shown = _shown(out["path"])
    assert out["frames"] == len(shown) == 25 + 15 + 10 + 12       # the fade-out gap: 12.5 frames -> 12
    assert shown[:25] == [None] * 25 and shown[25:40] == list(range(20, 35))
    assert shown[40:50] == list(range(10, 20)) and shown[50:] == [None] * (len(shown) - 50)
    sound = _sound(out["path"])
    loud = [i for i, v in enumerate(sound) if v > 0.2]
    assert loud and abs(loud[0] * 0.01 - 1.2) <= 0.02              # the click: 1.0 s gap + 0.2 s
    assert max(sound[:110]) < 0.01 and max(sound[130:]) < 0.01     # nothing else, also from the mute clip
    assert out["not_shown"] == ["fades are a black gap and a cut"]

    plan.audio_bed = RoughCutAudioBed(source_path=counter["tone"], offset_seconds=1.0, duration_seconds=0.5)
    sound = _sound(preview.render(plan, tmp_path / "bed.mp4", height=90)["path"])
    tone = [i for i, v in enumerate(sound) if v > 0.05]
    assert abs(tone[0] * 0.01 - 1.0) <= 0.02 and abs(tone[-1] * 0.01 - 1.48) <= 0.02   # 12 frames of bed
    assert max(sound[118:122]) > 0.5                              # the click is still there, on top


def test_captions_ride_along_as_a_subtitle_track(counter, tmp_path):
    cues = [SrtCue(0.2, 0.9, "Merhaba"), SrtCue(1.0, 1.6, "ikinci satır")]
    out = preview.render(_plan((0.0, 2.0), source=counter["clip"]), tmp_path / "p.mp4", height=90, cues=cues)
    assert out["captions"] == 2
    srt = subprocess.run([FFMPEG, "-loglevel", "error", "-i", out["path"], "-map", "0:s:0", "-f", "srt", "-"],
                         capture_output=True, text=True, check=True).stdout
    assert "00:00:00,200 --> 00:00:00,900\nMerhaba" in srt and "ikinci satır" in srt


def test_a_clip_past_the_end_of_its_source_holds_the_last_frame(counter, tmp_path):
    # The source is 2.24 s (frames 0-55); the clip asks for 2.0-2.6. validate_plan warns about
    # it; the preview still has every frame of the plan, as the timeline will.
    out = preview.render(_plan((2.0, 0.6), source=counter["clip"]), tmp_path / "p.mp4", height=90)
    assert _shown(out["path"]) == list(range(50, 56)) + [55] * 9


def test_a_preview_with_the_wrong_number_of_frames_is_not_written(counter, tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "_count_frames", lambda path: 49)
    with pytest.raises(RuntimeError, match="the preview has 49 frames, the plan 50"):
        preview.render(_plan((0.0, 2.0), source=counter["clip"]), tmp_path / "p.mp4", height=90)
    assert not (tmp_path / "p.mp4").exists()


def test_a_plan_that_cannot_be_rendered_says_which_clip(counter, tmp_path):
    plan = _plan((0.0, 1.0), (0.0, 1.0, str(tmp_path / "gone.mov")), source=counter["clip"])
    with pytest.raises(RuntimeError, match=r"clip 1 \(gone.mov\)"):
        preview.render(plan, tmp_path / "p.mp4", height=90)
    assert not (tmp_path / "p.mp4").exists() and list(tmp_path.glob("preview-*")) == []
    assert preview.preview_size(RoughCutPlan(title="x", width=3840, height=2160), 540) == (960, 540)
    assert preview.preview_size(RoughCutPlan(title="x", width=1080, height=1920), 540) == (304, 540)
    assert preview.preview_size(RoughCutPlan(title="x", width=320, height=240), 540) == (320, 240)   # never up


# ── through the tool ────────────────────────────────────────────────────────

def _demo_plan(media):
    from resolve_pilot import mcp_server
    from resolve_pilot.editorial import ops
    clip = str(media["clip"])     # 6 s at 24 fps, 320x240, a steady 440 Hz tone, the picture cuts at 3 s
    made = mcp_server.create_plan("Demo", [
        ops.ClipSpec(source_path=clip, in_seconds=0.0, duration_seconds=1.0),
        ops.ClipSpec(source_path=clip, in_seconds=4.0, duration_seconds=0.25),
        ops.ClipSpec(source_path=clip, in_seconds=2.0, duration_seconds=1.5)])
    return made["plan_id"], clip


def test_render_preview_renders_checks_and_remembers(media, isolated_home):
    from resolve_pilot import mcp_server
    from resolve_pilot.editorial import ops
    pid, _ = _demo_plan(media)
    first = mcp_server.render_preview(pid, wait_seconds=0)
    assert first["status"] == "running" and first["pieces"] == 3 and "again" in first["next_step"]
    out = mcp_server.render_preview(pid, wait_seconds=120)
    assert out["status"] == "done" and out["cached"] is False
    assert out["path"] == str(isolated_home / "previews" / f"{pid}.mp4")
    assert (out["frames"], out["seconds"], out["width"], out["height"]) == (66, 2.75, 320, 240)
    assert len(_shown(out["path"])) == 66 and out["captions"] == 0 and out["not_checked"] == []
    # A six-frame clip, and two cuts through a tone that never dips.
    assert out["issue_counts"] == {"flash_clip": 1, "cut_in_sound": 2} and out["cuts"] == 2
    assert [(i["kind"], i["clip"], i["at_seconds"]) for i in out["issues"]] == [
        ("cut_in_sound", 1, 1.0), ("flash_clip", 1, 1.0), ("cut_in_sound", 2, 1.25)]

    assert mcp_server.render_preview(pid)["cached"] is True
    mcp_server.edit_plan(pid, [ops.RemoveOp(op="remove", indices=[1])])
    edited = mcp_server.render_preview(pid, wait_seconds=120)       # same file, new content
    assert edited["cached"] is False and edited["path"] == out["path"] and edited["frames"] == 60
    assert edited["issue_counts"] == {"cut_in_sound": 1}
    assert mcp_server.render_preview(pid, wait_seconds=120, refresh=True)["cached"] is False
    assert sorted(p.name for p in (isolated_home / "previews").iterdir()) == [f"{pid}.json", f"{pid}.mp4"]


def test_render_preview_carries_the_captions_of_the_cut(media, isolated_home):
    import json

    from resolve_pilot import mcp_server
    from resolve_pilot.transcribe import store
    pid, clip = _demo_plan(media)
    tid = "t-0123456789ab"
    words = [{"start": 2.2, "end": 2.6, "text": "Hello", "p": 1.0}, {"start": 2.6, "end": 3.1, "text": "there.", "p": 1.0}]
    (store.transcripts_dir() / f"{tid}.json").write_text(json.dumps({
        "transcript_id": tid, "source_path": clip, "language": "en",
        "segments": [{"start": 2.2, "end": 3.1, "text": "Hello there.", "words": words}]}), encoding="utf-8")
    assert mcp_server.render_preview(pid, wait_seconds=120)["captions"] == 0
    out = mcp_server.render_preview(pid, transcript_ids=[tid], wait_seconds=120)
    assert out["cached"] is False and out["captions"] == 1 and out["caption_warnings"] == []
    srt = subprocess.run([FFMPEG, "-loglevel", "error", "-i", out["path"], "-map", "0:s:0", "-f", "srt", "-"],
                         capture_output=True, text=True, check=True).stdout
    # Said 0.2 s into the third clip, which starts at 1.25 s on the timeline.
    assert "00:00:01,450 -->" in srt and "Hello there." in srt


def test_render_preview_refuses_a_plan_with_errors(media):
    from resolve_pilot import mcp_server
    from resolve_pilot.editorial import ops
    pid, clip = _demo_plan(media)
    mcp_server.edit_plan(pid, [ops.UpdateOp(op="update", index=1, duration_seconds=0.01)])
    with pytest.raises(ValueError, match="Fix these plan errors first"):
        mcp_server.render_preview(pid)
