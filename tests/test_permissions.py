"""Finding the app macOS checks privacy permissions for."""
from __future__ import annotations

from resolve_pilot.ui_automation import permissions

PY = "/Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python"


def _tree(monkeypatch, rows):
    table = {pid: (ppid, cmd) for pid, ppid, cmd in rows}
    monkeypatch.setattr(permissions, "_parent_and_command", lambda pid: table.get(pid))


def test_claude_desktop_disclaimer_makes_python_responsible(monkeypatch):
    # Measured: Claude Desktop -> disclaimer -> Python (the MCP server).
    _tree(monkeypatch, [(10, 9, PY), (9, 8, "/Applications/Claude.app/Contents/Helpers/disclaimer"),
                        (8, 1, "/Applications/Claude.app/Contents/MacOS/Claude")])
    assert permissions.responsible_app(10) == PY.split("/Contents/")[0]


def test_without_disclaimer_the_outer_app_is_responsible(monkeypatch):
    _tree(monkeypatch, [(10, 9, PY), (9, 8, "/bin/zsh"),
                        (8, 1, "/Applications/iTerm.app/Contents/MacOS/iTerm2")])
    assert permissions.responsible_app(10) == "/Applications/iTerm.app"


def test_hint_names_the_app_and_explains_python(monkeypatch):
    monkeypatch.setattr(permissions, "responsible_app", lambda: PY.split("/Contents/")[0])
    hint = permissions.permission_hint("Screen Recording")
    assert "Screen Recording" in hint and "Python.app" in hint and "interpreter" in hint


def test_unknown_tree_gives_generic_hint(monkeypatch):
    monkeypatch.setattr(permissions, "responsible_app", lambda: None)
    assert "enable the app that runs this MCP server" in permissions.permission_hint("Accessibility")
