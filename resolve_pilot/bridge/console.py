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
- the Console is a panel that hides while Resolve is in the background, and System Events
  no longer lists it then. Accessibility itself still gives it out (as Resolve's focused
  window, or through an element found earlier), takes text for its input and reads its
  output, and a Return sent to Resolve's process runs the line. So a request is made with
  Resolve where it is (``_Quiet``), in about 0.04 s;
- only when the Console cannot be reached that way (closed, a dialog in front of it) is
  Resolve brought forward and the Console driven through System Events, as every request
  was before; the app that was in front gets its place back (``_Front``).
"""
from __future__ import annotations

import atexit
import json
import os
import platform
import re
import subprocess
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
from resolve_pilot.ui_automation import applescript, ax, keys
from resolve_pilot.ui_automation.applescript import RESOLVE_BUNDLE_ID, UIAutomationError
from resolve_pilot.ui_automation.permissions import permission_hint

OFF_VALUES = ("0", "off", "false", "no")
FRONT_VALUE = "front"          # RESOLVE_PILOT_CONSOLE=front: always bring Resolve forward
HOLD_ENV = "RESOLVE_PILOT_CONSOLE_HOLD"
HOLD_DEFAULT_SECONDS = 4.0
_HOLD_MAX_SECONDS = 60.0
_STATUS_RECHECK_SECONDS = 10.0
_lock = threading.Lock()
_Timer = threading.Timer


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
# Two things move under this script (both seen on 21.2.0 Free):
# - System Events knows windows by their position in the stack, so a number found a moment
#   ago can mean another window by now: raising the Console renumbers them, and so does any
#   window that comes or goes ("Can't get text area 1 of window 1").
# - Bringing Resolve forward does not always hold: right after another app was given the
#   front (the previous request handing it back), that app's activation can land after
#   ours, and Resolve's windows are gone again.
# So one attempt is: bring Resolve forward, wait until it stays in front, find the Console
# (again after raising it), type. An attempt that loses its footing is made again from
# the start. Nothing has been typed at that point: Return is the last thing an attempt does.
# Return itself is pressed from here, for Resolve's process alone (``keys.press_return``):
# the script hands back READY and the process id. A key press of System Events goes to
# whichever app is in front at that instant, so it is only the second choice.
_TYPE = _FIND + '''
on run
	set state to "NOTFRONT"
	repeat 4 times
		try
			set state to my attempt(__TEXT__)
		on error msg number n
			-- no Accessibility permission will not get better by trying again: say so
			if msg contains "assistive access" or n is -25211 then error msg number n
			set state to "UNSTEADY"
		end try
		if state is "SENT" or state starts with "READY" then exit repeat
		delay 0.2
	end repeat
	return state
end run

on attempt(theText)
	-- Asked again every few looks: an activation of another app that was still on its
	-- way takes the front back once, not every time.
	set steady to 0
	repeat 5 times
		tell application "DaVinci Resolve" to activate
		tell application "System Events" to tell process "Resolve"
			repeat 8 times
				if frontmost and (count of windows) > 0 then
					set steady to steady + 1
					if steady is 3 then exit repeat
				else
					set steady to 0
				end if
				delay 0.05
			end repeat
		end tell
		if steady is 3 then exit repeat
	end repeat
	tell application "System Events" to tell process "Resolve"
		if steady < 3 then return "NOTFRONT"
		if (my consoleWindow()) is 0 then
			click menu item "Console" of menu 1 of menu bar item "Workspace" of menu bar 1
			repeat 30 times
				delay 0.1
				if (my consoleWindow()) is not 0 then exit repeat
			end repeat
		end if
		set w to my consoleWindow()
		if w is 0 then return "NOCONSOLE"
		-- After Resolve changed page or opened a timeline the Console is no longer the
		-- window that takes keys, and its input refuses the focus until it is raised.
		perform action "AXRaise" of window w
		set w to my consoleWindow()
		if w is 0 then return "NOCONSOLE"
		set input to item 1 of my consoleAreas(w)
		set value of input to theText
		set focused of input to true
		if not ((focused of input) and frontmost) then
			set value of input to ""
			return "NOFOCUS"
		end if
		if __OWN_KEY__ then
			key code 36
			return "SENT"
		end if
		return "READY " & (unix id as text)
	end tell
end attempt
'''

_READ = _FIND + '''
on run
	tell application "System Events" to tell process "Resolve"
		if not frontmost then return "NOTFRONT"
		set w to my consoleWindow()
		if w is 0 then return "NOCONSOLE"
		set areas to my consoleAreas(w)
		-- HELD: the line of this request is still in the input, so it has not been run.
		set mark to "OK"
		if __RID__ is not "" and ((value of item 1 of areas) as text) contains __RID__ then set mark to "HELD"
		return mark & linefeed & ((value of item 2 of areas) as text)
	end tell
end run
'''

_WIPE = _FIND + '''
on run
	tell application "System Events" to tell process "Resolve"
		if not frontmost then return "NOTFRONT"
		set w to my consoleWindow()
		if w is 0 then return "NOCONSOLE"
		set input to item 1 of my consoleAreas(w)
		if ((value of input) as text) contains __RID__ then set value of input to ""
		return "OK"
	end tell
end run
'''

_NO_PERMISSION = ("assistive access", "-25211")


def _no_permission(error: str) -> bool:
    return any(mark in error for mark in _NO_PERMISSION)


def _type(text: str, own_key: bool = False) -> str:
    """Put `text` into the Console's input and press Return: SENT, or why not
    (NOTFRONT, NOCONSOLE, NOFOCUS). Return is pressed for Resolve's process alone;
    `own_key` has System Events press it instead (it goes to the app in front)."""
    def script(press: bool) -> str:
        return (_TYPE.replace("__TEXT__", applescript._as_quoted(text))
                .replace("__OWN_KEY__", "true" if press else "false"))
    state = applescript.osascript(script(own_key), timeout=30.0, on_stdin=True)
    if not state.startswith("READY"):
        return state
    pid = state[5:].strip()
    if pid.isdigit() and keys.press_return(int(pid)):
        return "SENT"
    return applescript.osascript(script(True), timeout=30.0, on_stdin=True)


@dataclass
class _Look:
    output: str
    held: bool = False   # the request's line is still in the input: it has not been run


def _read(rid: str = "") -> _Look | None:
    """What the Console shows, or None while it cannot be read (Resolve busy or not in
    front, the window gone)."""
    try:
        out = applescript.osascript(_READ.replace("__RID__", applescript._as_quoted(rid)), timeout=20.0)
    except UIAutomationError as e:
        if _no_permission(str(e)):
            raise
        return None
    mark, _, output = out.partition("\n")
    if mark not in ("OK", "HELD"):
        return None
    return _Look(output, mark == "HELD")


def _wipe(rid: str) -> None:
    """Take the request's line out of the input again: a line left there runs when the
    user next presses Return in the Console."""
    if _quiet.wipe(rid):
        return
    try:
        applescript.osascript(_WIPE.replace("__RID__", applescript._as_quoted(rid)), timeout=10.0)
    except UIAutomationError:
        pass   # Resolve is busy or gone; there is nothing more to do from here


def _frontmost() -> str | None:
    return applescript.frontmost_bundle_id()


def _activate(bundle_id: str, settle_seconds: float = 1.5) -> None:
    """Bring an app to the front and wait until it is there. Returning earlier lets the
    next request bring Resolve forward while this activation is still on its way, and the
    two then take the front from each other."""
    try:
        applescript.osascript(f"tell application id {applescript._as_quoted(bundle_id)} to activate")
    except UIAutomationError:
        return   # the app quit meanwhile: nothing to give the front back to
    deadline = time.monotonic() + settle_seconds
    while _frontmost() != bundle_id and time.monotonic() < deadline:
        time.sleep(0.05)


def hold_seconds() -> float:
    """How long Resolve stays in front after a request before the app that was there gets
    its place back: RESOLVE_PILOT_CONSOLE_HOLD in seconds, 0 to hand it back at once."""
    raw = os.environ.get(HOLD_ENV, "").strip()
    try:
        value = float(raw) if raw else HOLD_DEFAULT_SECONDS
    except ValueError:
        return HOLD_DEFAULT_SECONDS
    if value != value:   # nan
        return HOLD_DEFAULT_SECONDS
    return min(max(value, 0.0), _HOLD_MAX_SECONDS)


class _Front:
    """The app that was in front before Resolve had to come forward, and when it gets its
    place back.

    Most requests leave Resolve where it is (``_Quiet``). Where one cannot, bringing Resolve
    forward and handing the front back is about half of what the request costs (measured on
    21.2.0 Free: 1.5 s back to back, 0.8 s with Resolve already in front), and the user sees
    two app switches per request. So while the next request would need Resolve in front
    again, Resolve stays there for a few seconds: a request that comes in that time starts
    at once and takes the debt over, and the app is brought back once, after the last one.
    When the next request can be made in the background, the front is given back at once."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._owed: str | None = None
        self._timer: threading.Timer | None = None
        self._back: str | None = None     # of the request that is running; under _lock
        self._forward = False

    def take(self) -> str | None:
        """A request starts: the app still owed the front, if any. Its hand-back is off."""
        with self._guard:
            owed, self._owed = self._owed, None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            return owed

    def begin(self) -> None:
        self._back, self._forward = self.take(), False

    def forward(self) -> None:
        """Resolve is about to be brought forward: whose place does it take?"""
        self._forward = True
        now = _frontmost()
        if now and now != RESOLVE_BUNDLE_ID:
            self._back = now   # also when another app was owed: the user is here now

    def end(self) -> None:
        """The request is over: give the front back, now or after the hold."""
        back, self._back = self._back, None
        if not back:
            return
        if not _quiet.ready():
            hold = hold_seconds()
            if hold > 0:
                self.owe(back, hold)
                return
        # Given back at once. An app owed from an earlier request only gets the front if
        # Resolve still has it: otherwise the user went somewhere else, and stays there.
        if self._forward or _frontmost() == RESOLVE_BUNDLE_ID:
            _activate(back)

    def owe(self, app: str, hold: float) -> None:
        """`app` gets the front back in `hold` seconds unless another request comes first."""
        with self._guard:
            self._owed = app
            self._timer = _Timer(hold, self._due)
            self._timer.daemon = True
            self._timer.start()

    def _due(self) -> None:
        if not _lock.acquire(blocking=False):
            return   # a request of this session has started: it takes the debt over
        try:
            app = self.take()
            if not app:
                return
            try:
                with file_lock(bridge_dir() / "console.lock", 0.0):
                    # In another app by now: the user went there, and stays there.
                    if _frontmost() == RESOLVE_BUNDLE_ID:
                        _activate(app)
            except LockTimeout:
                # Another resolve-pilot session is typing: Resolve has to stay in front for
                # it. That session found Resolve in front and owes nobody, so look again.
                self.owe(app, max(hold_seconds(), 1.0))
            except (UIAutomationError, OSError):
                pass   # nothing to hand the front back with; Resolve stays where it is
        finally:
            _lock.release()

    def settle(self) -> None:
        """The server is going away: hand the front back now."""
        app = self.take()
        try:
            if app and _frontmost() == RESOLVE_BUNDLE_ID:
                _activate(app, settle_seconds=0.0)
        except (UIAutomationError, OSError):
            pass


