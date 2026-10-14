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

from resolve_pilot import __version__, traps
from resolve_pilot._filelock import LockTimeout, file_lock
from resolve_pilot._paths import home
from resolve_pilot.lua_snippets.library import lua_string

SCRIPT_NAME = "ResolvePilotBridge.lua"
HEARTBEAT_STALE_SECONDS = 8.0
# A request may legitimately run this long (run_lua's maximum timeout plus slack).
# Past it, a busy bridge whose process is alive is probably stuck; without a
# process id to check, it probably died mid-request.
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


# A modal dialog in Resolve stops script calls, so a request that times out (or a
# heartbeat that goes stale) may simply be waiting behind one.
MODAL_HINT = (
    "A modal dialog in Resolve may be blocking the scripting API: check screenshot_resolve "
    "(macOS) or ask the user to close any open dialog in Resolve. "
    f"(known_trap: {traps.get('modal-dialog-blocks-api').id})"
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
    result_truncated: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        for flag in ("output_truncated", "result_truncated"):
            if not d[flag]:
                d.pop(flag)
        return d


def _windows_short_path(p: Path) -> Path | None:
    """The ASCII 8.3 alias of an existing folder, if the volume has one."""
    try:
        import ctypes
        from ctypes import wintypes

        fn = ctypes.windll.kernel32.GetShortPathNameW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        buf = ctypes.create_unicode_buffer(1024)
        n = fn(str(p), buf, len(buf))
        return Path(buf.value) if 0 < n < len(buf) else None
    except Exception:
        return None


def bridge_dir() -> Path:
    """Folder shared with the bridge. ``RESOLVE_PILOT_BRIDGE_DIR`` overrides it.

    On Windows the bridge's LuaJIT opens files through the ANSI code page, so a
    profile path like C:\\Users\\José would be unreachable; the folder's 8.3 short
    name (pure ASCII, still inside the user's own profile) is used instead.
    """
    env = os.environ.get("RESOLVE_PILOT_BRIDGE_DIR")
    p = Path(env).expanduser() if env else home() / "bridge"
    p.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows" and not str(p).isascii():
        short = _windows_short_path(p)
        if short is not None and str(short).isascii():
            return short
    return p


def render_script(idle_minutes: int = DEFAULT_IDLE_MINUTES, directory: Path | None = None) -> str:
    """Return the bridge Lua with this machine's folder and settings baked in."""
    d = directory or bridge_dir()
    if platform.system() == "Windows" and not str(d).isascii():
        raise BridgeError(
            f"The bridge folder {d} contains non-ASCII characters, which Lua inside Resolve "
            "cannot open on Windows, and this drive has no short (8.3) name for it. Set "
            "RESOLVE_PILOT_BRIDGE_DIR to an ASCII folder that only you can write to (e.g. one "
            "you create under your own drive), restart the MCP server and set the bridge up again."
        )
    minutes = max(1, int(idle_minutes))
    return (
        _TEMPLATE.read_text(encoding="utf-8")
        .replace("__RP_BRIDGE_DIR__", lua_string(str(d)))
        .replace("__RP_SEP__", lua_string(os.sep))
        .replace("__RP_VERSION_TEXT__", __version__)
        .replace("__RP_VERSION__", lua_string(__version__))
        .replace("__RP_IDLE_SECONDS__", str(minutes * 60))
        .replace("__RP_IDLE_MIN__", str(minutes))
    )


def scripts_menu_dir(scope: str = "user") -> Path:
    """Resolve's Workspace › Scripts folder for Utility scripts on this OS.

    RESOLVE_PILOT_SCRIPTS_DIR overrides it (the tests use that so they never write into
    the real Resolve install).
    """
    if scope not in ("user", "all"):
        raise ValueError("scope must be 'user' or 'all'")
    override = os.environ.get("RESOLVE_PILOT_SCRIPTS_DIR")
    if override:
        return Path(override).expanduser()
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
    script = render_script(idle_minutes)
    target = scripts_menu_dir(scope)
    target.mkdir(parents=True, exist_ok=True)
    path = target / SCRIPT_NAME
    path.write_text(script, encoding="utf-8")
    return path


def _pid_alive(pid: int) -> bool | None:
    """Whether a process exists; None when it cannot be told."""
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes

            k = ctypes.windll.kernel32
            handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return k.GetLastError() == 5  # access denied: it exists
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(handle, ctypes.byref(code))
            k.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


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


# Bridges (by token) whose process id this server cannot check: the id looked dead
# while the heartbeat was fresh, which happens when Resolve and this server run in
# different PID namespaces (a Flatpak or container sharing the home folder). Their
# process id is ignored from then on and the heartbeat alone counts.
_unverifiable: set[str] = set()


def _parse_heartbeat(fields: list[str]) -> tuple[str, int, int | None]:
    """(token, pid, index of "busy" or None) from a heartbeat's fields.

    Current bridges write "<epoch> <version> <token> [busy <start>] pid=<n>", so
    readers that predate the pid still find "busy" right after the token; 0.2.0
    bridges write no pid at all.
    """
    token = fields[2] if len(fields) > 2 else ""
    pid = 0
    for f in fields[3:]:
        if f.startswith("pid="):
            try:
                pid = int(f[4:])
            except ValueError:
                pid = 0
    busy_at = fields.index("busy", 3) if "busy" in fields[3:] else None
    return token, pid, busy_at


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
    token, pid, busy_at = _parse_heartbeat(fields)
    alive = _pid_alive(pid) if pid > 0 and token not in _unverifiable else None
    gone = BridgeStatus(
        False, str(d), age, version,
        "The bridge's Resolve process is gone (Resolve quit or crashed). " + START_HINT,
    )
    if busy_at is not None:
        # A busy bridge does not refresh its heartbeat, so only the process id can
        # tell a long request from a bridge that died with Resolve.
        try:
            busy_for = round(max(0.0, now - float(fields[busy_at + 1])), 1)
        except (IndexError, ValueError):
            busy_for = age
        if alive is False:
            return gone
        if busy_for <= MAX_BUSY_SECONDS:
            return BridgeStatus(
                True, str(d), age, version,
                f"Bridge is busy: Resolve has been running a request for {busy_for:.0f}s. "
                "Wait for it to finish before sending more work.",
                busy=True, busy_seconds=busy_for,
            )
        if alive:
            return BridgeStatus(
                True, str(d), age, version,
                f"Bridge is busy: Resolve has been running one request for {busy_for:.0f}s, "
                "longer than any tool call waits, so it may be stuck (an endless loop, or a "
                f"dialog in Resolve). {START_HINT} A newly started bridge takes over from the "
                "stuck one.",
                busy=True, busy_seconds=busy_for,
            )
        return BridgeStatus(
            False, str(d), age, version,
            f"The bridge has been stuck on one request for {busy_for:.0f}s; it probably died "
            "with Resolve. " + START_HINT,
        )
    if age > HEARTBEAT_STALE_SECONDS:
        if alive is False:
            return gone
        return BridgeStatus(
            False, str(d), age, version,
            f"Bridge heartbeat is {age:.0f}s old: it stopped, or Resolve is busy. {MODAL_HINT} "
            + START_HINT,
        )
    if alive is False:
        _unverifiable.add(token)  # a fresh heartbeat outranks a process id we cannot see
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


def _busy_error(st: BridgeStatus) -> BridgeBusy:
    if (st.busy_seconds or 0) > MAX_BUSY_SECONDS:
        return BridgeBusy(st.detail)
    return BridgeBusy(
        f"Resolve is still running a previous request (started {st.busy_seconds or 0:.0f}s ago). "
        "Wait, then retry."
    )


def exec_lua(code: str, timeout: float = 30.0) -> BridgeResult:
    """Run ``code`` inside Resolve and return what it printed and returned.

    A request that arrives while the bridge is working on another one (from this
    session, a parallel tool call, or another MCP client) waits up to ``timeout``
    for its turn, and once sent gets the full ``timeout`` to run, so it never starts
    with only a sliver of time left. Raises BridgeNotRunning when no bridge is
    listening, BridgeBusy when the turn did not come in time, and BridgeError when
    the bridge picked the request up but did not answer in time.
    """
    st = status()
    if not st.running:
        raise BridgeNotRunning(st.detail)
    start = time.monotonic()
    wait_deadline = start + timeout

    def wait_left() -> float:
        return max(0.0, wait_deadline - time.monotonic())

    # One request at a time, in this process and across processes sharing the folder.
    if not _lock.acquire(timeout=wait_left()):
        raise BridgeBusy("Another request from this session is still running; retry shortly.")
    try:
        d = bridge_dir()
        try:
            with file_lock(d / "host.lock", wait_left()):
                _wait_for_turn(wait_left)
                return _exchange(d, code, start, timeout)
        except LockTimeout:
            raise BridgeBusy(
                "Another resolve-pilot session is using the bridge right now; retry shortly."
            ) from None
    finally:
        _lock.release()


def _wait_for_turn(wait_left) -> None:
    # A request we waited behind (or one another client abandoned) may still be
    # running inside Resolve, or may have taken the bridge down with it.
    while True:
        st = status()
        if not st.running:
            raise BridgeNotRunning(st.detail)
        if not st.busy:
            return
        if wait_left() <= 0:
            raise _busy_error(st)
        time.sleep(0.1)


def _exchange(d: Path, code: str, start: float, timeout: float) -> BridgeResult:
    req, resp = d / "request.lua", d / "response.json"
    rid = uuid.uuid4().hex[:12]
    _remove(resp)  # safe under the lock: only abandoned requests leave one behind
    _write_atomic(req, f"-- rp-id: {rid}\n{code}", rid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
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
                    result_truncated=bool(payload.get("result_truncated")),
                )
            if payload is not None:
                _remove(resp)  # an answer to an abandoned request
        time.sleep(0.05)
    if req.exists():
        _remove(req)
        st = status()
        if st.running and st.busy:
            raise _busy_error(st)
        raise BridgeNotRunning(
            f"The bridge did not pick up the request in time. {MODAL_HINT} " + st.detail)
    raise BridgeError(
        "Resolve took the request but did not answer in time. It is probably still running "
        "(long imports or renders); the bridge reports busy until it is done. Wait, then "
        "retry with a longer timeout or check get_resolve_state. " + MODAL_HINT
    )


def stop() -> bool:
    """Ask a running bridge to exit. Returns whether one was running."""
    st = status()
    if st.running:
        (Path(st.directory) / "stop").write_text("stop", encoding="utf-8")
    return st.running
