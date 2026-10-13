"""Plans: edit operations, validation, the store, and transcript-based drafting."""
from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from resolve_pilot.analyzers.media import MediaInfo
from resolve_pilot.editorial import ops, roughcut, store
from resolve_pilot.editorial.roughcut import RoughCutAudioBed, RoughCutClip, RoughCutPlan
from resolve_pilot.transcribe import Segment

OPS = TypeAdapter(list[ops.PlanOp])


def _plan(n: int = 4, **kw) -> RoughCutPlan:
    clips = [RoughCutClip(f"/src/{i}.mov", in_seconds=10.0 * i, duration_seconds=2.0, name=f"c{i}")
             for i in range(n)]
    return RoughCutPlan(title="T", fps=24.0, clips=clips, **kw)


def _apply(plan, raw):
    return ops.apply_ops(plan, OPS.validate_python(raw))


def _media(duration=60.0, fps=24.0) -> MediaInfo:
    return MediaInfo(path="/x", duration_seconds=duration, width=1920, height=1080, fps=fps,
                     video_codec="h264", audio_codec="aac", sample_rate=48000, channels=2,
                     bit_rate=None, container="mov")


# ── operations ───────────────────────────────────────────────────────────────

def test_keep_selects_and_reorders():
    new, log = _apply(_plan(), [{"op": "keep", "indices": [3, 0]}])
    assert [c.name for c in new.clips] == ["c3", "c0"]
    assert log == ["kept 2 clips"]


def test_ops_apply_in_sequence_against_the_current_state():
    new, _ = _apply(_plan(), [
        {"op": "remove", "indices": [0]},          # c1 c2 c3
        {"op": "move", "index": 2, "to": 0},       # c3 c1 c2
        {"op": "trim", "index": 0, "head_seconds": 0.5, "tail_seconds": 0.25},
        {"op": "insert", "clip": {"source_path": "/b.mov", "in_seconds": 1, "out_seconds": 3}},
    ])
    assert [c.name for c in new.clips] == ["c3", "c1", "c2", None]
    assert new.clips[0].in_seconds == 30.5 and new.clips[0].duration_seconds == 1.25
    assert new.clips[-1].duration_seconds == 2.0


def test_batches_are_all_or_nothing():
    plan = _plan()
    with pytest.raises(ValueError, match=r"operation #1 \(remove\).*out of range.*No changes"):
        _apply(plan, [{"op": "remove", "indices": [0]}, {"op": "remove", "indices": [9]}])
    assert len(plan.clips) == 4


def test_update_in_point_keeps_out_point():
    new, _ = _apply(_plan(), [{"op": "update", "index": 1, "in_seconds": 10.5}])
    assert new.clips[1].in_seconds == 10.5 and new.clips[1].out_seconds == 12.0


def test_trim_cannot_remove_the_whole_clip():
    with pytest.raises(ValueError, match="no duration"):
        _apply(_plan(), [{"op": "trim", "index": 0, "tail_seconds": 5}])


def test_set_adds_and_removes_the_audio_bed():
    new, _ = _apply(_plan(), [{"op": "set", "audio_bed": {"source_path": "/song.wav"},
                               "fade_out_seconds": 1}])
    assert new.audio_bed.source_path == "/song.wav" and new.fade_out_seconds == 1
    again, log = _apply(new, [{"op": "set", "audio_bed": None}])
    assert again.audio_bed is None and log == ["set audio_bed"]


def test_clip_spec_needs_exactly_one_length():
    with pytest.raises(ValidationError):
        ops.ClipSpec(source_path="/a", in_seconds=1)
    with pytest.raises(ValidationError):
        ops.ClipSpec(source_path="/a", in_seconds=3, out_seconds=2)


# ── validation ───────────────────────────────────────────────────────────────

@pytest.fixture
def fake_media(monkeypatch):
    infos: dict[str, MediaInfo] = {}
    monkeypatch.setattr(roughcut, "probe_media_cached", lambda p: infos.get(str(p)))
    return infos


def test_validate_reports_errors_and_warnings(fake_media):
    fake_media["/src/0.mov"] = _media(duration=11.0, fps=25.0)
    plan = RoughCutPlan(title="T", fps=24.0, default_transition_seconds=1.0, clips=[
        RoughCutClip("/src/0.mov", 0.0, 10.5),
        RoughCutClip("/src/0.mov", 10.0, 0.5),   # overlaps clip 0; shorter than the dissolve
    ])
    report = roughcut.validate_plan(plan)
    assert report["errors"] == ["transition 0->1 is 1.00s, longer than clip 1"]
    warnings = " | ".join(report["warnings"])
    assert "source file not found" not in warnings
    assert "25.000 fps" in warnings
    assert "reuse 0.50s" in warnings


def test_validate_flags_missing_handles_and_short_sources(fake_media):
    fake_media["/a.mov"] = _media(duration=5.0)
    plan = RoughCutPlan(title="T", fps=24.0, default_transition_seconds=1.0, clips=[
        RoughCutClip("/a.mov", 0.0, 4.8),
        RoughCutClip("/a.mov", 0.0, 2.0),
        RoughCutClip("/a.mov", 4.0, 2.0),
    ])
    warnings = " | ".join(roughcut.validate_plan(plan)["warnings"])
    assert "less than 0.50s of media after its out point" in warnings
    assert "starts 0.00s into its source" in warnings
    assert "ends at 6.00s but a.mov is only 5.00s long" in warnings