def _resolve_pid() -> int | None:
    """Resolve's process id, or None when it is not running."""
    try:
        out = subprocess.run(["lsappinfo", "info", "-only", "pid", "-app", RESOLVE_BUNDLE_ID],
                             capture_output=True, text=True, timeout=2.0, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = re.search(r'"pid"\s*=\s*(\d+)', out)
    return int(found.group(1)) if found else None


_NOT_HERE = object()   # the Console cannot be reached in the background: use the other way
_BUSY = "BUSY"
_QUIET_TIMEOUT = 1.5    # seconds a look waits for a Resolve that is running code
_FOCUS_SECONDS = 0.5
_LOST_RETURNS = 2       # so many Returns in a row that did not arrive: stop sending them so


class _Quiet:
    """Resolve's Console with Resolve left where it is.

    The Console is found as Resolve's focused window, or in its window list while Resolve
    is in front, and its input and output are kept: they answer for as long as the window
    exists, also while it is hidden. A request sets the input's text, makes sure the input
    has Resolve's keyboard focus (raising the Console inside Resolve when it has not: that
    shows nothing while Resolve is in the background) and sends Return to Resolve's process.
    Nothing here changes which app is in front, and no key goes to another app."""

    def __init__(self) -> None:
        self.pid: int | None = None
        self.found: tuple | None = None     # (window, input, output)
        self.keys = True                    # Return for one process has been arriving
        self._lost = 0
        self._timeout_set = False

    def enabled(self) -> bool:
        return (os.environ.get("RESOLVE_PILOT_CONSOLE", "").strip().lower() != FRONT_VALUE
                and self.keys and ax.available())

    def ready(self) -> bool:
        """The next request can be made without Resolve in front."""
        return self.found is not None and self.enabled()

    def forget(self) -> None:
        self.pid, self.found = None, None

    def _console(self):
        """(window, input, output), looked up again when what was kept no longer answers.
        AXError (busy) while Resolve is running and does not answer."""
        if self.found:
            try:
                if self.found[1].get("AXRole") == "AXTextArea":
                    return self.found
            except ax.AXError as e:
                if e.busy and _resolve_pid() == self.pid:
                    raise
            self.forget()
        pid = _resolve_pid()
        if not pid:
            return None
        if not self._timeout_set:
            ax.set_timeout(_QUIET_TIMEOUT)
            self._timeout_set = True
        app = ax.application(pid)
        windows = []
        for attribute in ("AXFocusedWindow", "AXWindows"):
            got = app.get(attribute)
            windows += got if isinstance(got, list) else [got]
        for window in windows:
            if not isinstance(window, ax.Element):
                continue
            areas = [k for k in window.get("AXChildren") or []
                     if isinstance(k, ax.Element) and k.get("AXRole") == "AXTextArea"]
            if len(areas) == 2:
                areas.sort(key=lambda area: (area.get("AXSize") or (0, 0))[1])   # the input is the lower
                self.pid, self.found = pid, (window, areas[0], areas[1])
                return self.found
        return None

    def type(self, text: str):
        """Put `text` into the input and press Return for Resolve alone. True when sent,
        False when it cannot be done this way (nothing is left typed), _BUSY while Resolve
        does not answer."""
        if not self.enabled():
            return False
        entry = None
        try:
            found = self._console()
            if not found:
                return False
            window, entry, _ = found
            entry.set("AXValue", text)
            if entry.get("AXValue") != text:
                raise ax.AXError(0, "the input did not take the text")
            if entry.get("AXFocused") is not True:
                window.perform("AXRaise")
                entry.set("AXFocused", True)
                deadline = time.monotonic() + _FOCUS_SECONDS
                while entry.get("AXFocused") is not True:
                    if time.monotonic() >= deadline:   # a dialog has the keyboard
                        raise ax.AXError(0, "the input did not take the focus")
                    time.sleep(0.02)
            if not keys.press_return(self.pid):
                self.keys = False
                raise ax.AXError(0, "Return could not be sent")
            return True
        except ax.AXError as e:
            if e.busy:
                return _BUSY
            try:
                if entry is not None:
                    entry.set("AXValue", "")
            except ax.AXError:
                pass
            self.forget()
            return False

    def read(self, rid: str = ""):
        """What the Console shows (_Look); None while Resolve does not answer; _NOT_HERE
        when the Console cannot be reached this way."""
        if not self.enabled():
            return _NOT_HERE
        try:
            found = self._console()
            if not found:
                return _NOT_HERE
            _, entry, shown = found
            output = shown.get("AXValue") or ""
            return _Look(output, bool(rid) and rid in (entry.get("AXValue") or ""))
        except ax.AXError as e:
            if e.busy:
                return None
            self.forget()
            return _NOT_HERE

    def wipe(self, rid: str) -> bool:
        """Take the request's line out of the input; False when that has to be done the
        other way."""
        try:
            if not (self.enabled() and self.found):
                return False
            entry = self.found[1]
            if rid in (entry.get("AXValue") or ""):
                entry.set("AXValue", "")
            return True
        except ax.AXError:
            return False

    def arrived(self, did: bool) -> None:
        """Whether the Return this sent ran the line. Twice in a row not: keys for one
        process do not reach Resolve here, and the requests go the other way from now on."""
        self._lost = 0 if did else self._lost + 1
        if self._lost >= _LOST_RETURNS:
            self.keys = False


_quiet = _Quiet()
_front = _Front()


def _settle_at_exit() -> None:
    _front.settle()


atexit.register(_settle_at_exit)


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
    return ConsoleStatus(True, automatic, "Resolve's Console window can be used: a request types "
                         "into Workspace > Console and reads the answer, with Resolve left in "
                         "the background. Resolve comes to the front only when its Console "
                         "cannot be reached otherwise (closed, or a dialog is open).")


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
    "NOTFRONT": "DaVinci Resolve did not stay in front, so nothing was typed. This channel needs "
                "Resolve in front for about a second: if the user is working in another app, ask "
                "them to pause, then try again. (Or Resolve is hidden behind a full-screen app.)",
    "NOCONSOLE": "Resolve's Console window could not be opened (Workspace > Console). Open a "
                 "project in Resolve first; with Resolve in another language than English, open "
                 "the Console by hand once.",
    "NOFOCUS": "The Console's input did not take the keyboard focus, so Return was not pressed "
               "and nothing ran. " + MODAL_HINT,
    "UNSTEADY": "Resolve's windows kept changing while the Console was looked up, so nothing was "
                "typed. Is another app taking the front, or Resolve opening or closing windows? "
                "Try again.",
    "NORETURN": "Return did not reach Resolve's Console: the line stayed in its input, so nothing "
                "ran, and it was taken out again. If the user is typing or clicking, ask them to "
                "pause, then try again. " + MODAL_HINT,
}
# A line still in the input this long after Return, in this many looks in a row, was not
# run: the Console empties its input before it runs a line (measured on 21.2.0 Free).
_HELD_LOOKS = 3
_HELD_SECONDS = 0.6


