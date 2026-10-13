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
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda *_a, **_k: "")
    with pytest.raises(applescript.UIAutomationError):
        applescript.send_keystroke("octarine")


def test_send_keystroke_single_char_uses_keystroke(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.send_keystroke("s", "command")
    assert 'keystroke "s"' in captured["s"]
    assert "command down" in captured["s"]


def test_send_keystroke_named_key_uses_key_code(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.send_keystroke("return")
    assert "key code 36" in captured["s"]


def test_menu_pick_normalizes_ascii_ellipsis(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.menu_pick(["File", "Import", "Timeline..."])
    assert '"Timeline…"' in captured["s"]
    assert "..." not in captured["s"]


def test_menu_pick_walks_submenus(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.menu_pick(["Timeline", "AI Tools", "Create Subtitles from Audio…"])
    assert (
        'click menu item "Create Subtitles from Audio…" of menu 1 of '
        'menu item "AI Tools" of menu 1 of menu bar item "Timeline" of menu bar 1'
    ) in captured["s"]


def test_menu_pick_raises_on_studio_upsell(monkeypatch):
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda *_a, **_k: "STUDIO_ONLY")
    with pytest.raises(applescript.StudioOnlyFeature):
        applescript.menu_pick(["Timeline", "Detect Scene Cuts"])


def test_trigger_returns_not_ok_on_studio_upsell(monkeypatch):
    from resolve_pilot.ui_automation import ai_features
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(ai_features, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda *_a, **_k: "STUDIO_ONLY")
    out = ai_features.trigger_scene_cut_detection()
    assert out["ok"] is False
    assert "Studio-only" in out["error"]


def test_mods_to_using_accepts_aliases_and_rejects_unknown_modifiers():
    assert applescript._mods_to_using("opt+ctl") == "using {option down, control down}"
    with pytest.raises(applescript.UIAutomationError, match="octarine"):
        applescript._mods_to_using("command+octarine")


def test_send_keystroke_is_scoped_to_the_resolve_process(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.send_keystroke('"', "shift")
    assert 'tell process "Resolve"' in captured["s"]
    assert "set frontmost to true" in captured["s"]
    assert 'keystroke "\\""' in captured["s"]


def test_nothing_is_typed_or_clicked_when_resolve_is_not_frontmost(monkeypatch):
    scripts = []
    monkeypatch.setattr(applescript, "focus_resolve", lambda: False)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: scripts.append(script))
    with pytest.raises(applescript.UIAutomationError, match="not sent"):
        applescript.send_keystroke("s", "command")
    with pytest.raises(applescript.UIAutomationError, match="not sent"):
        applescript.menu_pick(["File", "Save Project"])
    assert scripts == []


def test_bad_modifier_is_rejected_before_focusing(monkeypatch):
    monkeypatch.setattr(applescript, "focus_resolve", lambda: pytest.fail("must not focus"))
    with pytest.raises(applescript.UIAutomationError, match="hyper"):
        applescript.send_keystroke("s", "hyper")


def test_frontmost_bundle_id_parses_lsappinfo(monkeypatch):
    class Result:
        def __init__(self, stdout):
            self.stdout, self.returncode = stdout, 0

    responses = [Result("ASN:0x0-0x12345:\n"),
                 Result('"CFBundleIdentifier"="com.blackmagic-design.DaVinciResolve"\n')]
    argv = []

    def run(args, **_kwargs):
        argv.append(args)
        return responses.pop(0)

    monkeypatch.setattr(applescript.subprocess, "run", run)
    assert applescript.frontmost_bundle_id() == applescript.RESOLVE_BUNDLE_ID
    assert argv[1] == ["lsappinfo", "info", "-only", "bundleid", "ASN:0x0-0x12345:"]


def test_frontmost_bundle_id_survives_missing_lsappinfo(monkeypatch):
    def run(*_a, **_k):
        raise FileNotFoundError("lsappinfo")

    monkeypatch.setattr(applescript.subprocess, "run", run)
    assert applescript.frontmost_bundle_id() is None


def test_focus_resolve_gives_up_when_another_app_stays_in_front(monkeypatch):
    monkeypatch.setattr(applescript, "osascript", lambda *_a, **_k: "")
    monkeypatch.setattr(applescript, "frontmost_bundle_id", lambda: "com.example.Other")
    assert applescript.focus_resolve(timeout=0, poll=0) is False


def test_focus_resolve_waits_until_resolve_is_frontmost(monkeypatch):
    fronts = iter(["com.example.Other", applescript.RESOLVE_BUNDLE_ID])
    monkeypatch.setattr(applescript, "osascript", lambda *_a, **_k: "")
    monkeypatch.setattr(applescript, "frontmost_bundle_id", lambda: next(fronts))
    assert applescript.focus_resolve(timeout=5, poll=0) is True


def test_focus_resolve_returns_false_when_activation_is_denied(monkeypatch):
    def denied(*_a, **_k):
        raise applescript.UIAutomationError("not authorized")

    monkeypatch.setattr(applescript, "osascript", denied)
    assert applescript.focus_resolve() is False


def test_menu_pick_escapes_quotes_and_backslashes(monkeypatch):
    captured = {}
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda script, **_k: captured.setdefault("s", script))
    applescript.menu_pick(["File", 'Save "As"\\Copy'])
    assert 'menu item "Save \\"As\\"\\\\Copy"' in captured["s"]


@pytest.mark.parametrize("name", ["trigger_scene_cut_detection", "trigger_auto_subtitle"])
def test_dialog_features_say_the_user_still_has_to_finish(monkeypatch, name):
    from resolve_pilot.ui_automation import ai_features
    monkeypatch.setattr(applescript, "focus_resolve", lambda: True)
    monkeypatch.setattr(applescript, "osascript", lambda *_a, **_k: "")
    monkeypatch.setattr(ai_features, "_wait", lambda _ms: None)
    out = getattr(ai_features, name)()
    assert out["ok"] is True and out["completed"] is False and out["next_step"]
