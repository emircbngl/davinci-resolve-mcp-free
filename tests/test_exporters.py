"""Unit tests for the exporter primitives."""
from __future__ import annotations

from resolve_pilot.editorial.roughcut import RoughCutPlan
from resolve_pilot.exporters.fcpxml import (
    Clip,
    TimelineSpec,
    _frame_duration,
    _rational,
    _transition_duration_for,
    build_fcpxml,
)
from resolve_pilot.exporters.srt import SrtCue, _fmt_timestamp, build_srt


def test_rational_integer_fps_is_simple():
    assert _rational(1.0, 24.0) == "24/24s"
    assert _rational(2.5, 30.0) == "75/30s"


def test_rational_fractional_fps_uses_1001_denominator():
    # 23.976 ≈ 24000/1001 → one second is 24000/24000s, but rounded to nearest frame
    out = _rational(1.0, 23.976)
    assert out.endswith("/24000s")
    frames, _ = out.split("/")
    assert int(frames) == 24 * 1001


def test_frame_duration_integer():
    assert _frame_duration(24.0) == "1/24s"


def test_frame_duration_fractional():
    assert _frame_duration(23.976) == "1001/24000s"


def test_build_fcpxml_smoke():
    spec = TimelineSpec(
        name="t",
        fps=24.0,
        clips=[Clip(source_path="/tmp/sample.mp4", in_seconds=0.0, duration_seconds=1.0)],
    )
    xml = build_fcpxml(spec)
    assert '<fcpxml version="1.10">' in xml
    assert "asset-clip" in xml


def test_srt_timestamp_zero():
    assert _fmt_timestamp(0.0) == "00:00:00,000"


def test_srt_timestamp_negative_clamps_to_zero():
    assert _fmt_timestamp(-1.0) == "00:00:00,000"


def test_srt_timestamp_hours_minutes_ms():
    # 1h 2m 3.456s
    assert _fmt_timestamp(3723.456) == "01:02:03,456"


def test_build_srt_roundtrip():
    text = build_srt([
        SrtCue(start=0.0, end=1.5, text="hello"),
        SrtCue(start=1.5, end=3.0, text="world"),
    ])
    assert "1\n00:00:00,000 --> 00:00:01,500\nhello" in text
    assert "2\n00:00:01,500 --> 00:00:03,000\nworld" in text


# ── Transitions & fades ────────────────────────────────────────────────────


def _two_clip_spec(**kwargs) -> TimelineSpec:
    return TimelineSpec(
        name="t",
        fps=24.0,
        clips=[
            Clip(source_path="/tmp/a.mp4", in_seconds=0.0, duration_seconds=2.0),
            Clip(source_path="/tmp/b.mp4", in_seconds=0.0, duration_seconds=2.0),
        ],
        **kwargs,
    )


def test_no_transition_emitted_when_default_is_zero():
    xml = build_fcpxml(_two_clip_spec())
    assert "Cross Dissolve" not in xml
    assert "<gap" not in xml


def test_default_transition_emits_cross_dissolve_between_every_cut():
    xml = build_fcpxml(_two_clip_spec(default_transition_seconds=0.5))
    assert xml.count('name="Cross Dissolve"') == 1  # one cut, one transition
    # 0.5s @ 24fps = 12 frames
    assert 'duration="12/24s"' in xml


def test_per_clip_transition_overrides_default():
    spec = TimelineSpec(
        name="t", fps=24.0, default_transition_seconds=0.5,
        clips=[
            Clip(source_path="/tmp/a.mp4", in_seconds=0, duration_seconds=2,
                 transition_out_seconds=1.0),
            Clip(source_path="/tmp/b.mp4", in_seconds=0, duration_seconds=2),
        ],
    )
    xml = build_fcpxml(spec)
    assert 'duration="24/24s"' in xml  # 1.0s @ 24fps overrides
    assert 'duration="12/24s"' not in xml


def test_last_clip_transition_out_is_ignored():
    spec = TimelineSpec(
        name="t", fps=24.0,
        clips=[
            Clip(source_path="/tmp/a.mp4", in_seconds=0, duration_seconds=2),
            Clip(source_path="/tmp/b.mp4", in_seconds=0, duration_seconds=2,
                 transition_out_seconds=1.0),  # set on last clip — no next, so noop
        ],
    )
    xml = build_fcpxml(spec)
    assert "Cross Dissolve" not in xml


def test_fade_in_prepends_gap_and_transition():
    xml = build_fcpxml(_two_clip_spec(fade_in_seconds=0.5))
    assert '<gap name="Gap"' in xml
    assert "Cross Dissolve" in xml
    # Gap is the first spine element (offset 0)
    assert 'offset="0/24s"' in xml


def test_fade_out_appends_transition_and_gap():
    xml = build_fcpxml(_two_clip_spec(fade_out_seconds=0.5))
    assert '<gap name="Gap"' in xml
    assert "Cross Dissolve" in xml


def test_fade_in_extends_sequence_duration():
    xml_no_fade = build_fcpxml(_two_clip_spec())
    xml_with_fade = build_fcpxml(_two_clip_spec(fade_in_seconds=1.0, fade_out_seconds=1.0))
    # 2 clips × 2s = 4s; with 1s fades on both ends = 6s
    assert 'duration="96/24s"' in xml_no_fade   # 4 * 24 = 96
    assert 'duration="144/24s"' in xml_with_fade  # 6 * 24 = 144


def test_transition_duration_helper_prefers_per_clip():
    clip = Clip(source_path="/tmp/x.mp4", in_seconds=0, duration_seconds=1,
                transition_out_seconds=0.7)
    assert _transition_duration_for(clip, 0.3) == 0.7
    clip2 = Clip(source_path="/tmp/x.mp4", in_seconds=0, duration_seconds=1)
    assert _transition_duration_for(clip2, 0.3) == 0.3
    assert _transition_duration_for(clip2, 0.0) == 0.0


def test_roughcut_plan_roundtrips_transition_fields():
    data = {
        "title": "t",
        "fps": 24,
        "width": 1920,
        "height": 1080,
        "clips": [{
            "source_path": "/tmp/a.mp4", "in_seconds": 0.0, "duration_seconds": 2.0,
            "rationale": "x", "name": None, "transition_out_seconds": 0.5,
        }],
        "notes": "",
        "default_transition_seconds": 0.25,
        "fade_in_seconds": 1.0,
        "fade_out_seconds": 1.5,
    }
    plan = RoughCutPlan.from_dict(data)
    assert plan.default_transition_seconds == 0.25
    assert plan.fade_in_seconds == 1.0
    assert plan.fade_out_seconds == 1.5
    assert plan.clips[0].transition_out_seconds == 0.5
    # Roundtrip through to_dict/from_dict
    again = RoughCutPlan.from_dict(plan.to_dict())
    assert again.default_transition_seconds == 0.25
    assert again.clips[0].transition_out_seconds == 0.5
