"""Lazy bridge to DaVinci Resolve's scripting API.

Resolve ships its Python bridge as `DaVinciResolveScript` plus a compiled
`fusionscript` library. The bridge lives at platform-specific paths that
must be on `sys.path` and pointed at by `RESOLVE_SCRIPT_LIB`.

Loading is lazy and tolerant: if Studio is not installed or not running we
raise `StudioNotAvailable` with an actionable message rather than ImportError.
"""
from __future__ import annotations

import os
import platform
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


class StudioNotAvailable(RuntimeError):
    """Raised when the Studio scripting bridge can't be loaded.

    Distinct exception type so callers can fall back to Lua/UI workflows.
    """


def _candidate_paths() -> tuple[Path, Path]:
    """Return (modules_path, lib_path) for the current platform."""
    sysname = platform.system()
    if sysname == "Darwin":
        modules = Path(
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve"
            "/Developer/Scripting/Modules"
        )
        lib = Path(
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries"
            "/Fusion/fusionscript.so"
        )
    elif sysname == "Windows":
        modules = Path(
            os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            "Blackmagic Design", "DaVinci Resolve", "Support", "Developer",
            "Scripting", "Modules",
        )
        lib = Path(
            r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
        )
    elif sysname == "Linux":
        modules = Path("/opt/resolve/Developer/Scripting/Modules")
        lib = Path("/opt/resolve/libs/Fusion/fusionscript.so")
    else:
        raise StudioNotAvailable(f"Unsupported OS for Resolve scripting: {sysname}")
    return modules, lib


@lru_cache(maxsize=1)
def _load() -> Any:
    modules, lib = _candidate_paths()
    if not modules.exists():
        raise StudioNotAvailable(
            f"Resolve scripting modules not found at {modules}. "
            "Studio not installed, or this is the Free edition."
        )
    if not lib.exists():
        raise StudioNotAvailable(
            f"fusionscript library not found at {lib}. Studio install incomplete."
        )

    os.environ.setdefault("RESOLVE_SCRIPT_API", str(modules.parent))
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", str(lib))
    if str(modules) not in sys.path:
        sys.path.insert(0, str(modules))

    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except ImportError as e:
        raise StudioNotAvailable(
            f"Could not import DaVinciResolveScript: {e}. "
            "Set RESOLVE_SCRIPT_API/RESOLVE_SCRIPT_LIB env vars manually if needed."
        ) from e

    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise StudioNotAvailable(
            "scriptapp('Resolve') returned None. Is Resolve Studio running? "
            "Also check Preferences > System > General > External scripting → Local."
        )
    return resolve


def get_resolve() -> Any:
    """Return the live Resolve scripting handle, raising StudioNotAvailable on failure."""
    return _load()


def studio_available() -> bool:
    """Cheap probe: True if the Studio bridge loads, False otherwise."""
    try:
        _load()
        return True
    except StudioNotAvailable:
        return False
