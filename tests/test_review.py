"""The automatic look at a cut: every issue is on the timeline, with its clip."""
import json
import subprocess

from resolve_pilot.analyzers import media as media_probe
from resolve_pilot.analyzers.media import MediaInfo
from resolve_pilot.analyzers.quality import Span
from resolve_pilot.editorial import review
from resolve_pilot.editorial.roughcut import RoughCutClip, RoughCutPlan
from resolve_pilot.exporters.srt import SrtCue

A, B = "/media/a.mov", "/media/b.mov"


def _plan(*ranges):
    return RoughCutPlan(title="Cut", fps=25.0, clips=[
        RoughCutClip(source_path=r[2] if len(r) > 2 else A, in_seconds=r[0], duration_seconds=r[1]) for r in ranges])


def _starts(plan, lead=0.0):
    out = [lead]
    for c in plan.clips:
        out.append(out[-1] + c.duration_seconds)
    return out


def _levels(seconds, *stretches, default=-20.0):
    """A level curve: `default` dB everywhere but in the (start, end, dB) stretches."""
    out = [default] * round(seconds * 100)
    for a, b, db in stretches:
        out[round(a * 100):round(b * 100)] = [db] * (round(b * 100) - round(a * 100))
    return out


def _kinds(issues):
    return [(i.kind, i.clip, round(i.at_seconds, 2)) for i in issues]


def test_clips_are_found_by_timeline_time_and_gaps_belong_to_none():
    starts = [1.0, 3.0, 3.2, 6.0]            # a 1 s fade-in gap, then three clips
    assert [review.clip_at(starts, t) for t in (0.5, 1.0, 2.99, 3.0, 3.1, 5.99, 6.0)] == [None, 0, 0, 1, 1, 2, None]


def test_a_cut_is_where_the_footage_jumps_not_where_it_runs_on():
    plan = _plan((0.0, 2.0), (2.0, 1.0), (5.0, 1.0), (6.0, 1.0, B), (7.0, 1.0, B))
    assert review.real_cuts(plan) == [2, 3]   # 0->1 and 3->4 play on unbroken


def test_flash_clips_and_rotated_sources_come_from_the_plan():
    plan = _plan((0.0, 2.0), (5.0, 0.24), (9.0, 0.5), (0.0, 1.0, B), (3.0, 1.0, B))
    info = MediaInfo(path=B, duration_seconds=60, width=3840, height=2160, fps=25.0, video_codec="hevc",
                     audio_codec="aac", sample_rate=48000, channels=2, bit_rate=None, container="mov", rotation=90)
    issues = review.plan_issues(plan, _starts(plan), {A: None, B: info})
    assert _kinds(issues) == [("flash_clip", 1, 2.0), ("rotated_source", 3, 2.74)]   # 0.5 s is not a flash
    assert "0.24 s (6 frames)" in issues[0].detail
    assert "b.mov is tagged as turned 90 degrees" in issues[1].detail and "2160x3840" in issues[1].detail
    upside_down = MediaInfo(**{**info.to_dict(), "rotation": 180})
    assert "3840x2160 inside" in review.plan_issues(plan, _starts(plan), {B: upside_down})[1].detail


def test_silence_black_and_frozen_stretches_are_mapped_to_their_clips():
    plan = _plan((0.0, 4.0), (10.0, 4.0), (20.0, 4.0))
    starts = _starts(plan, lead=1.0)          # 1 s fade-in gap; clips at 1-5, 5-9, 9-13; fade-out to 14
    spans = [Span(0.0, 1.0, "black", 1.0),    # the fade-in gap is meant to be black
             Span(0.0, 1.3, "silence", 1.0),  # short
             Span(5.5, 7.5, "silence", 1.0),
             Span(4.9, 6.2, "freeze", 1.0),   # starts in clip 0, most of it in clip 1
             Span(9.4, 9.8, "black", 1.0),
             Span(12.6, 14.0, "black", 1.0),  # runs into the fade-out gap: only 0.4 s is the clip's
             Span(2.0, 3.0, "loud", 1.0)]
    issues = review.quality_issues(spans, starts, 14.0)
    assert _kinds(issues) == [("silence", 1, 5.5), ("frozen_picture", 0, 4.9), ("black_picture", 2, 9.4),
                              ("black_picture", 2, 12.6)]
    assert "2.0 s" in issues[0].detail and "0.4 s" in issues[3].detail


