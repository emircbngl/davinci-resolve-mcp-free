"""Host side of the live Lua bridge.

The bridge (``bridge.lua``) runs inside DaVinci Resolve — started once per session
from Workspace › Scripts or pasted into the Console — and polls a folder for
requests. This module writes requests into that folder and waits for answers,
which gives the agent a read/act/verify loop in the Free edition, where
Resolve's external scripting API is disabled.
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from resolve_pilot import __version__
from resolve_pilot._paths import home
from resolve_pilot.lua_snippets.library import lua_string

SCRIPT_NAME = "ResolvePilotBridge.lua"
HEARTBEAT_STALE_SECONDS = 8.0
DEFAULT_IDLE_MINUTES = 60
_TEMPLATE = Path(__file__).with_name("bridge.lua")
_lock = threading.Lock()

START_HINT = (
    "Ask the user to start the bridge inside Resolve: Workspace > Scripts > "
    "ResolvePilotBridge (put it there with setup_bridge(action='install')), or paste "
    "the script from setup_bridge(action='show_script') into Workspace > Console "
    "(Lua tab) and press Enter."
)


class BridgeError(RuntimeError):
    """The bridge answered badly or not at all."""


class BridgeNotRunning(BridgeError):
    """No live bridge: nothing inside Resolve is reading requests."""


@dataclass
class BridgeStatus:
    running: bool
    directory: str
    heartbeat_age_seconds: float | None = None
    bridge_version: str | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BridgeResult:
    ok: bool
    output: str
    result: Any
    error: str | None
    elapsed_seconds: float

    def to_dict(self) -> dict:
        return asdict(self)


def bridge_dir() -> Path:
    """Folder shared with the bridge. ``RESOLVE_PILOT_BRIDGE_DIR`` overrides it."""
    env = os.environ.get("RESOLVE_PILOT_BRIDGE_DIR")
    p = Path(env).expanduser() if env else home() / "bridge"
    p.mkdir(parents=True, exist_ok=True)
    return p


def render_script(idle_minutes: int = DEFAULT_IDLE_MINUTES, directory: Path | None = None) -> str:
    """Return the bridge Lua with this machine's folder and settings baked in."""
    d = directory or bridge_dir()
    minutes = max(1, int(idle_minutes))
    return (
        _TEMPLATE.read_text(encoding="utf-8")
        .replace("__RP_BRIDGE_DIR__", lua_string(str(d)))
        .replace("__RP_VERSION_TEXT__", __version__)
        .replace("__RP_VERSION__", lua_string(__version__))
        .replace("__RP_IDLE_SECONDS__", str(minutes * 60))
        .replace("__RP_IDLE_MIN__", str(minutes))
    )


def scripts_menu_dir(scope: str = "user") -> Path:
    """Resolve's Workspace › Scripts folder for Utility scripts on this OS."""
    if scope not in ("user", "all"):
        raise ValueError("scope must be 'user' or 'all'")
    sysname = platform.system()
    if sysname == "Darwin":
        base = (
            Path.home() / "Library" if scope == "user" else Path("/Library")
        ) / "Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts"
    elif sysname == "Windows":
        if scope == "user":
            appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            base = Path(appdata) / "Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts"
        else:
            programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            base = Path(programdata) / "Blackmagic Design/DaVinci Resolve/Fusion/Scripts"
    else:
        base = (
            Path.home() / ".local/share/DaVinciResolve/Fusion/Scripts"
            if scope == "user" else Path("/opt/resolve/Fusion/Scripts")
        )
    return base / "Utility"


def install_script(idle_minutes: int = DEFAULT_IDLE_MINUTES, scope: str = "user") -> Path:
    """Write the bridge into Resolve's Scripts menu folder and return its path."""
    target = scripts_menu_dir(scope)
    target.mkdir(parents=True, exist_ok=True)
    path = target / SCRIPT_NAME
    path.write_text(render_script(idle_minutes), encoding="utf-8")
    return path


def status() -> BridgeStatus:
    d = bridge_dir()
    beat = d / "heartbeat"
    try:
        fields = beat.read_text(encoding="utf-8", errors="replace").split()
        mtime = beat.stat().st_mtime
    except FileNotFoundError:
        return BridgeStatus(False, str(d), detail="Bridge is not running. " + START_HINT)
    try:
        epoch = float(fields[0])
    except (IndexError, ValueError):
        epoch = mtime
    age = round(max(0.0, time.time() - epoch), 1)
    version = fields[1] if len(fields) > 1 else None
    if age > HEARTBEAT_STALE_SECONDS:
        return BridgeStatus(
            False, str(d), age, version,
            f"Bridge heartbeat is {age:.0f}s old: it stopped, or Resolve is busy. " + START_HINT,
        )
    detail = "Bridge is running."
    if version != __version__:
        detail += (
            f" Its script comes from resolve-pilot {version} (server is {__version__}); "
            "reinstall/restart it if something misbehaves."
        )
    return BridgeStatus(True, str(d), age, version, detail)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".host-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def exec_lua(code: str, timeout: float = 30.0) -> BridgeResult:
    """Run ``code`` inside Resolve and return what it printed and returned.

    Raises BridgeNotRunning when no bridge is listening and BridgeError when it
    picked the request up but did not answer in time.
    """
    st = status()
    if not st.running:
        raise BridgeNotRunning(st.detail)
    d = Path(st.directory)
    req, resp = d / "request.lua", d / "response.json"
    rid = uuid.uuid4().hex[:12]
    with _lock:
        _remove(resp)
        start = time.monotonic()
        _write_atomic(req, f"-- rp-id: {rid}\n{code}")
        while time.monotonic() - start < timeout:
            try:
                raw = resp.read_bytes()
            except FileNotFoundError:
                raw = None
            if raw is not None:
                try:
                    payload = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and payload.get("id") == rid:
                    _remove(resp)
                    return BridgeResult(
                        ok=bool(payload.get("ok")),
                        output=payload.get("output") or "",
                        result=payload.get("result"),
                        error=payload.get("error"),
                        elapsed_seconds=round(time.monotonic() - start, 3),
                    )
                if payload is not None:
                    _remove(resp)  # an answer to an older request
            time.sleep(0.05)
        picked_up = not req.exists()
        if not picked_up:
            _remove(req)
    if picked_up:
        raise BridgeError(
            f"Resolve took the request but did not answer within {timeout:g}s. The Lua may "
            "still be running (long imports or renders); check get_resolve_state before retrying."
        )
    raise BridgeNotRunning(f"The bridge did not pick up the request within {timeout:g}s. " + START_HINT)


def stop() -> bool:
    """Ask a running bridge to exit. Returns whether one was running."""
    st = status()
    if st.running:
        (Path(st.directory) / "stop").write_text("stop", encoding="utf-8")
    return st.running
