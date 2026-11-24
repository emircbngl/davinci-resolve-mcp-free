"""The Console channel: the real bridge.lua in Console mode under LuaJIT, behind a stand-in
for Resolve's Console window (what was measured on 21.2.0 Free: an input, an output that
keeps its last 131,072 characters, Lua without io, ffi or debug)."""
from __future__ import annotations

import re
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from resolve_pilot import bridge, control, mcp_server, resolve_app
from resolve_pilot.bridge import console
from resolve_pilot.lua_snippets import library as lua
from resolve_pilot.lua_snippets.library import lua_string
from resolve_pilot.ui_automation import ax, keys
from resolve_pilot.ui_automation.applescript import RESOLVE_BUNDLE_ID, UIAutomationError
from tests.conftest import LUAJIT, MOCK_RESOLVE

OTHER_APP = "com.example.editor"
NO_PERMISSION = ("osascript failed (exit 1): System Events got an error: osascript is not "
                 "allowed assistive access. (-1719)")

_PROMPT = """\
dofile(%s)
local stdin, stdout, loadstring, pcall, tostring, select = io.stdin, io.stdout, loadstring, pcall, tostring, select
io, debug, package, require, dofile, loadfile, ffi = nil, nil, nil, nil, nil, nil, nil
os.execute, os.remove, os.rename = nil, nil, nil
function print(...)
  local row = {}
  for i = 1, select("#", ...) do row[i] = tostring((select(i, ...))) end
  stdout:write(table.concat(row, "\\t"), "\\n")
end
while true do
  local n = stdin:read("*l")
  if not n then break end
  local fn, err = loadstring(stdin:read(tonumber(n)), "=console")
  if not fn then print(err) else
    local ok, e = pcall(fn)
    if not ok then print(e) end
  end
  stdout:write("\\0\\n")
  stdout:flush()
end
"""


