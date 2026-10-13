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


def test_zero_transition_forces_hard_cut_despite_default():
    spec = TimelineSpec(
        name="t", fps=24.0, default_transition_seconds=0.5,
        clips=[
            Clip(source_path="/tmp/a.mp4", in_seconds=0, duration_seconds=2,
                 transition_out_seconds=0),
            Clip(source_path="/tmp/b.mp4", in_seconds=0, duration_seconds=2),
            Clip(source_path="/tmp/c.mp4", in_seconds=0, duration_seconds=2),
        ],
    )
    xml = build_fcpxml(spec)
    assert xml.count("Cross Dissolve") == 1  # only b -> c uses the default


def test_plan_hard_cut_survives_clipspec_and_validation():
    from resolve_pilot.editorial import roughcut
    from resolve_pilot.editorial.ops import ClipSpec
    beats = [ClipSpec(source_path="/tmp/a.mp4", in_seconds=i * 0.5, duration_seconds=0.5,
                      transition_out_seconds=0) for i in range(4)]
    plan = roughcut.RoughCutPlan(title="beats", fps=24.0, default_transition_seconds=1.0,
                                 clips=[b.to_clip() for b in beats])
    assert not any("dissolve" in e for e in roughcut.validate_plan(plan)["errors"])
    assert "Cross Dissolve" not in build_fcpxml(roughcut.plan_to_timeline_spec(plan))


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


# ── Audio bed, asset URIs, frame-exact boundaries, SRT wrapping ─────────────

import xml.etree.ElementTree as ET  # noqa: E402

from resolve_pilot.exporters.fcpxml import AudioBed  # noqa: E402
from resolve_pilot.exporters.srt import wrap_cues  # noqa: E402


def _spine(xml: str) -> list[ET.Element]:
    return list(ET.fromstring(xml.encode()).find(".//spine"))


def test_audio_bed_is_a_connected_clip_on_the_first_spine_item():
    spec = _two_clip_spec(audio_bed=AudioBed(source_path="/tmp/song.wav", in_seconds=1.0))
    spec.clips[0].in_seconds = 2.0
    spine = _spine(build_fcpxml(spec))
    bed = spine[0].find("asset-clip")
    assert bed is not None and bed.get("lane") == "-1"
    # Offset is in the parent's source time: parent start (2 s) + timeline position (0).
    assert bed.get("offset") == "48/24s"
    assert bed.get("start") == "24/24s"
    assert bed.get("duration") == "96/24s"  # runs to the end of the 4 s timeline


def test_audio_bed_with_fade_in_hangs_off_the_gap():
    spec = _two_clip_spec(fade_in_seconds=1.0,
                          audio_bed=AudioBed(source_path="/tmp/song.wav", offset_seconds=0.5))
    spine = _spine(build_fcpxml(spec))
    assert spine[0].tag == "gap"
    bed = spine[0].find("asset-clip")
    assert bed.get("offset") == "12/24s" and bed.get("duration") == "108/24s"


def test_asset_src_is_a_proper_file_uri():
    spec = TimelineSpec(name="t", fps=24.0, clips=[
        Clip(source_path="/tmp/My Clips/şehir #1.mov", in_seconds=0, duration_seconds=1)])
    src = ET.fromstring(build_fcpxml(spec).encode()).find(".//asset").get("src")
    # /tmp may resolve elsewhere (macOS: /private/tmp), so check the encoding, not the prefix.
    assert src.startswith("file:///")
    assert src.endswith("/My%20Clips/%C5%9Fehir%20%231.mov")


def test_clip_boundaries_are_frame_exact_without_gaps():
    # 10 clips of 0.43 s at 24 fps: each is 10.32 frames, so per-clip rounding drifts.
    spec = TimelineSpec(name="t", fps=24.0, clips=[
        Clip(source_path="/tmp/a.mp4", in_seconds=0, duration_seconds=0.43) for _ in range(10)])
    spine = _spine(build_fcpxml(spec))

    def frames(v: str) -> int:
        return int(v.split("/")[0])

    pos = 0
    for el in spine:
        assert frames(el.get("offset")) == pos
        pos += frames(el.get("duration"))
    assert pos == round(4.3 * 24)


def test_wrap_cues_wraps_and_splits_proportionally():
    text = "one two three four five six seven eight nine ten eleven twelve"
    cues = wrap_cues([SrtCue(0.0, 6.0, text)], max_chars_per_line=20, max_lines=2)
    assert len(cues) == 2
    assert all(len(line) <= 20 for c in cues for line in c.text.split("\n"))
    assert cues[0].start == 0.0 and cues[-1].end == 6.0
    assert cues[0].end == cues[1].start
    assert " ".join(c.text.replace("\n", " ") for c in cues) == text


def test_wrap_cues_short_text_and_disabled():
    cue = SrtCue(1.0, 2.0, "short line")
    assert wrap_cues([cue])[0].text == "short line"
    long = SrtCue(0.0, 1.0, "x " * 50)
    assert wrap_cues([long], max_chars_per_line=0) == [long]


def test_audio_bed_is_audio_only_and_zero_length_elements_are_skipped():
    spec = _two_clip_spec(audio_bed=AudioBed(source_path="/tmp/song.wav"), fade_in_seconds=0.01)
    xml = build_fcpxml(spec)
    bed = [c for c in ET.fromstring(xml.encode()).iter("asset-clip") if c.get("lane") == "-1"]
    assert bed[0].get("srcEnable") == "audio"
    assert "<gap" not in xml  # 0.01 s fade rounds to 0 frames at 24 fps
    late = _two_clip_spec(audio_bed=AudioBed(source_path="/tmp/song.wav", offset_seconds=10))
    assert 'lane="-1"' not in build_fcpxml(late)
