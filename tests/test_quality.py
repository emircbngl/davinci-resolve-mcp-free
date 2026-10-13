"""Quality analysis: silence / black / freeze / loud / quiet spans."""
import json
import math
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from resolve_pilot.analyzers import quality


def test_interval_parsers_include_open_ended_events():
    silence = quality._parse_intervals("silence_start: 1.5\nsilence_end: 2.5\nsilence_start: 8", "silence", 10)
    freeze = quality._parse_intervals("freeze_start: 3\nfreeze_end: 5", "freeze", 10)
    black = quality._parse_intervals("black_start:6 black_end:7 black_duration:1", "black", 10)
    assert [(s.start, s.end) for s in silence] == [(1.5, 2.5), (8, 10)]
    assert [(s.start, s.end) for s in freeze] == [(3, 5)]
    assert [(s.start, s.end) for s in black] == [(6, 7)]


def test_relative_threshold_uses_local_neighbourhood():
    values = [(0, -30), (1, -30), (2, -10), (40, -5), (41, -5), (42, -5)]
    spans = quality._relative_spans(values, "quiet", "loud", 5, 43)
    assert [(s.start, s.kind) for s in spans] == [(2, "loud")]


def test_unavailable_detectors_are_reported_as_skipped(tmp_path, monkeypatch):
    source = tmp_path / "clip.mov"
    source.touch()
    monkeypatch.setattr(quality, "probe_media", lambda _: SimpleNamespace(
        duration_seconds=10, fps=24, audio_codec="aac", video_codec="h264"))
    monkeypatch.setattr(quality.shutil, "which", lambda _: "/ffmpeg")
    calls = []

    def fake_run(command, timeout, **_kwargs):
        calls.append((command, timeout))
        if "-filters" in command:
            return subprocess.CompletedProcess(command, 0, " .S astats A->A\n .. silencedetect A->A\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(quality.subprocess, "run", fake_run)
    spans, meta = quality.analyze_quality(source)
    assert spans == []
    assert meta["detectors_ran"] == ["silencedetect", "astats"]
    assert set(meta["detectors_skipped"]) == {"blackdetect", "freezedetect"}
    assert "blurdetect" not in meta["detectors_ran"]
    assert "blurdetect" not in meta["detectors_skipped"]
    assert all(timeout is not None for _, timeout in calls)


def test_analyze_rejects_nonpositive_window():
    with pytest.raises(ValueError):
        quality.analyze_quality("anything", window_seconds=0)


def test_metadata_and_relative_spans_drop_nonfinite_samples():
    text = "\n".join([
        "pts_time:0", "lavfi.astats.Overall.RMS_level=-inf",
        "pts_time:1", "lavfi.astats.Overall.RMS_level=-20",
        "pts_time:2", "lavfi.astats.Overall.RMS_level=nan",
    ])
    assert quality._metadata_values(text, quality._RMS) == [(1.0, -20.0)]
    assert quality._relative_spans([(0, -20), (1, math.nan), (2, -20)],
                                   "quiet", "loud", 30, 3) == []


def test_mad_floor_ignores_tiny_near_constant_variation():
    values = [(float(index), -20.0 + (0.001 if index % 2 else 0.0)) for index in range(100)]
    assert quality._relative_spans(values, "quiet", "loud", 30, 100) == []


def test_adjacent_metadata_windows_merge_into_one_span():
    samples = [(float(index), -30.0) for index in range(10)]
    samples[4:7] = [(4.0, -10.0), (4.1, -10.0), (4.2, -10.0)]
    text = "\n".join(
        line
        for timestamp, value in samples
        for line in (f"pts_time:{timestamp}", f"lavfi.astats.Overall.RMS_level={value}")
    )
    raw = quality._relative_spans(quality._metadata_values(text, quality._RMS),
                                  "quiet", "loud", 30, 10)
    assert len(raw) == 3
    merged = quality._merge_adjacent_spans(raw, 0.5)
    assert [(span.start, span.end, span.kind, span.score) for span in merged] == [
        (4.0, 7.0, "loud", 1.0),
    ]


def test_analyze_merges_adjacent_metadata_windows(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.touch()
    samples = [(float(index), -30.0) for index in range(10)]
    samples[4:7] = [(4.0, -10.0), (4.1, -10.0), (4.2, -10.0)]
    metadata = "\n".join(
        line
        for timestamp, value in samples
        for line in (f"pts_time:{timestamp}", f"lavfi.astats.Overall.RMS_level={value}")
    )
    monkeypatch.setattr(quality, "probe_media", lambda _: SimpleNamespace(
        duration_seconds=10, fps=0, audio_codec="pcm_s16le", video_codec=None))
    monkeypatch.setattr(quality.shutil, "which", lambda _: "/ffmpeg")
    monkeypatch.setattr(quality, "_available_filters", lambda _: {"astats"})
    monkeypatch.setattr(quality, "_run", lambda _command, timeout: metadata)
    spans, _ = quality.analyze_quality(source)
    assert [(span.start, span.end, span.kind) for span in spans] == [(4.0, 7.0, "loud")]


def test_video_detectors_share_one_downscaled_pass(tmp_path, monkeypatch):
    source = tmp_path / "video.mov"
    source.touch()
    monkeypatch.setattr(quality, "probe_media", lambda _: SimpleNamespace(
        duration_seconds=10, fps=24, audio_codec=None, video_codec="h264"))
    monkeypatch.setattr(quality.shutil, "which", lambda _: "/ffmpeg")
    monkeypatch.setattr(quality, "_available_filters", lambda _: {"blackdetect", "freezedetect"})
    commands = []
    monkeypatch.setattr(quality, "_run",
                        lambda command, timeout: commands.append((command, timeout)) or "")
    _, meta = quality.analyze_quality(source)
    assert len(commands) == 1
    assert commands[0][0][commands[0][0].index("-vf") + 1] == (
        "scale=-2:360,blackdetect=d=0.5,freezedetect=d=1")
    assert commands[0][1] == 125.0
    assert meta["detectors_ran"] == ["blackdetect", "freezedetect"]


@pytest.mark.parametrize("gap", [-0.1, math.nan, math.inf])
def test_merge_gap_must_be_finite_and_nonnegative(gap):
    with pytest.raises(ValueError, match="merge_gap_seconds"):
        quality.analyze_quality("anything", merge_gap_seconds=gap)


def test_no_audio_stream_skips_audio_detectors(tmp_path, monkeypatch):
    source = tmp_path / "video.mov"
    source.touch()
    monkeypatch.setattr(quality, "probe_media", lambda _: SimpleNamespace(
        duration_seconds=10, fps=24, audio_codec=None, video_codec="h264"))
    monkeypatch.setattr(quality.shutil, "which", lambda _: "/ffmpeg")
    monkeypatch.setattr(quality, "_available_filters", lambda _: {"silencedetect", "astats"})
    monkeypatch.setattr(quality, "_run", lambda *_args, **_kwargs: pytest.fail("no detector runs"))
    spans, meta = quality.analyze_quality(source)
    assert spans == []
    assert meta["detectors_ran"] == []
    assert {"silencedetect", "astats"} <= set(meta["detectors_skipped"])


def test_detector_timeout_scales_with_media_duration(tmp_path, monkeypatch):
    source = tmp_path / "long.mov"
    source.touch()
    monkeypatch.setattr(quality, "probe_media", lambda _: SimpleNamespace(
        duration_seconds=3600, fps=24, audio_codec=None, video_codec="h264"))
    monkeypatch.setattr(quality.shutil, "which", lambda _: "/ffmpeg")
    monkeypatch.setattr(quality, "_available_filters", lambda _: {"blackdetect"})
    timeouts = []
    monkeypatch.setattr(quality, "_run", lambda _command, timeout: timeouts.append(timeout) or "")
    quality.analyze_quality(source)
    assert timeouts == [1920.0]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is unavailable")
def test_digitally_silent_audio_does_not_flag_every_frame(tmp_path):
    source = tmp_path / "silence.wav"
    subprocess.run([
        shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "1", str(source),
    ], check=True, timeout=20)
    spans, meta = quality.analyze_quality(source)
    assert "astats" in meta["detectors_ran"]
    assert not any(span.kind in {"quiet", "loud"} for span in spans)


@pytest.fixture(scope="module")
def flawed_clip(tmp_path_factory):
    """6 s: 2 s moving pattern + tone, 2 s black + silence, 2 s still orange + tone."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("quality")
    target = d / "flawed.mp4"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
        "-f", "lavfi", "-i", "color=c=black:size=320x180:rate=25:duration=2",
        "-f", "lavfi", "-i", "color=c=orange:size=320x180:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a]aresample=44100[a0];[5:a]aresample=44100[a2];"
        "[a0][4:a][a2]concat=n=3:v=0:a=1[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(target),
    ], check=True)
    return target


def test_real_ffmpeg_finds_black_silence_and_freeze(flawed_clip):
    from resolve_pilot import mcp_server

    out = mcp_server.analyze_quality(str(flawed_clip), kinds=["silence", "black", "freeze"])
    json.dumps(out)  # what the agent receives must serialize
    by_kind = {s["kind"]: s for s in out["spans"]}
    assert set(by_kind) == {"silence", "black", "freeze"}
    assert by_kind["black"]["start"] == pytest.approx(2.0, abs=0.1)
    assert by_kind["black"]["end"] == pytest.approx(4.0, abs=0.1)
    assert by_kind["silence"]["start"] == pytest.approx(2.0, abs=0.15)
    assert by_kind["silence"]["end"] == pytest.approx(4.0, abs=0.15)
    freezes = [(s["start"], s["end"]) for s in out["spans"] if s["kind"] == "freeze"]
    assert any(a <= 4.1 and b == pytest.approx(6.0, abs=0.1) for a, b in freezes), freezes
    assert out["counts"]["silence"] == 1 and out["counts"]["black"] == 1


def test_tool_filters_and_limits_spans(monkeypatch):
    from resolve_pilot import mcp_server

    spans = [quality.Span(0, 0.2, "silence", 1.0), quality.Span(1, 5, "loud", 0.5),
             quality.Span(6, 7, "black", 1.0)]
    monkeypatch.setattr(mcp_server.analyzers, "analyze_quality", lambda *a, **k: (
        spans, {"duration": 10, "detectors_ran": [], "detectors_skipped": []}))
    out = mcp_server.analyze_quality("x.mov", min_seconds=0.5, limit=1)
    assert out["counts"] == {"loud": 1, "black": 1}
    assert [s["kind"] for s in out["spans"]] == ["loud"]  # 0.5 x 4 s outweighs 1.0 x 1 s


def test_tiny_wobble_on_a_steady_level_is_not_loud():
    values = [(t / 10, -20.0 + (0.3 if t == 50 else 0.0)) for t in range(100)]
    assert quality._relative_spans(values, "quiet", "loud", 5, 10, min_delta=6.0) == []
    jump = [(t / 10, -20.0 + (12.0 if t == 50 else 0.0)) for t in range(100)]
    assert [s.kind for s in quality._relative_spans(jump, "quiet", "loud", 5, 10, min_delta=6.0)] == ["loud"]


def test_runs_merge_per_kind_and_implied_spans_are_dropped():
    spans = [quality.Span(0.0, 2.0, "black", 1.0), quality.Span(0.0, 2.0, "freeze", 1.0),
             quality.Span(0.0, 0.1, "loud", 1.0), quality.Span(0.1, 1.0, "loud", 0.8),
             quality.Span(5.0, 7.0, "freeze", 0.9),
             quality.Span(8.0, 10.0, "silence", 1.0), quality.Span(7.95, 10.05, "quiet", 1.0)]
    out = quality._drop_implied_spans(quality._merge_adjacent_spans(spans, 0.5))
    assert sorted((s.kind, s.start, s.end) for s in out) == [
        ("black", 0.0, 2.0), ("freeze", 5.0, 7.0), ("loud", 0.0, 1.0), ("silence", 8.0, 10.0)]
