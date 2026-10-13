"""Act on Resolve through whichever channel reaches it.

Three channels, tried in this order when ``channel="auto"``:

- ``studio``  — Resolve Studio's external Python scripting API.
- ``bridge``  — the resolve-pilot Lua bridge running inside Resolve (Free or Studio).
- ``manual``  — nothing live: hand back Lua for the user to paste into the Console.

Every action is written once as a self-contained Lua snippet (used by the bridge
and manual channels) plus a Python equivalent for Studio, and every channel
reports results in the same dict shape, tagged with ``channel``.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from resolve_pilot import bridge
from resolve_pilot.bridge import BridgeError

CHANNELS = ("auto", "studio", "bridge", "manual")

PASTE_INSTRUCTIONS = (
    "In DaVinci Resolve open Workspace > Console, choose the Lua tab, paste the code "
    "and press Enter. The Console prints a [resolve-pilot] line when it is done."
)


class ChannelUnavailable(RuntimeError):
    """The caller asked for a channel that cannot run this action right now."""


_STUDIO_RECHECK_SECONDS = 30.0
_studio_cache: tuple[float, bool, str] | None = None


def studio_status(refresh: bool = False) -> tuple[bool, str]:
    """(available, reason) for the Studio scripting API, cached for a short while.

    Connecting can take a moment when Resolve is not running, and the answer
    rarely changes within a session, so we avoid re-probing on every call.
    """
    global _studio_cache
    now = time.monotonic()
    if not refresh and _studio_cache and now - _studio_cache[0] < _STUDIO_RECHECK_SECONDS:
        return _studio_cache[1], _studio_cache[2]
    try:
        from resolve_pilot.studio_api import get_resolve
        get_resolve()
        result = (True, "Studio scripting API connected.")
    except Exception as e:  # StudioNotAvailable, or anything the native module throws
        result = (False, str(e))
    _studio_cache = (now, *result)
    return result


def choose_channel(prefer: str = "auto", studio_supported: bool = True) -> str:
    """Pick the channel for one action, honoring an explicit preference."""
    if prefer not in CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}; got {prefer!r}")
    if prefer == "manual":
        return "manual"
    if prefer in ("auto", "studio") and studio_supported:
        ok, reason = studio_status()
        if ok:
            return "studio"
        if prefer == "studio":
            raise ChannelUnavailable(f"Studio scripting API is not available: {reason}")
    if prefer == "studio" and not studio_supported:
        raise ChannelUnavailable("This action has no Studio API implementation; use channel='auto'.")
    st = bridge.status()
    if st.running:
        return "bridge"
    if prefer == "bridge":
        raise bridge.BridgeNotRunning(st.detail)
    return "manual"


def perform(
    lua_code: str,
    studio_fn: Callable[[], dict[str, Any]] | None,
    prefer: str = "auto",
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Run one action and return its result dict, tagged with ``channel``."""
    channel = choose_channel(prefer, studio_supported=studio_fn is not None)
    if channel == "studio":
        assert studio_fn is not None
        return {"channel": "studio", **studio_fn()}
    if channel == "bridge":
        r = bridge.exec_lua(lua_code, timeout=timeout)
        if not r.ok:
            raise BridgeError(f"Lua error inside Resolve: {r.error}")
        out: dict[str, Any] = {"channel": "bridge"}
        out.update(r.result if isinstance(r.result, dict) else {"result": r.result})
        return out
    return {
        "channel": "manual",
        "ok": None,
        "lua_code": lua_code,
        "instructions": PASTE_INSTRUCTIONS,
        "note": (
            "Not connected to Resolve, so nothing was changed yet. Give the user lua_code and "
            "the instructions verbatim, or offer setup_bridge for hands-free control."
        ),
    }