_BUSY_SECONDS = 10.0


def _press(text: str, own_key: bool = False) -> bool:
    """Type `text` and press Return; True when it was done with Resolve left where it is."""
    deadline = time.monotonic() + _BUSY_SECONDS
    while not own_key:
        sent = _quiet.type(text)
        if sent is True:
            return True
        if sent is not _BUSY:
            break
        if time.monotonic() >= deadline:
            raise ConsoleError(
                f"DaVinci Resolve did not react for {_BUSY_SECONDS:.0f} seconds, so nothing was "
                "typed. It is busy: a long import or render, or the request before this one "
                "is still running. Wait, then check get_resolve_state.")
        time.sleep(0.2)
    _front.forward()
    try:
        state = _type(text, own_key)
    except UIAutomationError as e:
        if _no_permission(str(e)):
            raise ConsoleUnavailable("This server may not use other apps' windows. "
                                     + permission_hint("Accessibility")) from None
        if "timed out" in str(e):
            raise ConsoleError(_TYPE_ERRORS["NOTFRONT"] + " (Resolve did not react for 30 seconds.)") from None
        raise ConsoleError(f"Resolve's Console could not be typed into: {e}") from None
    if state != "SENT":
        raise ConsoleError(_TYPE_ERRORS.get(state, f"Resolve's Console could not be typed into ({state})."))
    return False


