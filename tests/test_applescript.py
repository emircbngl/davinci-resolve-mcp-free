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


def test_menu_pick_normalizes_ascii_ellipsis(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: None)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.menu_pick(["File", "Import", "Timeline..."])
    assert '"Timeline…"' in captured["s"]
    assert "..." not in captured["s"]


def test_menu_pick_walks_submenus(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: None)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.menu_pick(["Timeline", "AI Tools", "Create Subtitles from Audio…"])
    assert (
        'click menu item "Create Subtitles from Audio…" of menu 1 of '
        'menu item "AI Tools" of menu 1 of menu bar item "Timeline" of menu bar 1'
    ) in captured["s"]


def test_menu_pick_raises_on_studio_upsell(monkeypatch):
    monkeypatch.setattr(applescript, "focus_resolve", lambda: None)
    monkeypatch.setattr(applescript, "osascript", lambda *_a, **_k: "STUDIO_ONLY")
    with pytest.raises(applescript.StudioOnlyFeature):
        applescript.menu_pick(["Timeline", "Detect Scene Cuts"])


def test_trigger_returns_not_ok_on_studio_upsell(monkeypatch):
    from resolve_pilot.ui_automation import ai_features
    monkeypatch.setattr(applescript, "focus_resolve", lambda: None)
    monkeypatch.setattr(ai_features, "focus_resolve", lambda: None)
    monkeypatch.setattr(applescript, "osascript", lambda *_a, **_k: "STUDIO_ONLY")
    out = ai_features.trigger_scene_cut_detection()
    assert out["ok"] is False
    assert "Studio-only" in out["error"]
