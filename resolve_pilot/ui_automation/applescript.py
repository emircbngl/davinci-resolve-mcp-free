"""macOS UI automation for DaVinci Resolve via AppleScript / System Events.

Resolve has no native AppleScript dictionary, so we drive it through System
Events. This unlocks features the scripting API exposes weakly or not at all:
Magic Mask, Smart Reframe, Voice Isolation toggles, dialog dismissal, page
switching when scripting is disabled (Free edition).

Requires Accessibility permission for the host process (Terminal / Claude /
the MCP server). On first run macOS will prompt; user must approve.
"""
from __future__ import annotations

import subprocess
from typing import Literal

Page = Literal["media", "cut", "edit", "fusion", "color", "fairlight", "deliver"]

_PAGE_SHORTCUTS: dict[Page, tuple[str, str]] = {
    # (key, modifiers) — Resolve default page shortcuts. User-customized keymaps
    # will need overrides; we expose them as defaults.
    "media":    ("2", "shift+command"),
    "cut":      ("3", "shift+command"),
    "edit":     ("4", "shift+command"),
    "fusion":   ("5", "shift+command"),
    "color":    ("6", "shift+command"),
    "fairlight":("7", "shift+command"),
    "deliver":  ("8", "shift+command"),
}


class UIAutomationError(RuntimeError):
    pass


def osascript(script: str, timeout: float = 15.0) -> str:
    """Run an AppleScript and return its stdout. Raises on non-zero exit."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise UIAutomationError(f"osascript timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise UIAutomationError(
            f"osascript failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def is_resolve_running() -> bool:
    out = osascript(
        'tell application "System Events" to (name of processes) contains "Resolve"'
    )
    return out.lower() == "true"


def focus_resolve() -> None:
    """Bring DaVinci Resolve to the front."""
    osascript('tell application "DaVinci Resolve" to activate')


def _mods_to_using(mods: str) -> str:
    """Convert 'shift+command' style into AppleScript 'using' clause."""
    parts = [m.strip().lower() for m in mods.split("+") if m.strip()]
    mapping = {
        "command": "command down",
        "cmd": "command down",
        "shift": "shift down",
        "option": "option down",
        "alt": "option down",
        "control": "control down",
        "ctrl": "control down",
    }
    pieces = [mapping[p] for p in parts if p in mapping]
    if not pieces:
        return ""
    return "using {" + ", ".join(pieces) + "}"


_KEY_CODES = {
    "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


def send_keystroke(key: str, modifiers: str = "") -> None:
    """Send a single keystroke (with optional modifiers) to the frontmost app.

    `key` is a single character or a special key name like 'tab', 'space',
    'return', 'escape', 'left'/'right'/'up'/'down', 'f1'..'f12'.
    """
    focus_resolve()
    using = _mods_to_using(modifiers)
    if len(key) == 1:
        esc = key.replace("\\", "\\\\").replace('"', '\\"')
        cmd = f'keystroke "{esc}"'
    else:
        code = _KEY_CODES.get(key.lower())
        if code is None:
            raise UIAutomationError(f"Unknown key name: {key}")
        cmd = f"key code {code}"
    script = f'tell application "System Events" to {cmd} {using}'.strip()
    osascript(script)


def open_page(page: Page) -> None:
    """Switch Resolve to the named page (works in Free edition)."""
    if page not in _PAGE_SHORTCUTS:
        raise UIAutomationError(f"Unknown page: {page}")
    key, mods = _PAGE_SHORTCUTS[page]
    send_keystroke(key, mods)


def menu_pick(menu_path: list[str]) -> None:
    """Click a menu item by path, e.g. ['File', 'Import', 'Timeline...'].

    Uses System Events accessibility. Requires Accessibility permission.
    """
    if not menu_path:
        raise UIAutomationError("Empty menu path")
    focus_resolve()
    path_quoted = [f'"{m}"' for m in menu_path]
    parts = [f'click menu bar item {path_quoted[0]} of menu bar 1']
    cur = f'menu bar item {path_quoted[0]} of menu bar 1'
    last = len(path_quoted) - 1
    for i, m in enumerate(path_quoted[1:], start=1):
        parts.append(f'click menu item {m} of menu 1 of {cur}')
        if i < last:
            cur = f'menu item {m} of menu 1 of {cur}'
    body = "\n            ".join(parts)
    script = f'''
tell application "System Events"
    tell process "Resolve"
        set frontmost to true
        {body}
    end tell
end tell
'''
    osascript(script)
