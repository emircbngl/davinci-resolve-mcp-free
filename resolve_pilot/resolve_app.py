"""Which DaVinci Resolve is installed here, read from the app bundle (macOS)."""
from __future__ import annotations

import platform
from pathlib import Path

MAC_RESOLVE_APPS = (
    Path("/Applications/DaVinci Resolve/DaVinci Resolve.app"),
    Path("/Applications/DaVinci Resolve Studio/DaVinci Resolve Studio.app"),
)


def installed() -> tuple[str | None, bool]:
    """Installed Resolve version and whether it is Studio.

    (None, False) off macOS or if unknown; with both editions installed, Free is reported.
    """
    if platform.system() != "Darwin":
        return None, False
    import plistlib
    for app in MAC_RESOLVE_APPS:
        try:
            with open(app / "Contents" / "Info.plist", "rb") as f:
                found = str(plistlib.load(f).get("CFBundleShortVersionString") or "") or None
            return found, "Studio" in app.name
        except (OSError, plistlib.InvalidFileException):
            continue
    return None, False


def version_at_least(text: str | None, major: int, minor: int, patch: int = 0) -> bool:
    nums: list[int] = []
    for part in (text or "").split(".")[:3]:
        if not part.isdigit():
            break
        nums.append(int(part))
    if len(nums) < 2:
        return False
    return tuple(nums + [0] * (3 - len(nums))) >= (major, minor, patch)


def file_bridge_possible(version: str | None, studio: bool) -> bool:
    """Whether Lua inside this Resolve can reach files, which the bridge lives on.

    Free 21.1.2 and later cannot (measured on 21.2.0 Free; 21.1.2 from user reports).
    """
    return studio or not version_at_least(version, 21, 1, 2)
