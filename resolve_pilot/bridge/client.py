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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from resolve_pilot import __version__
from resolve_pilot._paths import home
from resolve_pilot.lua_snippets.library import lua_string

SCRIPT_NAME = "ResolvePilotBridge.lua"
HEARTBEAT_STALE_SECONDS = 8.0
# A request may legitimately run this long (run_lua's maximum timeout plus slack);
# past it, a "busy" heartbeat means the bridge died mid-request.
MAX_BUSY_SECONDS = 900.0
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


class BridgeBusy(BridgeError):
    """The bridge is alive but still running an earlier request (or another session's)."""


@dataclass
class BridgeStatus:
    running: bool
    directory: str
    heartbeat_age_seconds: float | None = None
    bridge_version: str | None = None
    detail: str = ""
    busy: bool = False
    busy_seconds: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BridgeResult:
    ok: bool
    result: Any
    error: str | None
    output: str
    elapsed_seconds: float
    output_truncated: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        if not self.output_truncated:
            d.pop("output_truncated")
        return d


def bridge_dir() -> Path:
    """Folder shared with the bridge. ``RESOLVE_PILOT_BRIDGE_DIR`` overrides it."""
    env = os.environ.get("RESOLVE_PILOT_BRIDGE_DIR")
    if env:
        p = Path(env).expanduser()
    else:
        p = home() / "bridge"
        if platform.system() == "Windows" and not str(p).isascii():
            # LuaJIT inside Resolve opens files through the ANSI code page, so a
            # profile path like C:\Users\José would be unreachable from the bridge.
            base = os.environ.get("PUBLIC") or os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
            p = Path(base) / "resolve-pilot" / "bridge"
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


def _read_heartbeat(beat: Path) -> tuple[list[str], float] | None:
    # The bridge replaces the file by rename; on Windows there is a brief gap
    # between removing the old file and renaming the new one, so look twice.
    for attempt in range(2):
        try:
            return beat.read_text(encoding="utf-8", errors="replace").split(), beat.stat().st_mtime
        except FileNotFoundError:
            if attempt == 0:
                time.sleep(0.05)
    return None


def status() -> BridgeStatus:
    d = bridge_dir()
    beat = _read_heartbeat(d / "heartbeat")
    if beat is None:
        return BridgeStatus(False, str(d), detail="Bridge is not running. " + START_HINT)
    fields, mtime = beat
    try:
        epoch = float(fields[0])
    except (IndexError, ValueError):
        epoch = mtime
    now = time.time()
    age = round(max(0.0, now - epoch), 1)
    version = fields[1] if len(fields) > 1 else None
    if len(fields) >= 5 and fields[3] == "busy":
        try:
            busy_for = round(max(0.0, now - float(fields[4])), 1)
        except ValueError:
            busy_for = age
        if busy_for <= MAX_BUSY_SECONDS:
            return BridgeStatus(
                True, str(d), age, version,
                f"Bridge is busy: Resolve has been running a request for {busy_for:.0f}s. "
                "Wait for it to finish before sending more work.",
                busy=True, busy_seconds=busy_for,
            )
        return BridgeStatus(
            False, str(d), age, version,
            f"The bridge has been stuck on one request for {busy_for:.0f}s; it probably died "
            "with Resolve. " + START_HINT,
        )
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


def _write_atomic(path: Path, text: str, tag: str) -> None:
    tmp = path.with_name(f"{path.name}.{tag}.host-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def _host_lock(d: Path, timeout: float) -> Iterator[None]:
    """Serialize requests across processes (e.g. Claude Desktop + Claude Code).

    There is one request/response slot, so two MCP servers talking to the same
    bridge must take turns or they overwrite each other's requests.
    """
    with open(d / "host.lock", "a+b") as f:
        if os.name == "nt":
            import msvcrt

            def try_lock() -> None:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

            def unlock() -> None:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def try_lock() -> None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock() -> None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout
        while True:
            try:
                try_lock()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BridgeBusy(
                        "Another resolve-pilot session is using the bridge right now; retry shortly."
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            unlock()


def exec_lua(code: str, timeout: float = 30.0) -> BridgeResult:
    """Run ``code`` inside Resolve and return what it printed and returned.

    Raises BridgeNotRunning when no bridge is listening, BridgeBusy when it is
    still working on an earlier request, and BridgeError when it picked the
    request up but did not answer in time.
    """
    st = status()
    if not st.running:
        raise BridgeNotRunning(st.detail)
    if st.busy:
        raise BridgeBusy(
            f"Resolve is still running a previous request (started {st.busy_seconds:.0f}s ago). "
            "Wait, then retry."
        )
    d = Path(st.directory)
    req, resp = d / "request.lua", d / "response.json"
    rid = uuid.uuid4().hex[:12]
    start = time.monotonic()
    with _lock, _host_lock(d, timeout):
        _remove(resp)  # safe under the lock: only abandoned requests leave one behind
        _write_atomic(req, f"-- rp-id: {rid}\n{code}", rid)
        while time.monotonic() - start < timeout:
            try:
                raw = resp.read_bytes()
            except (FileNotFoundError, PermissionError):
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
                        result=payload.get("result"),
                        error=payload.get("error"),
                        output=payload.get("output") or "",
                        elapsed_seconds=round(time.monotonic() - start, 3),
                        output_truncated=bool(payload.get("output_truncated")),
                    )
                if payload is not None:
                    _remove(resp)  # an answer to an abandoned request
            time.sleep(0.05)
        picked_up = not req.exists()
        if not picked_up:
            _remove(req)
    if picked_up:
        raise BridgeError(
            f"Resolve took the request but did not answer within {timeout:g}s. It is probably "
            "still running (long imports or renders); the bridge reports busy until it is done. "
            "Wait, then retry with a longer timeout or check get_resolve_state."
        )
    raise BridgeNotRunning(f"The bridge did not pick up the request within {timeout:g}s. " + START_HINT)


def stop() -> bool:
    """Ask a running bridge to exit. Returns whether one was running."""
    st = status()
    if st.running:
        (Path(st.directory) / "stop").write_text("stop", encoding="utf-8")
    return st.running
