"""UI automation routines for DaVinci Resolve's AI features.

The scripting API only weakly exposes Magic Mask, Smart Reframe, Voice
Isolation and friends. The cleanest workaround is to drive their GUI affordances
through System Events. These functions assume Resolve is frontmost and the
appropriate page is already open — pair them with `open_page` from applescript.py.

Stability note: menu/button labels can drift between Resolve versions. We
isolate every label in one place at the top of the file so updates are easy.
"""
from __future__ import annotations

import time
from typing import Literal

from resolve_pilot.ui_automation.applescript import (
    focus_resolve,
    menu_pick,
    osascript,
    send_keystroke,
)

# Menu labels — known good as of Resolve 19.x / 20.x. Override here if a future
# release renames them.
LABELS = {
    "smart_reframe_menu":   ["Workspace", "Reframe..."],
    "voice_isolation_menu": ["Clip", "Voice Isolation..."],
    "scene_cut_detect":     ["Timeline", "Detect Scene Cuts"],
    "speed_warp":           ["Clip", "Retime Controls"],
    "auto_subtitle":        ["Timeline", "Create Subtitles from Audio..."],
}


def _wait(ms: int) -> None:
    time.sleep(ms / 1000.0)


def open_color_page() -> None:
    """Switch to the Color page where Magic Mask lives."""
    from resolve_pilot.ui_automation.applescript import open_page
    open_page("color")
    _wait(400)


def trigger_smart_reframe(target_aspect: Literal["1:1", "9:16", "4:5", "16:9"] = "9:16") -> dict:
    """Open the Smart Reframe dialog and select the target aspect.

    Smart Reframe runs Resolve's neural engine to track subjects and reframe
    each clip. We open the dialog, wait, and let the user confirm — no fully
    headless invocation because the dialog has confirm-only controls.
    """
    focus_resolve()
    menu_pick(LABELS["smart_reframe_menu"])
    _wait(800)
    # Resolve's dialog has a popup with aspect choices. We can't reliably click
    # arbitrary popup items without UI-element inspection per build, so we
    # nudge the user with a hint.
    return {
        "ok": True,
        "message": (
            "Smart Reframe dialog opened. In the dialog, pick framing="
            f"'{target_aspect}' and click Reframe. Smart Reframe will track "
            "and reframe automatically on the current selection."
        ),
    }


def trigger_voice_isolation(intensity: int = 60) -> dict:
    """Open Voice Isolation on the currently selected audio clip(s).

    intensity: 0–100. We don't drag the slider (per-build coordinates) — we
    leave the dialog open and tell the user the target value.
    """
    focus_resolve()
    menu_pick(LABELS["voice_isolation_menu"])
    _wait(500)
    return {
        "ok": True,
        "message": (
            f"Voice Isolation dialog opened. Set intensity to {intensity} and "
            "click Apply. Re-run this for each clip you want to clean."
        ),
    }


def trigger_scene_cut_detection() -> dict:
    """Run Resolve's built-in scene cut detection on the active timeline."""
    focus_resolve()
    menu_pick(LABELS["scene_cut_detect"])
    return {
        "ok": True,
        "message": (
            "Detect Scene Cuts dialog opened. Adjust sensitivity if needed, "
            "click Add Cuts to Selected Clip. Resulting cuts appear as edits "
            "in the timeline."
        ),
    }


def trigger_auto_subtitle(language: str = "en") -> dict:
    """Open the Create Subtitles from Audio dialog on the current timeline."""
    focus_resolve()
    menu_pick(LABELS["auto_subtitle"])
    _wait(500)
    return {
        "ok": True,
        "message": (
            f"Create Subtitles dialog opened. Set language to '{language}', "
            "tweak max chars/line, click Create. Subtitles appear on a "
            "subtitle track. Note: needs Studio for the auto-transcribe engine."
        ),
    }


def trigger_magic_mask_person() -> dict:
    """Open Magic Mask in person/face mode on the currently selected clip.

    Magic Mask lives on the Color page → Magic Mask panel (bottom of node
    graph). True UI automation of the mask painting requires per-build
    coordinates; we instead open the page and hand the user precise
    instructions for the click-paint step.
    """
    open_color_page()
    return {
        "ok": True,
        "next_steps": [
            "On the Color page, look at the bottom toolbar — click the Magic Mask icon "
            "(person silhouette). The Magic Mask panel opens at the bottom right.",
            "In Magic Mask panel: choose 'Person' mode (default).",
            "Click on the subject in the viewer once — Resolve generates the mask.",
            "Click Track Forward (forward-arrow icon) to track through the clip.",
            "Output of the mask connects to the next node automatically; grade as usual.",
        ],
    }


def open_speed_warp() -> dict:
    """Open the Retime Controls for the selected clip and switch to Speed Warp."""
    focus_resolve()
    send_keystroke("r", "command")  # default keyboard shortcut for Retime Controls
    return {
        "ok": True,
        "message": (
            "Retime Controls opened on selected clip. Right-click the speed indicator "
            "above the clip → Retime Process → Optical Flow → Speed Warp 2 (Studio)."
        ),
    }
