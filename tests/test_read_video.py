"""read_video: transcript segments next to the frames on screen while they are spoken."""
from __future__ import annotations

import json
import subprocess

import pytest

from resolve_pilot import mcp_server
from resolve_pilot.analyzers import frames
from resolve_pilot.transcribe import store
from resolve_pilot.transcribe.whisper import Segment, TranscriptionResult
from tests.conftest import FFMPEG

SEGMENTS = [Segment(0.0, 3.0, "red words"), Segment(3.0, 6.0, "green words"),
            Segment(6.0, 9.0, "blue words")]


@pytest.fixture(scope="module")
def color_video(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    target = tmp_path_factory.mktemp("read") / "colors.mkv"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:s=160x90:d=3:r=25",
        "-f", "lavfi", "-i", "color=c=lime:s=160x90:d=3:r=25",
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=3:r=25",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
        "-map", "[out]", "-c:v", "ffv1", "-y", str(target),
    ], check=True)
    return target


@pytest.fixture
def whisper(monkeypatch):
    """Stand in for Whisper at the transcription boundary; counts calls."""
    calls = []

    def fake(path, model=None, language=None, word_timestamps=False, engine="auto"):
        calls.append(str(path))
        return TranscriptionResult(language="en", text="", segments=list(SEGMENTS))

    monkeypatch.setattr(store, "transcribe", fake)
    monkeypatch.setattr(store, "resolve_engine", lambda engine="auto": "faster-whisper")
    return calls


def _split(result):
    return json.loads(result.content[0].text), result.content[1:]


def test_segment_groups_are_contiguous_and_cover_everything():
    assert mcp_server._segment_groups(3, 12) == [(0, 0), (1, 1), (2, 2)]
    groups = mcp_server._segment_groups(10, 4)
    assert groups[0][0] == 0 and groups[-1][1] == 9
    assert all(b[0] == a[1] + 1 for a, b in zip(groups, groups[1:]))


def test_pairs_each_segment_with_its_frame_and_reuses_the_transcript_cache(color_video, whisper):
    header, blocks = _split(mcp_server.read_video(str(color_video)))
    assert header["ok"] is True and header["segment_count"] == 3 and header["missing_frames"] == []
    assert [b.text for b in blocks[0::2]] == [
        "[0] 0.0-3.0s | speech: red words",
        "[1] 3.0-6.0s | speech: green words",
        "[2] 6.0-9.0s | speech: blue words",
    ]
    assert all(b.type == "image" for b in blocks[1::2])
    again, _ = _split(mcp_server.read_video(transcript_id=header["transcript_id"]))
    assert again["transcript_id"] == header["transcript_id"]
    mcp_server.read_video(str(color_video))
    assert len(whisper) == 1  # transcribed once, then served from the cache


def test_a_dropped_frame_is_reported_not_silently_lost(color_video, whisper, monkeypatch):
    real = frames.subprocess.run

    def flaky(cmd, **kw):  # the middle segment's frame (t=4.5) fails
        if "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "4.500":
            return subprocess.CompletedProcess(cmd, 1, "", "corrupt packet")
        return real(cmd, **kw)

    monkeypatch.setattr(frames.subprocess, "run", flaky)
    header, blocks = _split(mcp_server.read_video(str(color_video)))
    assert header["returned"] == 2
    assert header["missing_frames"] == [{"segments": [1, 1], "start": 3.0, "end": 6.0,
                                         "speech": "green words",
                                         "reason": "ffmpeg produced no frame: corrupt packet"}]
    kinds = [b.type for b in blocks]
    assert kinds == ["text", "image", "text", "text", "image"]
    assert blocks[2].text.startswith("[1] 3.0-6.0s | speech: green words (no frame:")
    assert blocks[3].text.startswith("[2]")


def test_many_segments_are_grouped_into_max_frames(color_video, monkeypatch):
    many = [Segment(i * 0.5, i * 0.5 + 0.5, f"w{i}") for i in range(18)]
    monkeypatch.setattr(store, "transcribe", lambda *a, **k: TranscriptionResult("en", "", many))
    monkeypatch.setattr(store, "resolve_engine", lambda engine="auto": "faster-whisper")
    header, blocks = _split(mcp_server.read_video(str(color_video), max_frames=3, layout="grid"))
    assert header["grouped"] is True and header["grid"] == {"cols": 3, "rows": 1}
    label = blocks[0].text
    assert "cell 1: [0-5] 0.0-3.0s | speech: w0 w1 w2 w3 w4 w5" in label
    assert "cell 3: [12-17]" in label


def test_still_transcribing_returns_status_to_poll(color_video, monkeypatch):
    monkeypatch.setattr(store, "get_or_start", lambda *a, **k: {
        "status": "running", "transcript_id": "t-0123456789ab", "elapsed_seconds": 45.0})
    header, blocks = _split(mcp_server.read_video(str(color_video)))
    assert header["status"] == "running" and "again" in header["hint"] and blocks == []


def test_needs_a_source():
    with pytest.raises(ValueError, match="path"):
        mcp_server.read_video()


def test_audio_only_source_returns_the_speech_without_frame_errors(tmp_path, whisper):
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    audio = tmp_path / "voice.m4a"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=9", "-y", str(audio)], check=True)
    header, blocks = _split(mcp_server.read_video(str(audio)))
    assert header["ok"] is True and "no video stream" in header["hint"]
    assert header["segments"][1] == "[1] 3.00-6.00 green words"
    assert blocks == [] and "missing_frames" not in header
