"""Transcript cache + background jobs, with a fake Whisper engine."""
from __future__ import annotations

import threading

import pytest

from resolve_pilot.transcribe import store, whisper
from resolve_pilot.transcribe.whisper import Segment, TranscriptionResult


@pytest.fixture
def fake_engine(monkeypatch):
    calls = []
    gate = threading.Event()
    gate.set()

    def fake_transcribe(path, model=None, language=None, word_timestamps=False, engine="auto"):
        calls.append((str(path), model, language))
        gate.wait(5)
        return TranscriptionResult(language=language or "en", text="a b",
                                   segments=[Segment(0.0, 1.0, " a "), Segment(1.0, 2.5, "b")])

    monkeypatch.setattr(store, "transcribe", fake_transcribe)
    monkeypatch.setattr(store, "resolve_engine", lambda engine="auto": "faster-whisper")
    return calls, gate


def test_transcript_is_cached_per_file_and_settings(tmp_path, fake_engine):
    calls, _ = fake_engine
    f = tmp_path / "talk.mp4"
    f.write_bytes(b"x")
    first = store.get_or_start(f, language="tr")
    assert first["status"] == "done" and first["cached"] is False
    assert first["language"] == "tr" and len(first["segments"]) == 2
    again = store.get_or_start(f, language="tr")
    assert again["cached"] is True and again["transcript_id"] == first["transcript_id"]
    assert len(calls) == 1
    other = store.get_or_start(f, language="en")
    assert other["transcript_id"] != first["transcript_id"] and len(calls) == 2


def test_slow_jobs_report_running_then_finish(tmp_path, fake_engine):
    calls, gate = fake_engine
    gate.clear()
    f = tmp_path / "long.mp4"
    f.write_bytes(b"x")
    running = store.get_or_start(f, wait_seconds=0.05)
    assert running["status"] == "running"
    again = store.get_or_start(f, wait_seconds=0.05)  # no second job is started
    assert again["status"] == "running" and len(calls) == 1
    gate.set()
    done = store.get_or_start(f, wait_seconds=5)
    assert done["status"] == "done" and done["transcript_id"] == running["transcript_id"]


def test_failures_surface_and_can_be_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "resolve_engine", lambda engine="auto": "mlx")
    monkeypatch.setattr(store, "transcribe", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="disk full"):
        store.get_or_start(f)
    with pytest.raises(RuntimeError, match="disk full"):
        store.get_or_start(f)


def test_format_segments_numbers_and_pages():
    segs = [{"start": i, "end": i + 0.5, "text": f" s{i} "} for i in range(5)]
    assert store.format_segments(segs, offset=3, limit=10) == ["[3] 3.00-3.50 s3", "[4] 4.00-4.50 s4"]
    assert len(store.format_segments(segs, limit=0)) == 5


def test_unknown_or_invalid_transcript_ids():
    with pytest.raises(ValueError, match="Unknown transcript_id"):
        store.require_transcript("t-000000000000")
    with pytest.raises(ValueError, match="Invalid transcript_id"):
        store.load_transcript("../x")


def test_model_names_map_per_engine():
    assert whisper.model_name_for("mlx", None) == "mlx-community/whisper-large-v3-turbo"
    assert whisper.model_name_for("mlx", "mlx-community/whisper-tiny") == "mlx-community/whisper-tiny"
    assert whisper.model_name_for("faster-whisper", None) == "large-v3-turbo"
    assert whisper.model_name_for("faster-whisper", "mlx-community/whisper-large-v3-mlx") == "large-v3"


def test_missing_engine_explains_install(monkeypatch):
    monkeypatch.setattr(whisper, "available_engines", lambda: [])
    with pytest.raises(RuntimeError, match="transcribe-cpu"):
        whisper.resolve_engine("auto")
