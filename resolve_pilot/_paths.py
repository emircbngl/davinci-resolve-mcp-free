"""Where resolve-pilot keeps its state: plans, transcripts, bridge files.

Everything lives under ``~/.resolve-pilot`` unless ``RESOLVE_PILOT_HOME`` points
somewhere else (tests use that to stay out of the real home directory).
"""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    root = os.environ.get("RESOLVE_PILOT_HOME")
    return Path(root).expanduser() if root else Path.home() / ".resolve-pilot"


def state_dir(name: str) -> Path:
    """Return ``<home>/<name>``, creating it on first use."""
    p = home() / name
    p.mkdir(parents=True, exist_ok=True)
    return p
