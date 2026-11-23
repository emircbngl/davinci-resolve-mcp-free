"""The bridge's requests through Resolve's own Console window (macOS).

Resolve Free 21.1.2 and later starts no script from its menu, and its Console Lua cannot
reach files, so the file bridge cannot exist there. The Console itself is still offered to
every user: type Lua at the prompt, read what it prints. This module does that for the user
through macOS Accessibility: it puts the Lua into the Console's input, presses Return and
reads the output text back. No file inside Resolve, no clipboard, no screenshot.

Measured on Resolve 21.2.0 Free:
- the Console window has two text areas, a one-line input and the output;
- globals set in one entry are still there in the next, so the helper (``bridge.lua`` in
  Console mode) is typed once per Resolve session;
- the output keeps its last 131,072 characters and loses a longer line entirely, so the
  helper prints an answer in parts of 60,000 characters;
- Resolve's windows are only visible to Accessibility while Resolve is the front app, so
  every exchange brings Resolve forward; the app that was in front gets its place back.
"""
from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
import uuid
from dataclasses import dataclass

from resolve_pilot import __version__, resolve_app
from resolve_pilot._filelock import LockTimeout, file_lock
from resolve_pilot.bridge.client import (
    MODAL_HINT,
    BridgeBusy,
    BridgeError,
    BridgeResult,
    bridge_dir,
    render_console_script,
)
from resolve_pilot.lua_snippets.library import lua_string
from resolve_pilot.ui_automation import applescript
from resolve_pilot.ui_automation.applescript import RESOLVE_BUNDLE_ID, UIAutomationError
from resolve_pilot.ui_automation.permissions import permission_hint

OFF_VALUES = ("0", "off", "false", "no")
_STATUS_RECHECK_SECONDS = 10.0
_lock = threading.Lock()


class ConsoleError(BridgeError):
    """The Console could not be driven, or did not answer."""


class ConsoleUnavailable(ConsoleError):
    """This machine cannot type into Resolve's Console right now."""


@dataclass
class ConsoleStatus:
    usable: bool
    automatic: bool   # channel="auto" may pick it: the file bridge cannot run on this Resolve
    detail: str

    def to_dict(self) -> dict:
        return {"usable": self.usable, "automatic": self.automatic, "detail": self.detail}


# ── AppleScript ──────────────────────────────────────────────────────────────

_FIND = '''
on consoleWindow()
	tell application "System Events" to tell process "Resolve"
		repeat with i from 1 to (count of windows)
			if (count of text areas of window i) is 2 then return i
		end repeat
	end tell
	return 0
end consoleWindow

on consoleAreas(w)
	-- {input, output}: the input is the lower of the two
	tell application "System Events" to tell process "Resolve"
		set a to text area 1 of window w
		set b to text area 2 of window w
		if (item 2 of (get size of a)) > (item 2 of (get size of b)) then return {b, a}
		return {a, b}
	end tell
end consoleAreas
'''

_PROBE = '''
tell application "System Events"
	if not (exists process "Resolve") then return "NOTRUNNING"
	tell process "Resolve" to count windows
	return "OK"
end tell
'''

# Return is pressed only while Resolve is the front app and the Console's input has the
# keyboard focus; otherwise it would go to whatever else is focused (a dialog's default
# button, another app).
_TYPE = _FIND + '''
on run
	tell application "DaVinci Resolve" to activate
	tell application "System Events" to tell process "Resolve"
		repeat 60 times
			if frontmost and (count of windows) > 0 then exit repeat
			delay 0.05
		end repeat
		if not frontmost then return "NOTFRONT"
		set w to my consoleWindow()
		if w is 0 then
			try
				click menu item "Console" of menu 1 of menu bar item "Workspace" of menu bar 1
			on error msg number n
				-- no Accessibility permission is not a missing window: say so
				if msg contains "assistive access" or n is -25211 then error msg number n
				return "NOCONSOLE"
			end try
			repeat 30 times
				delay 0.1
				set w to my consoleWindow()
				if w is not 0 then exit repeat
			end repeat
			if w is 0 then return "NOCONSOLE"
		end if
		set input to item 1 of my consoleAreas(w)
		set value of input to __TEXT__
		-- After Resolve changed page or opened a timeline the Console is no longer the
		-- window that takes keys, and its input refuses the focus until it is raised.
		repeat 10 times
			perform action "AXRaise" of window w
			set focused of input to true
			if focused of input then exit repeat
			delay 0.1
		end repeat
		if not ((focused of input) and frontmost) then
			set value of input to ""
			return "NOFOCUS"
		end if
		key code 36
		return "SENT"
	end tell
end run
'''

