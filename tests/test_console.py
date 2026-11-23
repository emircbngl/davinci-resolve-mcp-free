"""The Console channel: the real bridge.lua in Console mode under LuaJIT, behind a stand-in
for Resolve's Console window (what was measured on 21.2.0 Free: an input, an output that
keeps its last 131,072 characters, Lua without io, ffi or debug)."""
from __future__ import annotations

import re
import subprocess

import pytest

from resolve_pilot import bridge, control, mcp_server, resolve_app
from resolve_pilot.bridge import console
from resolve_pilot.lua_snippets import library as lua
from resolve_pilot.lua_snippets.library import lua_string
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
    def type(self, text: str) -> str:
        if not self.comes_forward:
            return "NOTFRONT"
        self.front = RESOLVE_BUNDLE_ID
        if not self.window:
            return "NOCONSOLE"
        if not self.takes_focus:
            return "NOFOCUS"
        self.typed.append(text)
        self.show(self.pending)
        self.pending = "Lua> " + text + "\n" + (self.run(text) if self.answers else "")
        if self.wanders:
            self.front = OTHER_APP
        return "SENT"

    def read(self) -> str | None:
        if self.front != RESOLVE_BUNDLE_ID or not self.window:
            return None
        seen = self.output          # the first look after Return still shows the old text
        self.show(self.pending)
        self.pending = ""
        return seen

    def activate(self, bundle: str) -> None:
        self.activated.append(bundle)
        if bundle == RESOLVE_BUNDLE_ID:
            self.wanders = False
        self.front = bundle


def _a_mac_with_resolve(monkeypatch, version="21.2.0", studio=False, probe="OK"):
    monkeypatch.delenv("RESOLVE_PILOT_CONSOLE", raising=False)
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
    assert con.front == OTHER_APP and con.activated == [OTHER_APP]


def test_resolve_stays_in_front_when_it_was(con):
    con.front = RESOLVE_BUNDLE_ID
    console.exec_lua("return 1")
    assert con.front == RESOLVE_BUNDLE_ID and con.activated == []


def test_the_front_app_is_given_back_after_a_failure_too(con):
    con.answers = False
    with pytest.raises(console.ConsoleError):
        console.exec_lua("return 1", timeout=0.2)
    assert con.front == OTHER_APP


def test_when_the_user_leaves_resolve_while_it_answers_it_is_brought_back(con):
    console.exec_lua("return 1")
    con.activated.clear()
    con.wanders = True
    assert console.exec_lua("return 7").result == 7
    assert con.activated == [RESOLVE_BUNDLE_ID, OTHER_APP]


@pytest.mark.parametrize("state, says", [
    ("comes_forward", "did not come to the front"),
    ("window", "Console window could not be opened"),
    ("takes_focus", "Return was not pressed"),
])
def test_nothing_is_typed_blind(con, state, says):
    setattr(con, state, False)
    with pytest.raises(console.ConsoleError, match=says):
        console.exec_lua("return 1")
    assert con.typed == []


def test_return_is_pressed_only_with_the_focus_in_the_console():
    script = console._TYPE
    guard = script.index("if not ((focused of input) and frontmost) then")
    assert script.count("key code 36") == 1 and guard < script.index("key code 36")
    assert script.index("if not frontmost then return") < script.index("set value of input")
    assert "keystroke" not in script and "clipboard" not in script


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

    def refused(text):
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
    assert any("brings Resolve to the front" in step for step in st["next_steps"])
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
