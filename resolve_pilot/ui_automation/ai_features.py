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
    StudioOnlyFeature,
    focus_resolve,
    menu_pick,
    osascript,
    send_keystroke,
)

# Menu labels — verified against Resolve 21.1.1 (English UI). Override here if a
# future release renames them. Smart Reframe and Voice Isolation have no menu
# entry in 21.x (they live in the Inspector), so they are not listed.
LABELS = {
    "scene_cut_detect":     ["Timeline", "Detect Scene Cuts"],
    "speed_warp":           ["Clip", "Clip Speed", "Retime Controls"],
    "auto_subtitle":        ["Timeline", "AI Tools", "Create Subtitles from Audio…"],
}


def _wait(ms: int) -> None:
    time.sleep(ms / 1000.0)


def open_color_page() -> None:
    """Switch to the Color page where Magic Mask lives."""
    from resolve_pilot.ui_automation.applescript import open_page
    open_page("color")
    _wait(400)


def trigger_smart_reframe(target_aspect: Literal["1:1", "9:16", "4:5", "16:9"] = "9:16") -> dict:
    """Explain how to run Smart Reframe — it has no menu entry to automate.

    Since Resolve 21 Smart Reframe lives only in the Inspector (Studio-only),
    so there is nothing reliable to click; we return manual steps instead.
    """
    return {
        "ok": False,
        "error": (
            "Smart Reframe has no menu entry in Resolve 21 and is Studio-only. "
            "Manually: set the timeline to the target aspect, select the clip, "
            "open Inspector > Video > Smart Reframe, choose framing="
            f"'{target_aspect}' and click Reframe."
        ),
    }


def trigger_voice_isolation(intensity: int = 60) -> dict:
    """Explain how to run Voice Isolation — it has no menu entry to automate.

    Since Resolve 21 Voice Isolation lives only in the Inspector (Studio-only),
    so there is nothing reliable to click; we return manual steps instead.
    """
    return {
        "ok": False,
        "error": (
            "Voice Isolation has no menu entry in Resolve 21 and is Studio-only. "
            "Manually: select the audio clip, open Inspector > Audio, enable "
            f"Voice Isolation and set the amount to {intensity}."
        ),
    }


def trigger_scene_cut_detection() -> dict:
    """Run Resolve's built-in scene cut detection on the active timeline."""
    try:
        menu_pick(LABELS["scene_cut_detect"])
    except StudioOnlyFeature as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "completed": False,
        "next_step": "Adjust sensitivity and click Add Cuts to Selected Clip in the dialog.",
        "message": (
            "Detect Scene Cuts dialog opened. Adjust sensitivity if needed, "
            "click Add Cuts to Selected Clip. Resulting cuts appear as edits "
            "in the timeline."
        ),
    }


def trigger_auto_subtitle(language: str = "en") -> dict:
    """Open the Create Subtitles from Audio dialog on the current timeline."""
    try:
        menu_pick(LABELS["auto_subtitle"])
    except StudioOnlyFeature as e:
        return {"ok": False, "error": str(e)}
    _wait(500)
    return {
        "ok": True,
        "completed": False,
        "next_step": "Set the subtitle options and click Create in the dialog.",
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
        "completed": False,
        "next_step": "Open the Magic Mask panel and track the subject in Resolve.",
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
    send_keystroke("r", "command")  # default keyboard shortcut for Retime Controls
    return {
        "ok": True,
        "completed": False,
        "next_step": "Choose Optical Flow and Speed Warp in the Retime Process menu.",
        "message": (
            "Retime Controls opened on selected clip. Right-click the speed indicator "
            "above the clip → Retime Process → Optical Flow → Speed Warp 2 (Studio)."
        ),
    }
