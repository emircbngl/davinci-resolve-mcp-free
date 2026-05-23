"""Unit tests for the AppleScript helpers — focus on pure logic, not osascript calls."""
from __future__ import annotations

import pytest

from resolve_pilot.ui_automation import applescript


def test_mods_to_using_single():
    assert applescript._mods_to_using("command") == "using {command down}"


def test_mods_to_using_multi_order_preserved():
    out = applescript._mods_to_using("shift+command")
    assert out.startswith("using {")
    assert "shift down" in out
    assert "command down" in out


def test_mods_to_using_empty():
    assert applescript._mods_to_using("") == ""


def test_key_codes_table_covers_named_keys():
    # The implementation looks up named keys here — if any go missing the
    # send_keystroke fallback raises, so guard against accidental deletions.
    for k in ("return", "tab", "space", "escape", "left", "right", "up", "down", "f1"):
        assert k in applescript._KEY_CODES


def test_send_keystroke_rejects_unknown_named_key(monkeypatch):
    # Make osascript a no-op so we never hit the system; we only care about the
    # validation branch raising before that.
    monkeypatch.setattr(applescript, "focus_resolve", lambda: None)
    monkeypatch.setattr(applescript, "osascript", lambda *_a, **_k: "")
    with pytest.raises(applescript.UIAutomationError):
        applescript.send_keystroke("octarine")


def test_send_keystroke_single_char_uses_keystroke(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: None)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.send_keystroke("s", "command")
    assert 'keystroke "s"' in captured["s"]
    assert "command down" in captured["s"]


def test_send_keystroke_named_key_uses_key_code(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: None)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.send_keystroke("return")
    assert "key code 36" in captured["s"]
