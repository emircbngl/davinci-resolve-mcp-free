"""Put LUT files where Resolve's scripting API can apply them.

TimelineItem.SetLUT only accepts LUTs inside Resolve's LUT folder: a .cube anywhere
else makes it return False without an error (measured on Resolve 20.3). ``stage_lut``
copies an outside LUT into ``<LUT folder>/resolve-pilot/`` and returns the path to
hand to SetLUT, after which the snippet calls Project.RefreshLUTList.
"""
from __future__ import annotations

import filecmp
import hashlib
import os
import platform
import shutil
from pathlib import Path

SUBDIR = "resolve-pilot"


def lut_dirs() -> list[Path]:
    """Resolve's LUT folders on this OS, the one we write to first."""
    override = os.environ.get("RESOLVE_PILOT_LUT_DIR")
    if override:
        return [Path(override).expanduser()]
    system = platform.system()
    if system == "Darwin":
        return [Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/LUT")]
    if system == "Windows":
        root = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return [Path(root, "Blackmagic Design", "DaVinci Resolve", "Support", "LUT")]
    return [Path.home() / ".local/share/DaVinciResolve/LUT", Path("/opt/resolve/LUT")]


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def stage_lut(lut_path: str | Path) -> tuple[str, bool]:
    """Return (path for SetLUT, whether a copy was made)."""
    src = Path(lut_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"LUT not found: {src}")
    dirs = lut_dirs()
    if any(_inside(src, d) for d in dirs if d.exists()):
        return str(src), False
    errors = []
    for folder in dirs:
        target_dir = folder / SUBDIR
        target = target_dir / src.name
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if target.exists() and not filecmp.cmp(src, target, shallow=False):
                digest = hashlib.sha1(src.read_bytes()).hexdigest()[:8]
                target = target_dir / f"{src.stem}-{digest}{src.suffix}"
            if not target.exists():
                shutil.copy2(src, target)
            return str(target), True
        except OSError as e:
            errors.append(f"{folder}: {e}")
    raise PermissionError(
        "Resolve only applies LUTs from its LUT folder, and copying this one there failed ("
        + "; ".join(errors) + "). Copy the file into Resolve's LUT folder yourself "
        "(Project Settings > Color Management > Open LUT Folder), then call apply_lut with the copied path."
    )