def test_cuts_are_judged_by_the_sound_around_them():
    plan = _plan((0.0, 2.0), (5.0, 2.0), (9.0, 2.0), (11.0, 2.0), (20.0, 2.0), (30.0, 2.0))
    starts = _starts(plan)                    # cuts at 2, 4, (6 runs on), 8, 10
    levels = _levels(12.0,
                     (1.9, 2.1, -60.0),       # cut 1 sits in a pause
                     (4.0, 6.0, -31.0),       # clip 2 is 11 dB quieter; no dip at 4.0
                     (6.0, 8.0, -20.0),
                     (7.9, 8.1, -55.0),       # cut 4 in a pause
                     (8.1, 10.0, -70.0))      # clip 4 is silent: nothing to compare at 8 or 10
    issues = review.cut_issues(plan, starts, levels)
    assert _kinds(issues) == [("level_jump", 2, 4.0), ("cut_in_sound", 2, 4.0)]
    assert "11 dB quieter" in issues[0].detail
    # The same cut with the voice dipping at it is only a level jump.
    dipped = levels[:395] + [-60.0] * 10 + levels[405:]
    assert _kinds(review.cut_issues(plan, starts, dipped)) == [("level_jump", 2, 4.0)]
    # Continuous sound and no jump: only "no dip".
    assert _kinds(review.cut_issues(_plan((0.0, 2.0), (5.0, 2.0)), [0.0, 2.0, 4.0], _levels(4.0))) == [
        ("cut_in_sound", 1, 2.0)]
    assert review.cut_issues(plan, starts, []) == []


def test_captions_too_fast_to_read_are_listed():
    cues = [SrtCue(1.0, 2.0, "Kısa."), SrtCue(2.0, 3.0, "Bu satır bir saniyede okunamayacak\nkadar uzun.")]
    (issue,) = review.caption_issues(cues, [0.0, 2.5, 5.0])
    assert (issue.kind, issue.clip, issue.at_seconds) == ("fast_caption", 0, 2.0) and "46 characters in 1.0 s" in issue.detail


def test_many_issues_of_a_kind_are_counted_not_all_listed():
    issues = [review.Issue("cut_in_sound", float(t), "x", t) for t in range(30)] + [review.Issue("silence", 3.5, "y", 3)]
    out = review.summarize(issues)
    assert out["issue_counts"] == {"cut_in_sound": 30, "silence": 1}
    assert len(out["issues"]) == 13 and [i["at_seconds"] for i in out["issues"][:5]] == [0.0, 1.0, 2.0, 3.0, 3.5]
    assert out["issues"][4] == {"kind": "silence", "at_seconds": 3.5, "detail": "y", "clip": 3}


def test_probe_reads_the_rotation_a_phone_tags(tmp_path, monkeypatch):
    clip = tmp_path / "phone.mov"
    clip.write_bytes(b"x")

    def probe(stream_extra):
        stream = {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
                  "avg_frame_rate": "30/1", **stream_extra}
        reply = json.dumps({"format": {"duration": "5.0", "format_name": "mov"}, "streams": [stream]})
        monkeypatch.setattr(media_probe, "_ffprobe_bin", lambda: "ffprobe")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, reply, ""))
        return media_probe.probe_media(clip)

    assert probe({}).rotation == 0
    assert probe({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}).rotation == -90
    assert probe({"tags": {"rotate": "90"}}).rotation == 90          # how older ffprobe reports it
    info = probe({"side_data_list": [{"side_data_type": "Other"}], "tags": {"rotate": "180"}})
    assert (info.rotation, info.width, info.height) == (180, 3840, 2160)   # stored size is kept
