"""Transcript cache and background transcription jobs.

Transcribing a long interview takes minutes, longer than many MCP clients wait
for a tool call. Jobs therefore run on a background thread: the tool waits a
bounded time, then reports ``status="running"`` and the agent simply calls again.
Finished transcripts are cached on disk, keyed by file identity + engine +
model + language, so asking again (even in a later conversation) is instant.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from resolve_pilot._paths import state_dir
from resolve_pilot.transcribe.whisper import Segment, model_name_for, resolve_engine, transcribe

_ID_RE = re.compile(r"^t-[0-9a-f]{12}$")
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def transcripts_dir() -> Path:
    return state_dir("transcripts")


def transcript_id(path: Path, engine: str, model: str, language: str | None, word_timestamps: bool) -> str:
    st = path.stat()
    key = json.dumps([str(path), st.st_size, int(st.st_mtime), engine, model, language or "",
                      bool(word_timestamps)])
    return "t-" + hashlib.sha1(key.encode()).hexdigest()[:12]


def load_transcript(tid: str) -> dict[str, Any] | None:
    if not _ID_RE.match(tid or ""):
        raise ValueError(f"Invalid transcript_id {tid!r} (expected something like 't-0123456789ab')")
    try:
        return json.loads((transcripts_dir() / f"{tid}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def require_transcript(tid: str) -> dict[str, Any]:
    data = load_transcript(tid)
    if data is None:
        raise ValueError(f"Unknown transcript_id {tid!r}. Run transcribe_audio first.")
    return data


def transcript_segments(data: dict[str, Any]) -> list[Segment]:
    return [Segment(float(s["start"]), float(s["end"]), str(s["text"])) for s in data["segments"]]


def _run(tid: str, path: Path, model: str, language: str | None, word_timestamps: bool, engine: str) -> None:
    job = _jobs[tid]
    try:
        result = transcribe(path, model=model, language=language,
                            word_timestamps=word_timestamps, engine=engine)
        data = {
            "transcript_id": tid,
            "source_path": str(path),
            "engine": engine,
            "model": model,
            "language": result.language,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "segments": [s.to_dict() for s in result.segments],
            "text": result.text,
        }
        out = transcripts_dir() / f"{tid}.json"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(out)
    except BaseException as e:  # surfaced to the next caller
        job["error"] = e
    finally:
        job["done"].set()


def get_or_start(
    path: str | Path,
    model: str | None = None,
    language: str | None = None,
    word_timestamps: bool = False,
    engine: str = "auto",
    wait_seconds: float = 45.0,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return the cached transcript, or start/await a background job.

    Result: ``{"status": "done", "cached": bool, ...transcript}`` or
    ``{"status": "running", "transcript_id", "elapsed_seconds"}``.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    eng = resolve_engine(engine)
    name = model_name_for(eng, model)
    tid = transcript_id(p, eng, name, language, word_timestamps)

    if not refresh:
        cached = load_transcript(tid)
        if cached is not None:
            return {"status": "done", "cached": True, **cached}

    with _jobs_lock:
        job = _jobs.get(tid)
        if job is None or (refresh and job["done"].is_set()):
            job = {"done": threading.Event(), "error": None, "started": time.monotonic()}
            _jobs[tid] = job
            threading.Thread(
                target=_run, args=(tid, p, name, language, word_timestamps, eng),
                name=f"transcribe-{tid}", daemon=True,
            ).start()

    if not job["done"].wait(timeout=max(0.0, wait_seconds)):
        return {
            "status": "running",
            "transcript_id": tid,
            "elapsed_seconds": round(time.monotonic() - job["started"], 1),
        }
    with _jobs_lock:
        _jobs.pop(tid, None)
    if job["error"] is not None:
        raise RuntimeError(f"Transcription failed: {job['error']}") from job["error"]
    return {"status": "done", "cached": False, **require_transcript(tid)}


def format_segments(segments: list[dict[str, Any]], offset: int = 0, limit: int = 200) -> list[str]:
    """Numbered one-line-per-segment view: ``[i] start-end text``."""
    rows = segments[offset:offset + limit] if limit > 0 else segments[offset:]
    return [
        f"[{i}] {float(s['start']):.2f}-{float(s['end']):.2f} {str(s['text']).strip()}"
        for i, s in enumerate(rows, start=offset)
    ]