class FakeConsole:
    LIMIT = 131072

    def __init__(self, tmp_path):
        runner = tmp_path / "console_prompt.lua"
        runner.write_text(_PROMPT % lua_string(str(MOCK_RESOLVE)), encoding="utf-8")
        self.lua = subprocess.Popen([LUAJIT, str(runner)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.output = ""
        self.front = OTHER_APP
        self.comes_forward = True
        self.window = True
        self.takes_focus = True
        self.answers = True
        self.wanders = False        # the user goes to another app while we wait for the answer
        self.typed: list[str] = []
        self.input = ""             # a line that was typed but not run is still here
        self.swallows = 0           # so many Returns do not reach the Console
        self.late = 0               # Return arrives, but only after so many looks
        self.own_keys: list[bool] = []
        self.wiped = 0
        self.background = False     # the Console can be reached with Resolve left where it is
        self.busy = 0               # so many times Resolve does not react
        self.quiet_typed = 0
        self.activated: list[str] = []
        self.longest = 0
        self.pending = ""           # printed, but not on screen before the next look

    def run(self, code: str) -> str:
        data = code.encode("utf-8")
        self.lua.stdin.write(f"{len(data)}\n".encode() + data)
        self.lua.stdin.flush()
        out = b""
        while not out.endswith(b"\0\n"):
            line = self.lua.stdout.readline()
            assert line, "the Lua prompt died"
            out += line
        return out[:-2].decode("utf-8")

    def show(self, text: str) -> None:
        for line in text.splitlines(keepends=True):
            self.longest = max(self.longest, len(line))
            self.output = "" if len(line) > self.LIMIT else (self.output + line)[-self.LIMIT:]

    # stand-ins for console._type / _read / _frontmost / _activate
    def type(self, text: str, own_key: bool = False) -> str:
        if not self.comes_forward:
            return "NOTFRONT"
        self.front = RESOLVE_BUNDLE_ID
        if not self.window:
            return "NOCONSOLE"
        if not self.takes_focus:
            return "NOFOCUS"
        self.own_keys.append(own_key)
        self.put(text)
        if self.wanders:
            self.front = OTHER_APP
        return "SENT"

    def put(self, text: str) -> None:
        self.typed.append(text)
        self.input = text
        if self.swallows:
            self.swallows -= 1
        elif not self.late:
            self.enter()

    def enter(self) -> None:
        text, self.input = self.input, ""
        self.show(self.pending)
        self.pending = "Lua> " + text + "\n" + (self.run(text) if self.answers else "")

    def read(self, rid: str = ""):
        if self.front != RESOLVE_BUNDLE_ID or not self.window:
            return None
        return self.look(rid)

    def look(self, rid: str = ""):
        if self.late and self.input:
            self.late -= 1
            if not self.late:
                self.enter()
        seen = self.output          # the first look after Return still shows the old text
        self.show(self.pending)
        self.pending = ""
        return console._Look(seen, bool(rid) and rid in self.input)

    def wipe(self, rid: str) -> None:
        if rid in self.input:
            self.input = ""
            self.wiped += 1

    def activate(self, bundle: str, settle_seconds: float = 1.5) -> None:
        self.activated.append(bundle)
        if bundle == RESOLVE_BUNDLE_ID:
            self.wanders = False
        self.front = bundle


class QuietSide:
    """Stand-in for console._quiet over the same Console: reached without Resolve in front
    while `background` is set, not at all otherwise."""

    def __init__(self, fake: FakeConsole):
        self.fake = fake
        self.found = False          # the Console was found, so the next request can be quiet
        self.arrivals: list[bool] = []

    def ready(self) -> bool:
        return self.fake.background or self.found

    def type(self, text: str):
        if not self.fake.background:
            return False
        if self.fake.busy:
            self.fake.busy -= 1
            return console._BUSY
        self.fake.quiet_typed += 1
        self.fake.put(text)
        return True

    def read(self, rid: str = ""):
        return self.fake.look(rid) if self.fake.background else console._NOT_HERE

    def wipe(self, rid: str) -> bool:
        if not self.fake.background:
            return False
        self.fake.wipe(rid)
        return True

    def arrived(self, did: bool) -> None:
        self.arrivals.append(did)


class FakeTimer:
    """threading.Timer that only goes off when the test says the time has passed."""
    made: list["FakeTimer"] = []

    def __init__(self, seconds, due):
        self.seconds, self.due, self.state = seconds, due, "new"
        FakeTimer.made.append(self)

    def start(self):
        self.state = "waiting"

    def cancel(self):
        self.state = "cancelled"

    @classmethod
    def time_passes(cls):
        for timer in [t for t in cls.made if t.state == "waiting"]:
            timer.state = "done"
            timer.due()

    @classmethod
    def waiting(cls):
        return [t.seconds for t in cls.made if t.state == "waiting"]


def _a_mac_with_resolve(monkeypatch, version="21.2.0", studio=False, probe="OK"):
    monkeypatch.delenv("RESOLVE_PILOT_CONSOLE", raising=False)
    monkeypatch.delenv(console.HOLD_ENV, raising=False)
    monkeypatch.setattr(FakeTimer, "made", [])
    monkeypatch.setattr(console, "_Timer", FakeTimer)
    monkeypatch.setattr(console, "_front", console._Front())
    monkeypatch.setattr(console, "_status_cache", None)
    monkeypatch.setattr(console.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(resolve_app, "installed", lambda: (version, studio))

    def answer():
        if isinstance(probe, Exception):
            raise probe
        return probe
    monkeypatch.setattr(console, "_probe", answer)


@pytest.fixture
def con(tmp_path, monkeypatch):
    if not LUAJIT:
        pytest.skip("luajit not installed")
    _a_mac_with_resolve(monkeypatch)
    fake = FakeConsole(tmp_path)
    monkeypatch.setattr(console, "_type", fake.type)
    monkeypatch.setattr(console, "_read", fake.read)
    monkeypatch.setattr(console, "_wipe", lambda rid: console._quiet.wipe(rid) or fake.wipe(rid))
    fake.quiet = QuietSide(fake)
    monkeypatch.setattr(console, "_quiet", fake.quiet)
    monkeypatch.setattr(console, "_frontmost", lambda: fake.front)
    monkeypatch.setattr(console, "_activate", fake.activate)
    yield fake
    fake.lua.kill()
    fake.lua.wait()


# ── the exchange ─────────────────────────────────────────────────────────────

def test_values_print_output_and_other_alphabets_come_back(con):
    r = console.exec_lua('print("hi", 2, {a = 1})\n'
                         'return {n = 1.5, list = {1, 2, 3}, s = "çğış İ 日本 😀 <b>&amp;  x", t = true, empty = {}}')
    assert r.ok, r.error
    assert r.output == 'hi\t2\t{"a":1}'
    assert r.result == {"n": 1.5, "list": [1, 2, 3], "s": "çğış İ 日本 😀 <b>&amp;  x", "t": True, "empty": []}
    # Only plain ASCII goes through the window, both ways.
    assert all(text.isascii() for text in con.typed) and con.output.isascii()


def test_bytes_that_are_not_text_do_not_break_the_answer(con):
    assert console.exec_lua('return "a\\255b" .. string.char(195)').result == "a�b�"


def test_the_helper_is_typed_once_per_resolve_session(con):
    assert console.exec_lua("return 1").result == 1
    assert len(con.typed) == 2 and "__rp_console = {" in con.typed[1]      # asked, then taught
    assert console.exec_lua("return 2").result == 2
    assert len(con.typed) == 3 and len(con.typed[2]) < 300


def test_a_helper_from_another_server_version_is_replaced(con):
    console.exec_lua("return 1")
    con.run('__rp_console.version = "0.0.0"')
    assert console.exec_lua("return rp.version").result == bridge.client.__version__
    assert sum("__rp_console = {" in text for text in con.typed) == 2


def test_a_long_answer_comes_back_whole_in_parts(con):
    r = console.exec_lua("local t = {} for i = 1, 3000 do t[i] = {i = i, name = 'klip ğ ' .. i, "
                         "pad = string.rep('x', 40)} end return t")
    assert r.ok and len(r.result) == 3000
    assert r.result[0] == {"i": 1, "name": "klip ğ 1", "pad": "x" * 40}
    assert r.result[-1] == {"i": 3000, "name": "klip ğ 3000", "pad": "x" * 40}
    assert sum(".part(" in text for text in con.typed) >= 3
    assert 50000 < con.longest < con.LIMIT        # no line the window would have dropped


def test_lua_errors_are_reported_and_the_console_keeps_working(con):
    r = console.exec_lua("return 1 +")
    assert not r.ok and r.error.startswith("Lua syntax error")
    r = console.exec_lua('print("before") error("boom")')
    assert not r.ok and "boom" in r.error and r.output == "before"
    assert console.exec_lua("return 42").result == 42


def test_requests_leave_no_globals_but_their_own_behind(con):
    console.exec_lua("leftover = 5")
    assert console.exec_lua("return leftover").result == 5        # as with the bridge
    assert con.run("print(leftover, type(io), type(__rp_console))") == "nil\tnil\ttable\n"


def test_tools_run_through_the_console(con):
    out = control.perform(lua.snippet_get_state(include_clips=True), None)
    assert out["channel"] == "console" and out["project"] == "Test Project"
    assert mcp_server.run_lua(code="return 2 + 2")["result"] == 4
    assert mcp_server.run_lua(code="return 2 + 2")["channel"] == "console"
    with pytest.raises(bridge.BridgeError, match="Lua error inside Resolve"):
        control.perform("error('nope')", None)


def test_parts_are_only_given_for_the_last_request(con):
    console.exec_lua("return 1")
    rid = re.search(r'run\("(\w+)"', con.typed[-1]).group(1)
    assert con.run(f'__rp_console.part("{rid}", 1)').startswith(f"@@rp:{rid}:1/1:")
    assert con.run(f'__rp_console.part("{rid}", 2)') == f"@@rp:{rid}:gone@@\n"
    assert con.run('__rp_console.part("another", 1)') == "@@rp:another:gone@@\n"


# ── what the window may do to an answer ──────────────────────────────────────

def _printed(rid, n, parts, text, end="@@"):
    return f"Lua> something\n@@rp:{rid}:{n}/{parts}:{len(text)}:{text}{end}\n"


def test_only_a_complete_answer_to_this_request_is_taken():
    reply = console._parse(_printed("abc", 1, 2, '{"id":"abc"'), "abc")
    assert (reply.kind, reply.n, reply.parts, reply.text) == ("part", 1, 2, '{"id":"abc"')
    assert console._parse(_printed("other", 1, 1, "{}"), "abc") is None
    assert console._parse(_printed("abc", 1, 1, "{}", end=""), "abc") is None          # still printing
    assert console._parse("Lua> x\n@@rp:abc:1/1:9:{}@@\n", "abc") is None              # shorter than it says
    assert console._parse("Lua> x\n@@rp:abc:1/1:1:{}@@\n", "abc") is None              # longer than it says
    # The code the Console echoes mentions the marker, but never at the start of a line.
    assert console._parse('Lua> print("@@rp:abc:1/1:2:{}@@")\n', "abc") is None
    later = _printed("abc", 1, 2, "one") + _printed("abc", 2, 2, "two")
    assert console._parse(later, "abc").text == "two"
    assert console._parse("@@rp:abc:need@@\n", "abc").kind == "need"
    assert console._parse("@@rp:abc:gone@@\n", "abc").kind == "gone"


def test_an_answer_the_window_cut_short_is_not_believed(con, monkeypatch):
    whole = con.run
    monkeypatch.setattr(con, "run", lambda code: whole(code).replace('"result":', '"result" :'))
    with pytest.raises(console.ConsoleError, match="did not answer in time"):
        console.exec_lua("return 1", timeout=0.3)


def test_no_answer_is_an_error_with_what_the_console_shows(con):
    console.exec_lua("return 1")
    con.answers = False
    with pytest.raises(console.ConsoleError, match="did not answer in time.*Last lines in the Console"):
        console.exec_lua("return 2", timeout=0.3)


# ── the front app and the keyboard ───────────────────────────────────────────

def test_the_app_that_was_in_front_gets_its_place_back(con):
    console.exec_lua("return 1")
    # Not at once: the next request may be on its way.
    assert con.front == RESOLVE_BUNDLE_ID and FakeTimer.waiting() == [console.HOLD_DEFAULT_SECONDS]
    FakeTimer.time_passes()
    assert con.front == OTHER_APP and con.activated == [OTHER_APP]


def test_a_run_of_requests_gives_the_front_back_once_after_the_last(con):
    for n in range(5):
        assert console.exec_lua(f"return {n}").result == n
        assert con.front == RESOLVE_BUNDLE_ID and con.activated == []
        assert len(FakeTimer.waiting()) == 1          # the earlier hand-backs are off
    FakeTimer.time_passes()
    assert con.front == OTHER_APP and con.activated == [OTHER_APP]
    FakeTimer.time_passes()
    assert con.activated == [OTHER_APP]              # and it is not given back twice


def test_without_a_hold_the_front_is_given_back_at_once(con, monkeypatch):
    monkeypatch.setenv(console.HOLD_ENV, "0")
    console.exec_lua("return 1")
    assert con.front == OTHER_APP and con.activated == [OTHER_APP] and FakeTimer.made == []


@pytest.mark.parametrize("raw, seconds", [
    ("", console.HOLD_DEFAULT_SECONDS), ("1.5", 1.5), ("0", 0.0), ("-3", 0.0), ("900", 60.0),
    ("soon", console.HOLD_DEFAULT_SECONDS), ("nan", console.HOLD_DEFAULT_SECONDS),
])
def test_the_hold_is_read_from_the_environment(monkeypatch, raw, seconds):
    monkeypatch.setenv(console.HOLD_ENV, raw)
    assert console.hold_seconds() == seconds


def test_a_user_who_went_elsewhere_meanwhile_is_left_there(con):
    console.exec_lua("return 1")
    con.front = "com.example.mail"                    # the user clicked into a third app
    FakeTimer.time_passes()
    assert con.front == "com.example.mail" and con.activated == []


def test_the_app_the_user_is_in_now_is_the_one_that_comes_back(con):
    console.exec_lua("return 1")                      # the editor is owed the front
    con.front = "com.example.mail"                    # the user moved on, then the next request
    console.exec_lua("return 2")
    FakeTimer.time_passes()
    assert con.front == "com.example.mail" and con.activated == ["com.example.mail"]


def test_the_front_is_not_given_back_under_a_request_that_is_running(con):
    console.exec_lua("return 1")
    assert console._lock.acquire(timeout=1)           # a request of this session is running
    try:
        FakeTimer.time_passes()
        assert con.front == RESOLVE_BUNDLE_ID and con.activated == []
    finally:
        console._lock.release()
    console.exec_lua("return 2")                      # that request took the debt over
    FakeTimer.time_passes()
    assert con.front == OTHER_APP and con.activated == [OTHER_APP]


def test_the_front_is_not_given_back_while_another_session_types(con, isolated_home):
    from resolve_pilot._filelock import file_lock
    console.exec_lua("return 1")
    with file_lock(bridge.bridge_dir() / "console.lock", 1.0):
        FakeTimer.time_passes()
        assert con.front == RESOLVE_BUNDLE_ID and con.activated == []
        assert len(FakeTimer.waiting()) == 1          # it looks again later
    FakeTimer.time_passes()
    assert con.front == OTHER_APP and con.activated == [OTHER_APP]


def test_a_server_that_goes_away_gives_the_front_back_first(con):
    console.exec_lua("return 1")
    console._front.settle()
    assert con.front == OTHER_APP and FakeTimer.waiting() == []


def test_giving_the_front_back_is_registered_for_the_servers_exit():
    # In a process of its own: importing the module again here would swap its classes
    # under the other tests.
    code = ("import atexit; seen = []; atexit.register = seen.append; "
            "from resolve_pilot.bridge import console; "
            "print(seen == [console._settle_at_exit])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         cwd=str(Path(__file__).resolve().parents[1]))
    assert out.stdout.strip() == "True", out.stderr


def test_the_front_app_is_given_back_after_a_failure_too(con):
    con.answers = False
    with pytest.raises(console.ConsoleError):
        console.exec_lua("return 1", timeout=0.2)
    FakeTimer.time_passes()
    assert con.front == OTHER_APP


def test_when_the_user_leaves_resolve_while_it_answers_it_is_brought_back(con):
    console.exec_lua("return 1")
    con.activated.clear()
    con.wanders = True
    assert console.exec_lua("return 7").result == 7
    FakeTimer.time_passes()
    assert con.activated == [RESOLVE_BUNDLE_ID, OTHER_APP]


@pytest.mark.parametrize("state, says", [
    ("comes_forward", "did not stay in front"),
    ("window", "Console window could not be opened"),
    ("takes_focus", "Return was not pressed"),
])
def test_nothing_is_typed_blind(con, state, says):
    setattr(con, state, False)
    with pytest.raises(console.ConsoleError, match=says):
        console.exec_lua("return 1")
    assert con.typed == []


COUNT = "__rp_count = (__rp_count or 0) + 1 return __rp_count"


# ── with Resolve left in the background ──────────────────────────────────────

def test_a_request_leaves_resolve_and_the_users_app_where_they_are(con, monkeypatch):
    con.background = True
    monkeypatch.setattr(console, "_frontmost", lambda: pytest.fail("looked which app is in front"))
    monkeypatch.setattr(console, "_type", lambda *a, **k: pytest.fail("brought Resolve forward"))
    r = console.exec_lua('print("çay") return {n = 41 + 1}')
    assert r.ok and r.result == {"n": 42} and r.output == "çay"
    assert console.exec_lua("return ('x'):rep(150000)").result == "x" * 150000      # in parts, too
    assert con.front == OTHER_APP and con.activated == [] and FakeTimer.made == []
    assert con.quiet_typed >= 5 and con.quiet.arrivals and all(con.quiet.arrivals)


def test_a_busy_resolve_is_waited_for_in_the_background(con, monkeypatch):
    con.background = True
    monkeypatch.setattr(console.time, "sleep", lambda seconds: None)
    con.busy = 3
    assert console.exec_lua("return 5").result == 5 and con.activated == []
    con.busy = 10 ** 9
    monkeypatch.setattr(console, "_BUSY_SECONDS", 0.2)
    typed = len(con.typed)
    with pytest.raises(console.ConsoleError, match="did not react for .* seconds, so nothing was typed"):
        console.exec_lua("return 6")
    assert len(con.typed) == typed and con.front == OTHER_APP and con.activated == []


def test_a_return_lost_in_the_background_is_pressed_again_with_resolve_in_front(con):
    con.background = True
    console.exec_lua("return 1")
    con.typed.clear(), con.quiet.arrivals.clear()
    con.swallows = 1
    assert console.exec_lua(COUNT, timeout=20).result == 1
    assert len(con.typed) == 2 and con.own_keys == [True] and con.quiet.arrivals == [False]
    # Resolve had to come forward for that; the next request will not need it there.
    assert con.front == OTHER_APP and con.activated == [OTHER_APP] and FakeTimer.made == []


def test_once_the_console_is_found_the_front_is_given_back_at_once(con):
    con.quiet.found = True            # found while Resolve was in front for this request
    console.exec_lua("return 1")
    assert con.front == OTHER_APP and con.activated == [OTHER_APP] and FakeTimer.made == []


def test_an_app_still_owed_the_front_gets_it_after_a_quiet_request(con):
    console.exec_lua("return 1")                      # in front, and held there
    assert con.front == RESOLVE_BUNDLE_ID and len(FakeTimer.waiting()) == 1
    con.background = True
    console.exec_lua("return 2")
    assert con.front == OTHER_APP and con.activated == [OTHER_APP] and FakeTimer.waiting() == []


def test_an_app_owed_the_front_is_not_put_over_where_the_user_went(con):
    console.exec_lua("return 1")
    con.background = True
    con.front = "com.example.mail"
    console.exec_lua("return 2")
    assert con.front == "com.example.mail" and con.activated == [] and FakeTimer.waiting() == []


def test_tests_cannot_reach_a_real_console(monkeypatch):
    # With the Console switched on for a test, the way into a real Resolve stays shut.
    _a_mac_with_resolve(monkeypatch)
    assert not console._quiet.enabled() and not console._Quiet().enabled()
    assert console._quiet.type("return 1") is False and console._quiet.read("x") is console._NOT_HERE


# ── console._Quiet itself, over a stand-in for what Accessibility hands out ──

class El:
    def __init__(self, role, **attributes):
        self.attributes = {"AXRole": role, **attributes}
        self.fails = None           # an Accessibility error code every call answers with
        self.performed: list[str] = []
        self.refuses: set[str] = set()
        self.on_perform = None

    def get(self, name):
        if self.fails:
            raise ax.AXError(self.fails, name)
        return self.attributes.get(name)

    def set(self, name, value):
        if self.fails:
            raise ax.AXError(self.fails, name)
        if name not in self.refuses:
            self.attributes[name] = value

    def perform(self, action):
        if self.fails:
            raise ax.AXError(self.fails, action)
        self.performed.append(action)
        if self.on_perform:
            self.on_perform()


@pytest.fixture
def resolve_ui(monkeypatch):
    """Resolve as Accessibility shows it from the background: a main window in the window
    list, the Console as the focused window with its input holding the keyboard."""
    ui = SimpleNamespace(pid=4242, keys=[], looked_up=0, key_arrives=True, can_press=True)
    ui.entry = El("AXTextArea", AXSize=(636.0, 19.0), AXValue="", AXFocused=True)
    ui.shown = El("AXTextArea", AXSize=(636.0, 432.0), AXValue="")
    ui.window = El("AXWindow", AXSubrole="AXFloatingWindow",
                   AXChildren=[ui.shown, ui.entry, El("AXButton"), El("AXStaticText")])
    ui.main = El("AXWindow", AXTitle="project", AXChildren=[El("AXSplitGroup")])
    ui.app = El("AXApplication", AXFocusedWindow=ui.window, AXWindows=[ui.main])

    def application(pid):
        ui.looked_up += 1
        assert pid == ui.pid
        return ui.app

    def press_return(pid):
        if not ui.can_press:
            return False
        ui.keys.append((pid, ui.entry.attributes["AXValue"]))
        if ui.key_arrives and ui.entry.attributes["AXFocused"] is True:
            ui.shown.attributes["AXValue"] += "Lua> " + ui.entry.attributes["AXValue"] + "\n"
            ui.entry.attributes["AXValue"] = ""
        return True

    monkeypatch.delenv("RESOLVE_PILOT_CONSOLE", raising=False)
    monkeypatch.setattr(console, "ax", SimpleNamespace(
        available=lambda: True, set_timeout=lambda seconds: None, application=application,
        Element=El, AXError=ax.AXError))
    monkeypatch.setattr(console, "_resolve_pid", lambda: ui.pid)
    monkeypatch.setattr(console.keys, "press_return", press_return)
    monkeypatch.setattr(console, "_FOCUS_SECONDS", 0.05)
    ui.quiet = console._Quiet()
    return ui


def test_the_console_is_typed_into_as_resolves_focused_window(resolve_ui):
    ui = resolve_ui
    assert ui.quiet.type("return 1") is True
    assert ui.keys == [(4242, "return 1")]            # the text was there before the key
    assert ui.window.performed == []                  # the input had the focus: nothing moved
    assert ui.quiet.read("zzz") == console._Look("Lua> return 1\n", False)
    assert ui.quiet.ready()


def test_the_consoles_parts_are_kept_and_not_looked_up_again(resolve_ui):
    ui = resolve_ui
    ui.app.attributes.update(AXFocusedWindow=ui.main, AXWindows=[ui.main, ui.window])   # in front
    assert ui.quiet.type("return 1") is True
    ui.app.attributes.update(AXFocusedWindow=ui.main, AXWindows=[])                    # behind, hidden
    assert ui.quiet.type("return 2") is True and ui.quiet.read().output.endswith("Lua> return 2\n")
    assert ui.looked_up == 1 and [text for _, text in ui.keys] == ["return 1", "return 2"]


def test_an_input_without_the_focus_gets_it_by_raising_the_console_inside_resolve(resolve_ui):
    ui = resolve_ui
    ui.entry.attributes["AXFocused"] = False
    ui.entry.refuses.add("AXFocused")                 # only a raised Console takes the keyboard
    ui.window.on_perform = lambda: ui.entry.refuses.discard("AXFocused")
    assert ui.quiet.type("return 1") is True
    assert ui.window.performed == ["AXRaise"] and ui.keys == [(4242, "return 1")]


def test_nothing_is_typed_quietly_when_the_input_does_not_take_the_focus(resolve_ui):
    ui = resolve_ui                                   # a dialog in Resolve has the keyboard
    ui.entry.attributes["AXFocused"] = False
    ui.entry.refuses.add("AXFocused")
    assert ui.quiet.type("return 1") is False
    assert ui.keys == [] and ui.entry.attributes["AXValue"] == "" and not ui.quiet.ready()


def test_nothing_is_typed_quietly_into_an_input_that_changed_the_text(resolve_ui):
    ui = resolve_ui
    real = ui.entry.set
    ui.entry.set = lambda name, value: real(name, value.replace("\n", " ") if isinstance(value, str) else value)
    assert ui.quiet.type("a\nb") is False
    assert ui.keys == [] and ui.entry.attributes["AXValue"] == ""


def test_a_console_that_cannot_be_found_is_left_to_the_other_way(resolve_ui):
    ui = resolve_ui
    ui.app.attributes.update(AXFocusedWindow=ui.main, AXWindows=[ui.main])
    assert ui.quiet.type("return 1") is False and ui.quiet.read("x") is console._NOT_HERE
    assert ui.keys == [] and not ui.quiet.ready() and ui.quiet.wipe("x") is False
    ui.app.attributes["AXFocusedWindow"] = None       # no window at all
    assert ui.quiet.type("return 1") is False


def test_a_console_that_was_closed_is_looked_up_again(resolve_ui):
    ui = resolve_ui
    assert ui.quiet.type("return 1") is True
    old_entry = ui.entry
    old_entry.fails = ax.INVALID                      # that window is gone
    ui.entry = El("AXTextArea", AXSize=(636.0, 19.0), AXValue="", AXFocused=True)
    ui.shown = El("AXTextArea", AXSize=(636.0, 432.0), AXValue="")
    ui.app.attributes["AXFocusedWindow"] = El("AXWindow", AXChildren=[ui.entry, ui.shown])
    assert ui.quiet.type("return 2") is True
    assert ui.keys[-1] == (4242, "return 2") and ui.looked_up == 2


def test_a_resolve_that_does_not_answer_is_busy_not_gone(resolve_ui):
    ui = resolve_ui
    assert ui.quiet.type("return 1") is True
    ui.entry.fails = ui.shown.fails = ax.CANNOT_COMPLETE
    assert ui.quiet.type("return 2") is console._BUSY and ui.quiet.read("x") is None
    assert ui.quiet.ready() and len(ui.keys) == 1     # kept: it will answer again
    ui.entry.fails = ui.shown.fails = None
    assert ui.quiet.type("return 3") is True and ui.looked_up == 1


def test_a_resolve_that_was_started_again_is_looked_up_again(resolve_ui, monkeypatch):
    ui = resolve_ui
    assert ui.quiet.type("return 1") is True
    ui.entry.fails = ax.CANNOT_COMPLETE               # the old process answers nothing any more
    ui.pid = 5151
    ui.entry = El("AXTextArea", AXSize=(636.0, 19.0), AXValue="", AXFocused=True)
    ui.app = El("AXApplication", AXWindows=[], AXFocusedWindow=El(
        "AXWindow", AXChildren=[ui.entry, El("AXTextArea", AXSize=(636.0, 432.0), AXValue="")]))
    assert ui.quiet.type("return 2") is True and ui.keys[-1] == (5151, "return 2")


def test_where_a_key_for_one_process_cannot_be_sent_nothing_stays_typed(resolve_ui):
    ui = resolve_ui
    ui.can_press = False
    assert ui.quiet.type("return 1") is False
    assert ui.entry.attributes["AXValue"] == "" and ui.quiet.keys is False
    ui.can_press = True
    assert ui.quiet.type("return 1") is False and ui.keys == []     # not tried again and again
    assert ui.quiet.read("x") is console._NOT_HERE and not ui.quiet.ready()


def test_returns_that_do_not_arrive_end_the_quiet_way(resolve_ui):
    ui = resolve_ui
    ui.quiet.arrived(False), ui.quiet.arrived(True), ui.quiet.arrived(False)
    assert ui.quiet.type("return 1") is True          # one lost Return is not the rule
    ui.quiet.arrived(False), ui.quiet.arrived(False)
    assert ui.quiet.type("return 1") is False and ui.quiet.keys is False


def test_the_line_of_a_request_is_seen_and_taken_out_of_the_input(resolve_ui):
    ui = resolve_ui
    ui.key_arrives = False
    assert ui.quiet.type('run("abc123")') is True
    assert ui.quiet.read("abc123") == console._Look("", True)
    assert ui.quiet.wipe("zzz") is True and ui.entry.attributes["AXValue"] == 'run("abc123")'
    assert ui.quiet.wipe("abc123") is True and ui.entry.attributes["AXValue"] == ""
    assert ui.quiet.read("abc123") == console._Look("", False)


@pytest.mark.parametrize("why", ["switched off", "no accessibility"])
def test_the_quiet_way_can_be_off(resolve_ui, monkeypatch, why):
    ui = resolve_ui
    if why == "switched off":
        monkeypatch.setenv("RESOLVE_PILOT_CONSOLE", "Front")
    else:
        monkeypatch.setattr(console.ax, "available", lambda: False)
    assert ui.quiet.type("return 1") is False and ui.quiet.read("x") is console._NOT_HERE
    assert ui.quiet.wipe("x") is False and not ui.quiet.ready()
    assert ui.looked_up == 0 and ui.keys == []
    _a_mac_with_resolve(monkeypatch)
    monkeypatch.setenv("RESOLVE_PILOT_CONSOLE", "front")
    assert console.status(refresh=True).usable        # only the quiet way is off


def test_resolves_process_is_asked_of_the_system(monkeypatch):
    answers = iter(['"pid"=95956\n', "", "garbage"])
    monkeypatch.setattr(console.subprocess, "run", lambda cmd, **kw: SimpleNamespace(stdout=next(answers)))
    assert console._resolve_pid() == 95956 and console._resolve_pid() is None and console._resolve_pid() is None

    def gone(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(console.subprocess, "run", gone)
    assert console._resolve_pid() is None


def test_text_survives_the_way_to_accessibility_and_back():
    api = ax._load()
    if api is None:
        assert sys.platform != "darwin"
        assert not ax.available()
        with pytest.raises(ax.AXError):
            ax.application(4242)
        return
    for text in ["", "return 1", "çğış ünlü — 日本語 😀", "a\0b", "line\nline", "x" * 200000]:
        made = api.string(text)
        try:
            assert api.text(made) == text
        finally:
            api.release(made)
    assert isinstance(ax.available(), bool)
    assert ax.AXError(ax.CANNOT_COMPLETE).busy and not ax.AXError(ax.INVALID).busy


def test_a_return_that_did_not_arrive_is_pressed_again(con):
    console.exec_lua("return 1")                      # the helper is in place
    con.typed.clear(), con.own_keys.clear()
    con.swallows = 1
    assert console.exec_lua(COUNT, timeout=20).result == 1          # run, and run once
    assert len(con.typed) == 2 and con.typed[0] == con.typed[1]
    assert con.own_keys == [False, True]              # the second time the other way


def test_a_return_that_never_arrives_is_said_soon_and_the_line_is_taken_out(con):
    console.exec_lua("return 1")
    con.typed.clear()
    con.swallows = 99
    start = time.monotonic()
    with pytest.raises(console.ConsoleError, match="Return did not reach Resolve's Console.*nothing"):
        console.exec_lua(COUNT, timeout=30)
    assert time.monotonic() - start < 10              # not the 30 seconds of "did not answer"
    assert len(con.typed) == 2 and con.input == "" and con.wiped == 1
    con.swallows = 0
    assert console.exec_lua(COUNT).result == 1        # nothing had run


def test_a_line_is_not_typed_again_while_its_return_may_still_arrive(con, monkeypatch):
    console.exec_lua("return 1")
    con.typed.clear()
    monkeypatch.setattr(console, "_HELD_SECONDS", 0.0)
    con.late = 2                                      # still in the input for two looks
    assert console.exec_lua(COUNT).result == 1
    assert len(con.typed) == 1


def test_a_line_is_not_typed_again_before_it_had_time_to_run(con, monkeypatch):
    console.exec_lua("return 1")
    con.typed.clear()
    monkeypatch.setattr(console, "_HELD_SECONDS", 2.0)
    con.late = console._HELD_LOOKS + 3                # many quick looks, but little time
    monkeypatch.setattr(console.time, "sleep", lambda seconds: None)
    assert console.exec_lua(COUNT).result == 1
    assert len(con.typed) == 1


def test_return_goes_to_resolves_process_alone(monkeypatch):
    scripts, pressed = [], []
    def osascript(script, **kw):
        scripts.append(script)
        return "READY 4242" if "if false then\n\t\t\tkey code 36" in script else "SENT"
    monkeypatch.setattr(console.applescript, "osascript", osascript)
    monkeypatch.setattr(console.keys, "press_return", lambda pid: pressed.append(pid) or True)
    assert console._type("return 1") == "SENT"
    assert pressed == [4242] and len(scripts) == 1    # System Events pressed nothing

    # Where a key cannot be sent to one process, System Events presses it after all.
    monkeypatch.setattr(console.keys, "press_return", lambda pid: False)
    assert console._type("return 1") == "SENT"
    assert len(scripts) == 3 and "if true then\n\t\t\tkey code 36" in scripts[2]

    # Why nothing was typed comes through unchanged.
    monkeypatch.setattr(console.applescript, "osascript", lambda script, **kw: "NOFOCUS")
    monkeypatch.setattr(console.keys, "press_return", lambda pid: pytest.fail("pressed blind"))
    assert console._type("return 1") == "NOFOCUS"


def test_the_key_for_one_process_is_a_plain_return(monkeypatch):
    class Lib:
        def __init__(self):
            self.calls = []

        def CGEventCreateKeyboardEvent(self, source, key, down):
            self.calls.append(("make", key, down))
            return 7 + down

        def CGEventSetFlags(self, event, flags):
            self.calls.append(("flags", event, flags))

        def CGEventPostToPid(self, pid, event):
            self.calls.append(("post", pid, event))

        def CFRelease(self, event):
            self.calls.append(("free", event))
    lib = Lib()
    assert keys.press_return(4242, lib) is True
    assert lib.calls == [("make", 36, True), ("flags", 8, 0), ("post", 4242, 8), ("free", 8),
                         ("make", 36, False), ("flags", 7, 0), ("post", 4242, 7), ("free", 7)]
    assert keys.press_return(0, lib) is False
    monkeypatch.setattr(keys.platform, "system", lambda: "Linux")
    assert keys.press_return(4242) is False           # nothing to send it with: say so


def test_what_the_console_shows_is_read_with_whether_the_line_is_still_typed(monkeypatch):
    asked = []
    answers = iter(["OK\nLua> x\n1", "HELD\nLua> x", "NOTFRONT", "OK"])
    monkeypatch.setattr(console.applescript, "osascript", lambda script, **kw: asked.append(script) or next(answers))
    assert console._read("abc123") == console._Look("Lua> x\n1", False)
    assert '"abc123"' in asked[0]
    assert console._read("abc123") == console._Look("Lua> x", True)
    assert console._read("abc123") is None
    assert console._read() == console._Look("", False)
    assert 'contains "abc123" then set value of input to ""' in console._WIPE.replace("__RID__", '"abc123"')


def test_return_is_pressed_only_with_the_focus_in_the_console():
    script = console._TYPE
    guard = script.index("if not ((focused of input) and frontmost) then")
    assert script.count("key code 36") == 1 and guard < script.index("key code 36")
    assert script.index("if steady < 3 then return") < script.index("set value of input")
    assert "keystroke" not in script and "clipboard" not in script
    # Raising the Console renumbers Resolve's windows: it is looked up again before use.
    raised = script.index('perform action "AXRaise"')
    assert raised < script.index("set w to my consoleWindow()", raised) < script.index("set input to")
    assert script.rindex("key code 36") > script.rindex("set focused of input to true")
    # The script only presses Return itself when asked to; otherwise it says READY and whose.
    own = script.index("if __OWN_KEY__ then")
    assert guard < own < script.index("key code 36") < script.index('return "READY " & (unix id as text)')
    assert script.index('state starts with "READY" then exit repeat') < script.index("on attempt(")
    # An attempt that loses its footing starts over, bringing Resolve forward again, and
    # Resolve has to stay in front before anything is looked up.
    assert script.index("repeat 4 times") < script.index("set state to my attempt(") < script.index("on attempt(")
    start = script.index("on attempt(")
    assert start < script.index('tell application "DaVinci Resolve" to activate') < script.index("if steady is 3")
    assert script.index("if steady < 3 then return") < script.index("my consoleWindow()", start)


def test_giving_the_front_back_waits_until_the_app_is_there(monkeypatch):
    asked, looks = [], iter([RESOLVE_BUNDLE_ID, RESOLVE_BUNDLE_ID, OTHER_APP])
    monkeypatch.setattr(console.applescript, "osascript", lambda script, **kw: asked.append(script) or "")
    monkeypatch.setattr(console, "_frontmost", lambda: next(looks))
    console._activate(OTHER_APP)
    assert OTHER_APP in asked[0] and next(looks, None) is None        # looked until it was in front
    # An app that never comes forward does not hold the request up for long.
    monkeypatch.setattr(console, "_frontmost", lambda: RESOLVE_BUNDLE_ID)
    console._activate(OTHER_APP, settle_seconds=0.2)


def test_a_resolve_that_does_not_react_is_said_in_plain_words(monkeypatch):
    _a_mac_with_resolve(monkeypatch)

    def hangs(text, own_key=False):
        raise UIAutomationError("osascript timed out after 30.0s")
    monkeypatch.setattr(console, "_type", hangs)
    monkeypatch.setattr(console, "_frontmost", lambda: OTHER_APP)
    monkeypatch.setattr(console, "_activate", lambda bundle: None)
    with pytest.raises(console.ConsoleError, match="did not stay in front.*did not react for 30 seconds"):
        console.exec_lua("return 1")


def test_an_app_that_quit_is_not_waited_for(monkeypatch):
    def gone(script, **kw):
        raise UIAutomationError("no such app")
    monkeypatch.setattr(console.applescript, "osascript", gone)
    monkeypatch.setattr(console, "_frontmost", lambda: pytest.fail("looked for an app that is gone"))
    console._activate(OTHER_APP)


# ── whether the channel is there ─────────────────────────────────────────────

def test_auto_uses_the_console_only_where_the_bridge_cannot_run(monkeypatch):
    _a_mac_with_resolve(monkeypatch, "21.2.0")
    assert control.choose_channel("auto") == "console"
    _a_mac_with_resolve(monkeypatch, "21.1.2")
    assert control.choose_channel("auto") == "console"
    for version, studio in (("20.3.0", False), ("21.1.1", False), ("21.2.0", True), (None, False)):
        _a_mac_with_resolve(monkeypatch, version, studio)
        assert control.choose_channel("auto") == "manual", version
        assert control.choose_channel("console") == "console"      # but it can be asked for


def test_a_running_bridge_comes_first(monkeypatch):
    _a_mac_with_resolve(monkeypatch)
    monkeypatch.setattr(bridge, "status", lambda: bridge.BridgeStatus(True, "x"))
    assert control.choose_channel("auto") == "bridge"
    assert control.choose_channel("console") == "console"


def test_without_permission_the_user_is_told_which_app_to_allow(monkeypatch):
    _a_mac_with_resolve(monkeypatch, probe=UIAutomationError(NO_PERMISSION))
    st = console.status()
    assert not st.usable and st.automatic and "Privacy & Security > Accessibility" in st.detail
    assert control.choose_channel("auto") == "manual"
    with pytest.raises(control.ChannelUnavailable, match="Accessibility"):
        control.choose_channel("console")
    with pytest.raises(console.ConsoleUnavailable, match="Accessibility"):
        console.exec_lua("return 1")
    out = control.perform("print(1)", None)
    assert out["channel"] == "manual" and "Accessibility" in out["console"]


def test_permission_taken_away_meanwhile_is_said_as_such(monkeypatch):
    _a_mac_with_resolve(monkeypatch)

    def refused(text, own_key=False):
        raise UIAutomationError(NO_PERMISSION)
    monkeypatch.setattr(console, "_type", refused)
    monkeypatch.setattr(console, "_frontmost", lambda: OTHER_APP)
    monkeypatch.setattr(console, "_activate", lambda bundle: None)
    with pytest.raises(console.ConsoleUnavailable, match="Privacy & Security > Accessibility"):
        console.exec_lua("return 1")


@pytest.mark.parametrize("change, says", [
    (lambda m: m.setattr(console, "_probe", lambda: "NOTRUNNING"), "not running"),
    (lambda m: m.setattr(console.platform, "system", lambda: "Linux"), "macOS only"),
    (lambda m: m.setenv("RESOLVE_PILOT_CONSOLE", "off"), "Turned off"),
])
def test_where_the_console_cannot_be_used(monkeypatch, change, says):
    _a_mac_with_resolve(monkeypatch)
    change(monkeypatch)
    st = console.status(refresh=True)
    assert not st.usable and says in st.detail
    assert control.choose_channel("auto") == "manual"
    assert "console" not in control.perform("print(1)", None) or st.automatic


def test_status_and_the_state_resource_do_not_touch_the_console(con, monkeypatch):
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))
    monkeypatch.setattr(mcp_server, "_resolve_app", lambda: ("21.2.0", False))
    st = mcp_server.resolve_pilot_status()
    assert st["channel"] == "console" and st["console"]["usable"]
    assert st["capabilities"]["live_resolve_control"] is True
    assert any("with Resolve left in the background" in step for step in st["next_steps"])
    assert mcp_server._state_snapshot() == {
        "connected": True, "channel": "console", "next_steps": mcp_server._state_snapshot()["next_steps"]}
    assert mcp_server.setup_bridge("status")["console"]["usable"] is True
    assert con.typed == [] and con.activated == []


def test_status_says_what_is_missing_for_the_console(monkeypatch):
    _a_mac_with_resolve(monkeypatch, probe=UIAutomationError(NO_PERMISSION))
    monkeypatch.setattr(control, "studio_status", lambda refresh=False: (False, "Free edition"))
    monkeypatch.setattr(mcp_server, "_resolve_app", lambda: ("21.2.0", False))
    st = mcp_server.resolve_pilot_status()
    assert st["channel"] == "manual"
    assert any("Console channel is not usable yet" in s and "Accessibility" in s for s in st["next_steps"])
    with pytest.raises(bridge.BridgeNotRunning, match="Console cannot be used either"):
        mcp_server.run_lua(code="return 1")
