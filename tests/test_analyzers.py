"""Analyzers against media generated with ffmpeg."""
from __future__ import annotations

import subprocess

import pytest

from resolve_pilot.analyzers import detect_beats, detect_scenes, estimate_tempo, probe_media
from tests.conftest import FFMPEG, needs_ffmpeg


@needs_ffmpeg
def test_beats_land_on_the_clicks(media):
    beats = detect_beats(media["song"])
    times = [b.pts_seconds for b in beats]
    assert times[0] == pytest.approx(0.5, abs=0.011)  # a click at t=0 is inside the first window
    assert len(times) == 11
    assert max(abs(t - round(t * 2) / 2) for t in times) < 0.011  # within one 10 ms block
    assert estimate_tempo(beats) == pytest.approx(120, abs=2)


@needs_ffmpeg
def test_a_song_that_starts_loud_keeps_its_beat_strengths(tmp_path):
    song = tmp_path / "loud_start.wav"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc='0.5*sin(2*PI*1000*t)+gte(t,0.25)*0.4*sin(2*PI*100*mod(t-0.25,0.5))"
                    "*exp(-mod(t-0.25,0.5)*30)':s=44100:d=10", str(song)], check=True)
    beats = detect_beats(song)
    kicks = [b for b in beats if b.strength > 0.3]
    assert len(kicks) >= 18 and kicks[0].pts_seconds == pytest.approx(0.25, abs=0.011)
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