def _send(rid: str, text: str, timeout: float, want_part: int = 1, ignore: str = "") -> _Reply:
    quietly = _press(text)
    pressed = time.monotonic()
    deadline = pressed + timeout
    seen = ""
    held = 0
    again = False
    while True:
        look = _quiet.read(rid)
        if look is _NOT_HERE:
            look = _read(rid)
            # Not readable that way either: the code is still running, or Resolve is not
            # in front (the user went to another app).
            if look is None and _frontmost() != RESOLVE_BUNDLE_ID:
                _front.forward()
                _activate(RESOLVE_BUNDLE_ID)
        if look is None:
            held = 0
        else:
            seen = look.output
            reply = _parse(seen, rid)
            if reply and reply.kind != ignore and (reply.kind != "part" or reply.n == want_part):
                if quietly:
                    _quiet.arrived(True)
                return reply
            held = held + 1 if look.held else 0
            if held >= _HELD_LOOKS and time.monotonic() - pressed >= _HELD_SECONDS:
                # Return did not arrive (seen on 21.2.0 Free with the user at the keyboard).
                # Nothing ran, so the line can be typed again; the second time Resolve comes
                # forward and System Events presses Return, in case keys for one process do
                # not arrive here.
                if quietly:
                    _quiet.arrived(False)
                if again:
                    _wipe(rid)
                    raise ConsoleError(_TYPE_ERRORS["NORETURN"])
                again, held, quietly = True, 0, False
                _press(text, own_key=True)
                pressed = time.monotonic()
        if time.monotonic() >= deadline:
            tail = " | ".join(line for line in seen.strip().splitlines()[-3:])[-400:]
            raise ConsoleError(
                "Resolve's Console did not answer in time. The code may still be running (long "
                "imports or renders): wait, then check get_resolve_state. " + MODAL_HINT
                + (f" Last lines in the Console: {tail}" if tail else ""))
        time.sleep(0.01 if quietly else 0.05)


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
    _front.begin()   # takes over what the request before this one still owes
    try:
        try:
            # One typist at a time, also across resolve-pilot sessions on this machine.
            with file_lock(bridge_dir() / "console.lock", timeout):
                rid = uuid.uuid4().hex[:12]
                text = _ask(rid, code, timeout)
        except LockTimeout:
            raise BridgeBusy(
                "Another resolve-pilot session is typing into the Console right now; retry shortly."
            ) from None
    finally:
        try:
            _front.end()
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