_READ = _FIND + '''
on run
	tell application "System Events" to tell process "Resolve"
		if not frontmost then return "NOTFRONT"
		set w to my consoleWindow()
		if w is 0 then return "NOCONSOLE"
		return "OK" & linefeed & ((value of item 2 of my consoleAreas(w)) as text)
	end tell
end run
'''

_NO_PERMISSION = ("assistive access", "-25211")


def _no_permission(error: str) -> bool:
    return any(mark in error for mark in _NO_PERMISSION)


def _type(text: str) -> str:
    """Put `text` into the Console's input and press Return: SENT, or why not
    (NOTFRONT, NOCONSOLE, NOFOCUS)."""
    return applescript.osascript(_TYPE.replace("__TEXT__", applescript._as_quoted(text)),
                                 timeout=30.0, on_stdin=True)


def _read() -> str | None:
    """The Console's output text, or None while it cannot be read (Resolve busy or not in
    front, the window gone)."""
    try:
        out = applescript.osascript(_READ, timeout=20.0)
    except UIAutomationError as e:
        if _no_permission(str(e)):
            raise
        return None
    if not out.startswith("OK"):
        return None
    return out[3:]


def _frontmost() -> str | None:
    return applescript.frontmost_bundle_id()


def _activate(bundle_id: str) -> None:
    try:
        applescript.osascript(f"tell application id {applescript._as_quoted(bundle_id)} to activate")
    except UIAutomationError:
        pass   # the app quit meanwhile: nothing to give the front back to


def _probe() -> str:
    return applescript.osascript(_PROBE, timeout=10.0)


# ── Status ───────────────────────────────────────────────────────────────────

_status_cache: tuple[float, ConsoleStatus] | None = None


def status(refresh: bool = False) -> ConsoleStatus:
    """Whether this server can type into Resolve's Console, checked without touching it."""
    global _status_cache
    now = time.monotonic()
    if not refresh and _status_cache and now - _status_cache[0] < _STATUS_RECHECK_SECONDS:
        return _status_cache[1]
    st = _check()
    _status_cache = (now, st)
    return st


def _check() -> ConsoleStatus:
    if os.environ.get("RESOLVE_PILOT_CONSOLE", "").strip().lower() in OFF_VALUES:
        return ConsoleStatus(False, False, "Turned off with RESOLVE_PILOT_CONSOLE.")
    if platform.system() != "Darwin":
        return ConsoleStatus(False, False, "Typing into Resolve's Console for the user is macOS only.")
    automatic = not resolve_app.file_bridge_possible(*resolve_app.installed())
    try:
        found = _probe()
    except UIAutomationError as e:
        if _no_permission(str(e)):
            return ConsoleStatus(False, automatic, "This server may not use other apps' windows yet. "
                                 + permission_hint("Accessibility"))
        return ConsoleStatus(False, automatic, f"Resolve's windows could not be read: {e}")
    if found != "OK":
        return ConsoleStatus(False, automatic, "DaVinci Resolve is not running. "
                             "setup_bridge(action='start') starts it.")
    return ConsoleStatus(True, automatic, "Resolve's Console window can be used: each request brings "
                         "Resolve to the front for a moment, types into Workspace > Console and "
                         "reads the answer.")


# ── Requests ─────────────────────────────────────────────────────────────────

def _lua_ascii(code: str) -> str:
    """`code` as a Lua string literal in plain ASCII (UTF-8 bytes as \\ddd escapes), so that
    what is typed does not depend on how any layer in between treats other characters."""
    quoted = lua_string(code)
    if quoted.isascii():
        return quoted
    return "".join(ch if ord(ch) < 128 else "".join(f"\\{b:03d}" for b in ch.encode("utf-8"))
                   for ch in quoted)


@dataclass
class _Reply:
    kind: str            # "need", "gone" or "part"
    n: int = 0
    parts: int = 0
    text: str = ""


def _parse(output: str, rid: str) -> _Reply | None:
    """The last answer to request `rid` in the Console's output, if it is complete."""
    found = None
    for found in re.finditer(rf"(?:^|\n)@@rp:{rid}:(need@@|gone@@|(\d+)/(\d+):(\d+):)", output):
        pass
    if found is None:
        return None
    if found.group(1) in ("need@@", "gone@@"):
        return _Reply(found.group(1)[:4])
    length = int(found.group(4))
    text = output[found.end():found.end() + length]
    # The length is the helper's own count: a part the window cut or changed does not pass.
    if len(text) != length or output[found.end() + length:found.end() + length + 2] != "@@":
        return None
    return _Reply("part", int(found.group(2)), int(found.group(3)), text)


