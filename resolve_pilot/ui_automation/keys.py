"""A key press for one app only (macOS).

System Events sends its key presses to whichever app is in front at that instant. For a
Return that runs code this is one check too few: an app that takes the front between the
look and the press gets the Return instead (a half-written message is sent, a dialog is
confirmed). A key event posted to a process id reaches that process and no other, also
when it is not in front, and carries no modifier key the user happens to hold.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import platform

RETURN_KEY = 36


def _core_graphics():
    if platform.system() != "Darwin":
        return None
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        lib.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        lib.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        lib.CGEventSetFlags.restype = None
        lib.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        lib.CGEventPostToPid.restype = None
        lib.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
        lib.CFRelease.restype = None
        lib.CFRelease.argtypes = [ctypes.c_void_p]
        return lib
    except (OSError, AttributeError, TypeError):
        return None


def press_return(pid: int, lib=None) -> bool:
    """Press Return in the process `pid` and nowhere else. False when that cannot be done
    here; the caller then has to press it another way. True says the key was sent, not
    that the app acted on it."""
    lib = lib or _core_graphics()
    if lib is None or pid <= 0:
        return False
    for down in (True, False):
        event = lib.CGEventCreateKeyboardEvent(None, RETURN_KEY, down)
        if not event:
            return False
        try:
            lib.CGEventSetFlags(event, 0)   # no Shift or Command the user is holding
            lib.CGEventPostToPid(pid, event)
        finally:
            lib.CFRelease(event)
    return True
