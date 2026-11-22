"""Long work behind a tool call: start it once, answer "running" until it is done.

An MCP call should not hang for minutes, so the tool returns after `wait_seconds` and the
agent calls again with the same arguments; the same key finds the same job.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def run(key: str, work: Callable[[Callable[[int, int], None]], Any], wait_seconds: float) -> dict[str, Any]:
    """``{"status": "done", "result": ...}`` or ``{"status": "running", "elapsed_seconds",
    "progress"}``. `work(report)` runs once per key in a background thread and may call
    ``report(done, total)``; an exception in it is raised to the caller that collects it."""
    with _lock:
        job = _jobs.get(key)
        if job is None:
            job = {"done": threading.Event(), "error": None, "result": None,
                   "started": time.monotonic(), "progress": None}
            _jobs[key] = job

            def target(job: dict[str, Any] = job) -> None:
                try:
                    job["result"] = work(lambda done, total: job.update(progress=[done, total]))
                except BaseException as e:  # surfaced to the next caller
                    job["error"] = e
                finally:
                    job["done"].set()

            threading.Thread(target=target, name=f"job-{key}", daemon=True).start()
    if not job["done"].wait(timeout=max(0.0, wait_seconds)):
        return {"status": "running", "elapsed_seconds": round(time.monotonic() - job["started"], 1),
                "progress": job["progress"]}
    with _lock:
        _jobs.pop(key, None)
    if job["error"] is not None:
        raise job["error"]
    return {"status": "done", "result": job["result"]}
