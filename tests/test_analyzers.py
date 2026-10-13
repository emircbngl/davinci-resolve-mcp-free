"""Analyzers against media generated with ffmpeg."""
from __future__ import annotations

import subprocess

import pytest

from resolve_pilot.analyzers import detect_beats, detect_scenes, estimate_tempo, probe_media
from tests.conftest import FFMPEG, needs_ffmpeg


@needs_ffmpeg
def test_beats_land_on_the_clicks_including_the_first(media):
    beats = detect_beats(media["song"])
    times = [b.pts_seconds for b in beats]
    assert times[0] == 0.0
    assert len(times) == 12
    assert max(abs(t - round(t * 2) / 2) for t in times) < 0.011  # within one 10 ms block
    assert estimate_tempo(beats) == pytest.approx(120, abs=2)


@needs_ffmpeg
def test_scene_detection_reports_ffmpeg_failures(tmp_path):
    bad = tmp_path / "not-a-video.mp4"
    bad.write_bytes(b"garbage")
    with pytest.raises(RuntimeError, match="scene detection failed"):
        detect_scenes(bad)


@needs_ffmpeg
def test_cover_art_is_not_treated_as_video(media, tmp_path):
    cover = tmp_path / "cover.png"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=blue:size=300x300", "-frames:v", "1", str(cover)], check=True)
    song = tmp_path / "song.m4a"
    made = subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(media["song"]), "-i", str(cover),
        "-map", "0:a", "-map", "1:v", "-c:a", "aac", "-c:v", "mjpeg",
        "-disposition:v:0", "attached_pic", str(song)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("this ffmpeg cannot write cover art")
    info = probe_media(song)
    assert info.video_codec is None and info.width is None
    assert info.audio_codec == "aac"
    assert probe_media(media["clip"]).video_codec == "h264"
