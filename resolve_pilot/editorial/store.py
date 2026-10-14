"""Server-side storage for rough-cut plans.

Plans live as JSON files under ``~/.resolve-pilot/plans`` so they survive a
server restart (Claude Desktop restarts MCP servers freely). Tools pass around
a short ``plan_id`` instead of the whole plan.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from resolve_pilot._filelock import LockTimeout, file_lock
from resolve_pilot._paths import state_dir
from resolve_pilot.editorial.roughcut import RoughCutPlan, validate_plan

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def plans_dir() -> Path:
    return state_dir("plans")


def new_plan_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32].strip("-") or "plan"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def _path(plan_id: str) -> Path:
    if not _ID_RE.match(plan_id or ""):
        raise ValueError(f"Invalid plan_id {plan_id!r}")
    return plans_dir() / f"{plan_id}.json"


@contextmanager
def plan_lock(plan_id: str) -> Iterator[None]:
    """Hold while reading, changing and saving one plan.

    mcp 2.x runs tool calls in parallel threads, and several MCP servers (Claude
    Desktop + Claude Code) share the plan folder; without this, two edit_plan
    calls on the same plan lose one edit.
    """
    path = _path(plan_id).with_suffix(".lock")
    with _locks_guard:
        lock = _locks.setdefault(plan_id, threading.Lock())
    with lock:
        try:
            with file_lock(path, timeout=30.0):
                yield
        except LockTimeout:
            raise ValueError(
                f"Plan {plan_id!r} is being edited by another session; retry shortly."
            ) from None


def save_plan(plan: RoughCutPlan, plan_id: str | None = None) -> str:
    pid = plan_id or new_plan_id(plan.title)
    path = _path(pid)
    tmp = path.with_name(f"{pid}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(plan.to_dict(), indent=1), encoding="utf-8")
    tmp.replace(path)
    return pid


def load_plan(plan_id: str) -> RoughCutPlan:
    path = _path(plan_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        known = sorted(p.stem for p in plans_dir().glob("*.json"))[-10:]
        raise ValueError(
            f"Unknown plan_id {plan_id!r}. Create one with plan_from_transcript, plan_from_scenes "
            f"or create_plan." + (f" Recent plans: {', '.join(known)}" if known else "")
        ) from None
    return RoughCutPlan.from_dict(data)


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


AUTO_LIST_MAX_CLIPS = 60


def plan_summary(plan_id: str, plan: RoughCutPlan, include_clips: bool | None = True) -> dict[str, Any]:
    """Compact, agent-friendly view of a plan plus its validation report.

    Source paths are listed once in ``sources``; each clip refers to one by index.
    ``include_clips=None`` lists clips only for plans of up to 60 clips, which keeps
    repeated edits of long plans cheap.
    """
    sources = list(dict.fromkeys(c.source_path for c in plan.clips))
    out: dict[str, Any] = {
        "plan_id": plan_id,
        "title": plan.title,
        "fps": plan.fps,
        "resolution": f"{plan.width}x{plan.height}",
        "clip_count": len(plan.clips),
        "total_seconds": _r(plan.total_seconds),
        "default_transition_seconds": plan.default_transition_seconds,
        "fade_in_seconds": plan.fade_in_seconds,
        "fade_out_seconds": plan.fade_out_seconds,
        "audio_bed": plan.audio_bed.__dict__ if plan.audio_bed else None,
        "notes": plan.notes,
        **validate_plan(plan),
        "sources": sources,
    }
    if include_clips is None:
        include_clips = len(plan.clips) <= AUTO_LIST_MAX_CLIPS
        if not include_clips:
            out["clips_omitted"] = "Long plan: call get_plan(plan_id) to list the clips."
    if include_clips:
        index = {s: i for i, s in enumerate(sources)}
        clips = []
        for i, c in enumerate(plan.clips):
            row: dict[str, Any] = {
                "index": i,
                "source": index[c.source_path],
                "in_seconds": _r(c.in_seconds),
                "out_seconds": _r(c.out_seconds),
                "duration_seconds": _r(c.duration_seconds),
                "name": c.name,
                "rationale": c.rationale,
            }
            if c.transition_out_seconds is not None:
                row["transition_out_seconds"] = c.transition_out_seconds
            clips.append(row)
        out["clips"] = clips
    return out