_TYPE_ERRORS = {
    "NOTFRONT": "DaVinci Resolve did not come to the front, so nothing was typed. Is it running, "
                "and not hidden behind a full-screen app?",
    "NOCONSOLE": "Resolve's Console window could not be opened (Workspace > Console). Open a "
                 "project in Resolve first; with Resolve in another language than English, open "
                 "the Console by hand once.",
    "NOFOCUS": "The Console's input did not take the keyboard focus, so Return was not pressed "
               "and nothing ran. " + MODAL_HINT,
}


def _send(rid: str, text: str, timeout: float, want_part: int = 1, ignore: str = "") -> _Reply:
    try:
        state = _type(text)
    except UIAutomationError as e:
        if _no_permission(str(e)):
            raise ConsoleUnavailable("This server may not use other apps' windows. "
                                     + permission_hint("Accessibility")) from None
        raise ConsoleError(f"Resolve's Console could not be typed into: {e}") from None
    if state != "SENT":
        raise ConsoleError(_TYPE_ERRORS.get(state, f"Resolve's Console could not be typed into ({state})."))
    deadline = time.monotonic() + timeout
    seen = ""
    while True:
        output = _read()
        if output is None:
            # Not readable: the code is still running, or the user went to another app.
            if _frontmost() != RESOLVE_BUNDLE_ID:
                _activate(RESOLVE_BUNDLE_ID)
        else:
            seen = output
            reply = _parse(output, rid)
            if reply and reply.kind != ignore and (reply.kind != "part" or reply.n == want_part):
                return reply
        if time.monotonic() >= deadline:
            tail = " | ".join(line for line in seen.strip().splitlines()[-3:])[-400:]
            raise ConsoleError(
                "Resolve's Console did not answer in time. The code may still be running (long "
                "imports or renders): wait, then check get_resolve_state. " + MODAL_HINT
                + (f" Last lines in the Console: {tail}" if tail else ""))
        time.sleep(0.05)


def _ask(rid: str, code: str, timeout: float) -> str:
    call = f'__rp_console.run("{rid}", {_lua_ascii(code)})'
    reply = _send(rid, f"if __rp_console and __rp_console.version == {lua_string(__version__)} "
                       f'then {call} else print("@@" .. "rp:{rid}:need@@") end', timeout)
    if reply.kind == "need":   # first request of this Resolve session, or another server version
        reply = _send(rid, "(function()\n" + render_console_script() + "\nend)()\n" + call,
                      timeout, ignore="need")
    if reply.kind != "part":
        raise ConsoleError("Resolve's Console lost the answer to this request; try it again.")
    texts = [reply.text]
    for n in range(2, reply.parts + 1):
        more = _send(rid, f'__rp_console.part("{rid}", {n})', 30.0, want_part=n)
        if more.kind != "part" or more.parts != reply.parts:
            raise ConsoleError("Resolve's Console lost the rest of a long answer (another request "
                               "ran in between); try it again.")
        texts.append(more.text)
    return "".join(texts)


def exec_lua(code: str, timeout: float = 30.0) -> BridgeResult:
    """Run ``code`` inside Resolve through its Console window; same result as the bridge's
    ``exec_lua``. Raises ConsoleUnavailable when this machine cannot do it and ConsoleError
    when the Console could not be driven or did not answer."""
    st = status()
    if not st.usable:
        raise ConsoleUnavailable(st.detail)
    start = time.monotonic()
    if not _lock.acquire(timeout=timeout):
        raise BridgeBusy("Another request from this session is still running; retry shortly.")
    try:
        try:
            # One typist at a time, also across resolve-pilot sessions on this machine.
            with file_lock(bridge_dir() / "console.lock", timeout):
                rid = uuid.uuid4().hex[:12]
                before = _frontmost()
                try:
                    text = _ask(rid, code, timeout)
                finally:
                    if before and before != RESOLVE_BUNDLE_ID:
                        _activate(before)
        except LockTimeout:
            raise BridgeBusy(
                "Another resolve-pilot session is typing into the Console right now; retry shortly."
            ) from None
    finally:
        _lock.release()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or payload.get("id") != rid:
        raise ConsoleError("Resolve's Console answered with something that is not this request's "
                           "result; try it again.")
    return BridgeResult(
        ok=bool(payload.get("ok")),
        result=payload.get("result"),
        error=payload.get("error"),
        output=payload.get("output") or "",
        elapsed_seconds=round(time.monotonic() - start, 3),
        output_truncated=bool(payload.get("output_truncated")),
        result_truncated=bool(payload.get("result_truncated")),
    )
