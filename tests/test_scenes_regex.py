"""Regression tests for the scdet regex — covers both ffmpeg output formats."""
from __future__ import annotations

from resolve_pilot.analyzers.scenes import _SCORE_RE, _TIME_RE


def _extract(line: str) -> tuple[float, float] | None:
    t = _TIME_RE.search(line)
    s = _SCORE_RE.search(line)
    if not t:
        return None
    return float(t.group(1)), float(s.group(1)) if s else 0.0


def test_colon_separator_modern_ffmpeg():
    line = "[Parsed_scdet_0 @ 0x14e605040] lavfi.scd.score: 19.499023, lavfi.scd.time: 6.853333"
    assert _extract(line) == (6.853333, 19.499023)


def test_equals_separator_legacy_ffmpeg():
    line = "[Parsed_scdet_0 @ 0x7f] lavfi.scd.time=6.853 lavfi.scd.score=19.499"
    assert _extract(line) == (6.853, 19.499)


def test_score_before_time_order():
    line = "lavfi.scd.score: 12.3, lavfi.scd.time: 0.5"
    assert _extract(line) == (0.5, 12.3)


def test_time_only_falls_back_to_zero_score():
    line = "lavfi.scd.time: 4.0"
    assert _extract(line) == (4.0, 0.0)


def test_unrelated_line_returns_none():
    assert _extract("frame= 100 fps= 25 q=-1.0 size=N/A") is None
