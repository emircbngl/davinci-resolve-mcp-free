"""Quickstart — exercises every layer without going through Claude.

Run with:
    source .venv/bin/activate
    python examples/quickstart.py /path/to/some/video.mp4
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from resolve_pilot.analyzers import detect_scenes, probe_media
from resolve_pilot.editorial.roughcut import (
    RoughCutPlan,
    plan_from_scenes,
    plan_to_timeline_spec,
)
from resolve_pilot.exporters.fcpxml import write_fcpxml
from resolve_pilot.lua_snippets import library as lua_lib


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: quickstart.py <video_file>")
        sys.exit(1)
    src = Path(sys.argv[1]).expanduser().resolve()
    if not src.exists():
        print(f"file not found: {src}")
        sys.exit(2)

    print(f"=== probing {src.name} ===")
    info = probe_media(src)
    print(json.dumps(info.to_dict(), indent=2))

    print("\n=== detecting scenes ===")
    cuts = detect_scenes(src, threshold=12.0)
    print(f"found {len(cuts)} scene cuts")
    for c in cuts[:10]:
        print(f"  {c.pts_seconds:7.2f}s   score={c.score:.2f}")

    print("\n=== building scene-based rough cut ===")
    plan = plan_from_scenes(src, cuts, title=f"{src.stem} — scene cut",
                            min_duration=0.8)
    print(f"plan has {len(plan.clips)} clips, total = "
          f"{sum(c.duration_seconds for c in plan.clips):.1f} s")

    out_fcpxml = src.with_suffix("").with_name(src.stem + "_scenes.fcpxml")
    write_fcpxml(plan_to_timeline_spec(plan), out_fcpxml)
    print(f"wrote {out_fcpxml}")
    print("→ In Resolve: File → Import → Timeline → pick that file.")

    print("\n=== sample Lua snippet ===")
    snippet = lua_lib.snippet_dump_project_info()
    print("Paste into Resolve's Workspace → Console:")
    print("-" * 60)
    print(snippet)


if __name__ == "__main__":
    main()
