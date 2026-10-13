"""macOS UI automation for DaVinci Resolve via AppleScript / System Events.

Resolve has no native AppleScript dictionary, so we drive it through System
Events. This unlocks features the scripting API exposes weakly or not at all:
Magic Mask, Smart Reframe, Voice Isolation toggles, dialog dismissal, page
switching when scripting is disabled (Free edition).

Requires Accessibility permission for the host process (Terminal / Claude /
the MCP server). On first run macOS will prompt; user must approve.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Literal

Page = Literal["media", "cut", "edit", "fusion", "color", "fairlight", "deliver"]
RESOLVE_BUNDLE_ID = "com.blackmagic-design.DaVinciResolve"

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


class StudioOnlyFeature(UIAutomationError):
    """Resolve Free answered a menu click with its 'upgrade to Studio' dialog."""


# Resolve Free's upsell dialog, shown when a Studio-only feature is invoked.
_UPSELL_WINDOW = "Message"
_UPSELL_TEXT = "reached a limitation"
_UPSELL_DISMISS = "Not Yet"


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


def frontmost_bundle_id() -> str | None:
    """Bundle identifier of the frontmost application, or None if it cannot be read."""
    try:
        front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                               timeout=1.0, check=False)
        if front.returncode != 0:
            return None
        asn = re.search(r"(ASN:\S+)", front.stdout)
        if asn is None:
            return None
        info = subprocess.run(["lsappinfo", "info", "-only", "bundleid", asn.group(1)],
                              capture_output=True, text=True, timeout=1.0, check=False)
        if info.returncode != 0:
            return None
        bundle = re.search(r'"CFBundleIdentifier"="([^"]+)"', info.stdout)
        return bundle.group(1) if bundle else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def focus_resolve(wait: bool = True, timeout: float = 3.0, poll: float = 0.1) -> bool:
    """Bring DaVinci Resolve to the front; return whether it actually is frontmost.

    Keystrokes and menu clicks go to whatever app is frontmost, so callers must not
    send them when this returns False (another app would receive them blind).
    """
    try:
        osascript('tell application "DaVinci Resolve" to activate')
    except UIAutomationError:
        return False
    deadline = time.monotonic() + (max(0.0, timeout) if wait else 0.0)
    while True:
        if frontmost_bundle_id() == RESOLVE_BUNDLE_ID:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.0, poll))


def _as_quoted(value: str) -> str:
    """``value`` as an AppleScript string literal (quotes and backslashes escaped)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mods_to_using(mods: str) -> str:
    """Convert 'shift+command' style into AppleScript 'using' clause."""
    parts = [m.strip().lower() for m in mods.split("+") if m.strip()]
    mapping = {
        "command": "command down",
        "cmd": "command down",
        "shift": "shift down",
        "option": "option down",
        "opt": "option down",
        "alt": "option down",
        "control": "control down",
        "ctrl": "control down",
        "ctl": "control down",
    }
    unknown = [p for p in parts if p not in mapping]
    if unknown:
        raise UIAutomationError(
            f"Unknown modifier(s): {', '.join(unknown)}. Use command, shift, option or control.")
    pieces = [mapping[p] for p in parts]
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
    """Send a single keystroke (with optional modifiers) to Resolve.

    Raises UIAutomationError, without typing anything, if Resolve cannot be made
    frontmost or a modifier name is unknown.

    `key` is a single character or a special key name like 'tab', 'space',
    'return', 'escape', 'left'/'right'/'up'/'down', 'f1'..'f12'.
    """
    using = _mods_to_using(modifiers)
    if len(key) == 1:
        cmd = f"keystroke {_as_quoted(key)}"
    else:
        code = _KEY_CODES.get(key.lower())
        if code is None:
            raise UIAutomationError(f"Unknown key name: {key}")
        cmd = f"key code {code}"
    if not focus_resolve():
        raise UIAutomationError("DaVinci Resolve did not become frontmost; keystroke not sent.")
    # Scoped to Resolve's process so the keystroke cannot land in another app.
    script = f'''
tell application "System Events"
    tell process "Resolve"
        set frontmost to true
        {cmd} {using}
    end tell
end tell
'''
    osascript(script)


def open_page(page: Page) -> None:
    """Switch Resolve to the named page (works in Free edition)."""
    if page not in _PAGE_SHORTCUTS:
        raise UIAutomationError(f"Unknown page: {page}")
    key, mods = _PAGE_SHORTCUTS[page]
    send_keystroke(key, mods)


def _normalize_menu_label(label: str) -> str:
    """Resolve labels end in a single '…' character; accept '...' as well."""
    if label.endswith("..."):
        return label[:-3] + "…"
    return label


def menu_pick(menu_path: list[str]) -> None:
    """Click a menu item by path, e.g. ['File', 'Import', 'Timeline...'].

    A trailing '...' is matched against Resolve's '…'. Uses System Events
    accessibility. Requires Accessibility permission.

    Raises StudioOnlyFeature (after dismissing the dialog) when Resolve Free
    responds with its upgrade-to-Studio message.
    """
    if not menu_path:
        raise UIAutomationError("Empty menu path")
    if not focus_resolve():
        raise UIAutomationError("DaVinci Resolve did not become frontmost; menu click not sent.")
    path_quoted = [_as_quoted(_normalize_menu_label(m)) for m in menu_path]
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
        repeat 8 times
            delay 0.15
            try
                set msg to (value of static texts of window "{_UPSELL_WINDOW}") as text
                if msg contains "{_UPSELL_TEXT}" then
                    click button "{_UPSELL_DISMISS}" of window "{_UPSELL_WINDOW}"
                    return "STUDIO_ONLY"
                end if
            end try
        end repeat
    end tell
end tell
'''
    if osascript(script) == "STUDIO_ONLY":
        raise StudioOnlyFeature(
            f"'{' > '.join(menu_path)}' is a Studio-only feature: Resolve Free "
            f"showed its upgrade dialog (dismissed with '{_UPSELL_DISMISS}')."
        )
