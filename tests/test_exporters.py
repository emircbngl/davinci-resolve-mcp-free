"""Unit tests for the exporter primitives."""
from __future__ import annotations

from resolve_pilot.exporters.fcpxml import (
    Clip,
    TimelineSpec,
    _frame_duration,
    _rational,
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
