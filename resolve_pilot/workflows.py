"""Workflow guides (the SKILL.md playbooks shipped in ``resolve_pilot/skills``).

They are served over MCP — as the ``get_workflow_guide`` tool and as prompts —
so any client gets them, not only clients that install Claude skills.
"""
from __future__ import annotations

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _parse(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    m = _FRONTMATTER.match(text)
    if m:
        for line in m.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip()
        text = text[m.end():]
    return meta, text.strip()


def list_workflows() -> list[dict[str, str]]:
    out = []
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        meta, _ = _parse(path)
        out.append({"name": meta.get("name", path.parent.name), "description": meta.get("description", "")})
    return out


def get_workflow(name: str) -> str:
    key = name.strip().lower()
    for path in SKILLS_DIR.glob("*/SKILL.md"):
        meta, body = _parse(path)
        names = {path.parent.name, meta.get("name", ""), path.parent.name.removeprefix("resolve-")}
        if key in names:
            return body
    available = ", ".join(w["name"] for w in list_workflows())
    raise ValueError(f"Unknown workflow {name!r}. Available: {available}")