def test_validate_audio_bed(fake_media):
    fake_media["/song.wav"] = _media(duration=3.0)
    plan = _plan(2, audio_bed=RoughCutAudioBed("/song.wav", in_seconds=1.0))
    warnings = " | ".join(roughcut.validate_plan(plan)["warnings"])
    assert "audio bed: needs 4.00s" in warnings


def test_empty_plan_is_an_error():
    assert roughcut.validate_plan(RoughCutPlan(title="T"))["errors"] == ["the plan has no clips"]


# ── store ────────────────────────────────────────────────────────────────────

def test_store_roundtrip_and_summary():
    plan = _plan(3, audio_bed=RoughCutAudioBed("/song.wav"))
    pid = store.save_plan(plan)
    assert pid.startswith("t-")
    loaded = store.load_plan(pid)
    assert loaded == plan
    summary = store.plan_summary(pid, loaded)
    assert summary["sources"] == ["/src/0.mov", "/src/1.mov", "/src/2.mov"]
    assert summary["clips"][2] == {"index": 2, "source": 2, "in_seconds": 20.0, "out_seconds": 22.0,
                                   "duration_seconds": 2.0, "name": "c2", "rationale": ""}
    assert summary["total_seconds"] == 6.0


def test_store_rejects_unknown_and_unsafe_ids():
    with pytest.raises(ValueError, match="Unknown plan_id"):
        store.load_plan("nope-123456")
    with pytest.raises(ValueError, match="Invalid plan_id"):
        store.load_plan("../../etc/passwd")


def test_from_dict_accepts_out_seconds_and_ignores_unknown_keys():
    plan = RoughCutPlan.from_dict({"title": "x", "clips": [
        {"source_path": "/a", "in_seconds": 1, "out_seconds": 4, "score": 9},
    ]})
    assert plan.clips[0].duration_seconds == 3


# ── transcript drafting ──────────────────────────────────────────────────────

@pytest.fixture
def probed(monkeypatch):
    monkeypatch.setattr(roughcut, "probe_media", lambda p: _media(duration=30.0, fps=25.0))


def test_plan_from_transcript_never_repeats_words(probed):
    segs = [Segment(1.0, 3.0, "one"), Segment(3.1, 5.0, "two"), Segment(9.0, 10.0, "three")]
    plan = roughcut.plan_from_transcript("/a.mov", segs, pad_seconds=0.2)
    c0, c1, c2 = plan.clips
    assert c0.in_seconds == pytest.approx(0.8)
    assert c0.out_seconds == pytest.approx(3.05) == pytest.approx(c1.in_seconds)
    assert c2.in_seconds == pytest.approx(8.8) and c2.out_seconds == pytest.approx(10.2)
    assert plan.fps == 25.0


def test_plan_from_transcript_honors_segment_selection(probed):
    segs = [Segment(float(i), i + 0.9, f"s{i}") for i in range(5)]
    plan = roughcut.plan_from_transcript("/a.mov", segs, segment_indices=[4, 1])
    assert [c.rationale for c in plan.clips] == ["segment 4: s4", "segment 1: s1"]
    with pytest.raises(ValueError, match="out of range"):
        roughcut.plan_from_transcript("/a.mov", segs, segment_indices=[7])


def test_long_plans_omit_clip_lists_unless_asked():
    plan = _plan(61)
    auto = store.plan_summary("x", plan, include_clips=None)
    assert "clips" not in auto and "get_plan" in auto["clips_omitted"]
    assert len(store.plan_summary("x", plan)["clips"]) == 61
    assert "clips" in store.plan_summary("x", _plan(3), include_clips=None)


def test_dissolves_are_checked_after_frame_rounding():
    # 30 fps: clip 1 rounds to 7 frames, each 0.25 s dissolve to 8 frames -> they collide.
    plan = RoughCutPlan(title="T", fps=30.0, default_transition_seconds=0.25, clips=[
        RoughCutClip("/a.mov", 10, 0.5), RoughCutClip("/a.mov", 20, 0.25), RoughCutClip("/a.mov", 30, 1.0)])
    errors = roughcut.validate_plan(plan)["errors"]
    assert errors == ["clip 1 is 7 frames but the dissolves on its edges use 8; "
                      "shorten the dissolves or lengthen the clip"]


def test_sub_frame_values_are_flagged():
    plan = _plan(2, fade_in_seconds=0.01)
    plan.clips.append(RoughCutClip("/x.mov", 0, 0.01))
    report = roughcut.validate_plan(plan)
    assert "clip 2 is shorter than one frame at 24 fps" in report["errors"]
    assert any("fade_in_seconds=0.01 rounds to 0 frames" in w for w in report["warnings"])


def test_audio_bed_after_the_end_is_an_error():
    plan = _plan(2, audio_bed=RoughCutAudioBed("/song.wav", offset_seconds=10))
    assert any("after the timeline ends" in e for e in roughcut.validate_plan(plan)["errors"])


def test_update_with_a_new_source_keeps_the_duration():
    new, _ = _apply(_plan(), [{"op": "update", "index": 0, "source_path": "/b.mov", "in_seconds": 3.0}])
    assert new.clips[0].source_path == "/b.mov"
    assert new.clips[0].in_seconds == 3.0 and new.clips[0].duration_seconds == 2.0


def test_parallel_edits_do_not_lose_updates():
    import threading
    pid = store.save_plan(_plan(40))
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        with store.plan_lock(pid):
            plan, _ = ops.apply_ops(store.load_plan(pid), OPS.validate_python(
                [{"op": "trim", "index": i, "tail_seconds": 0.5}]))
            store.save_plan(plan, pid)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    durations = [c.duration_seconds for c in store.load_plan(pid).clips[:8]]
    assert durations == [1.5] * 8
