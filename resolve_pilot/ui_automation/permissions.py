"""Which app macOS asks about for Accessibility and Screen Recording.

macOS checks privacy permissions against the "responsible" app of a process, which is
usually the app that launched it. Claude Desktop starts MCP servers through a helper
called ``disclaimer`` that hands that responsibility to the child, so the permission is
checked against the Python interpreter running this server (Python.app), not against
Claude. Telling the user "allow Claude" is then wrong; this finds the real app.
"""
from __future__ import annotations

import os
import re
import subprocess

_APP = re.compile(r"^(.*?\.app)(?:/|$)")


def _parent_and_command(pid: int) -> tuple[int, str] | None:
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    ppid, _, command = out.partition(" ")
    if not ppid.strip().isdigit():
        return None
    return int(ppid), command.strip()


def responsible_app(pid: int | None = None, max_depth: int = 16) -> str | None:
    """Path of the app bundle macOS checks permissions for, or None if unknown.

    Walks up the process tree. A ``disclaimer`` helper makes the process below it
    responsible for itself; otherwise the outermost app bundle in the chain is.
    """
    chain: list[str] = []
    pid = os.getpid() if pid is None else pid
    for _ in range(max_depth):
        step = _parent_and_command(pid)
        if step is None:
            break
        ppid, command = step
        chain.append(command)
        if ppid <= 1:
            break
        pid = ppid
    for i, command in enumerate(chain):
        if command.endswith("/disclaimer") and i > 0:
            m = _APP.match(chain[i - 1])
            return m.group(1) if m else chain[i - 1]
    apps = [m.group(1) for m in map(_APP.match, chain) if m]
    return apps[-1] if apps else None


def permission_hint(kind: str) -> str:
    """Where to enable `kind` ("Accessibility" or "Screen Recording") for this server."""
    app = responsible_app()
    where = f"System Settings > Privacy & Security > {kind}"
    if app is None:
        return f"{where}: enable the app that runs this MCP server, then quit and reopen it."
    extra = ""
    if os.path.basename(app) == "Python.app":
        extra = (" This is the Python interpreter that runs resolve-pilot (Claude Desktop starts "
                 "MCP servers so that macOS asks about them, not about Claude); allowing it lets "
                 "any script run by that Python use the permission.")
    return (f"{where}: add and enable {app} (use the + button, then Cmd+Shift+G to paste the "
            f"path), then restart the MCP host app.{extra}")
