"""Put LUT files where Resolve's scripting API can apply them.

TimelineItem.SetLUT only accepts LUTs inside Resolve's LUT folder: a .cube anywhere
else makes it return False without an error (measured on Resolve 20.3). ``stage_lut``
copies an outside LUT into ``<LUT folder>/resolve-pilot/`` and returns the path to
hand to SetLUT, after which the snippet calls Project.RefreshLUTList.
"""
from __future__ import annotations

import filecmp
import hashlib
import math
import os
import platform
import shutil
from pathlib import Path

from resolve_pilot._paths import state_dir

SUBDIR = "resolve-pilot"

# ── Built-in: phone HDR (HLG, BT.2020) to Rec.709 ─────────────────────────────
#
# An iPhone records HLG with BT.2020 colour. A Resolve project without colour management
# shows those code values as Rec.709: bright, flat and grey. The LUT undoes the HLG
# curve, converts the colours to BT.709 and encodes with a power and a little saturation.
# The two numbers were fitted to Apple's own SDR conversion of the same files (avconvert)
# on five frames of three clips and checked on three others: 35-39 dB PSNR against it,
# where the untouched picture has 17-20 dB and Resolve's own colour management with
# tone mapping 13-14 dB (it comes out brighter still).
HLG_TO_REC709 = "hlg-to-rec709"
BUILTIN_LUTS = (HLG_TO_REC709,)
HLG_COLOR_SPACE = "Rec.2100 HLG"   # what Resolve's "Input Color Space" clip property says
_POWER, _SATURATION = 0.40, 1.14
_BT2020_TO_BT709 = ((1.6605, -0.5876, -0.0728), (-0.1246, 1.1329, -0.0083), (-0.0182, -0.1006, 1.1187))
_LUMA = (0.2126, 0.7152, 0.0722)


def _hlg_to_linear(x: float) -> float:
    """ITU-R BT.2100 HLG signal (0-1) to scene light (0-1)."""
    if x <= 0.5:
        return x * x / 3.0
    return (math.exp((x - 0.55991073) / 0.17883277) + 0.28466892) / 12.0


def hlg_to_rec709(r: float, g: float, b: float) -> tuple[float, float, float]:
    """One HLG / BT.2020 colour (each 0-1) as Rec.709."""
    lin = [_hlg_to_linear(v) for v in (r, g, b)]
    out = [min(1.0, max(0.0, sum(m * v for m, v in zip(row, lin)))) ** _POWER for row in _BT2020_TO_BT709]
    luma = sum(w * v for w, v in zip(_LUMA, out))
    return tuple(min(1.0, max(0.0, luma + _SATURATION * (v - luma))) for v in out)


def hlg_to_rec709_cube(size: int = 33) -> str:
    """The conversion as a .cube 3D LUT (red changes fastest, as the format wants)."""
    steps = [i / (size - 1) for i in range(size)]
    lines = ['TITLE "resolve-pilot HLG BT.2020 to Rec.709"', f"LUT_3D_SIZE {size}"]
    for b in steps:
        for g in steps:
            for r in steps:
                lines.append("%.6f %.6f %.6f" % hlg_to_rec709(r, g, b))
    return "\n".join(lines) + "\n"


def builtin_lut(name: str) -> Path:
    """Write a built-in LUT into resolve-pilot's own folder (once) and return its path."""
    if name != HLG_TO_REC709:
        raise ValueError(f"Unknown built-in LUT {name!r}; there is: {', '.join(BUILTIN_LUTS)}")
    path = state_dir("luts") / f"{name}.cube"
    text = hlg_to_rec709_cube()
    if not path.is_file() or path.read_text() != text:
        path.write_text(text)
    return path


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
