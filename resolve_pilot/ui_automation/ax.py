"""macOS Accessibility called directly, for one app's windows (no System Events, no pyobjc).

System Events can only name an app's windows from its window list, and that list leaves
out what is not on screen: a panel that hides while its app is in the background, a window
on another desktop. The Accessibility API itself still hands such a window out as the app's
focused window, and an element once found keeps answering wherever its window is. That is
what lets Resolve's Console be used without bringing Resolve forward.

Only what that needs is here: read an attribute, set a text or a flag, perform an action.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import platform

_UTF8 = 0x08000100
_SIZE = 2                      # kAXValueCGSizeType
NO_VALUE = (-25212, -25205)    # no value, attribute not supported: an answer, not a failure
INVALID = -25202               # the element is gone (window closed, app quit)
CANNOT_COMPLETE = -25204       # the app did not answer in time: busy, or gone
NOT_ALLOWED = -25211           # this process may not use Accessibility


class AXError(Exception):
    def __init__(self, code: int, what: str = ""):
        super().__init__(f"Accessibility error {code}" + (f" ({what})" if what else ""))
        self.code = code

    @property
    def busy(self) -> bool:
        return self.code == CANNOT_COMPLETE


class _Range(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


class _Size(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _Api:
    def __init__(self) -> None:
        ax = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        ref, index, err = ctypes.c_void_p, ctypes.c_long, ctypes.c_int32

        def fn(lib, name, restype, *argtypes):
            f = getattr(lib, name)
            f.restype, f.argtypes = restype, list(argtypes)
            return f

        self.trusted = fn(ax, "AXIsProcessTrusted", ctypes.c_bool)
        self.application = fn(ax, "AXUIElementCreateApplication", ref, ctypes.c_int32)
        self.system_wide = fn(ax, "AXUIElementCreateSystemWide", ref)
        self.set_timeout = fn(ax, "AXUIElementSetMessagingTimeout", err, ref, ctypes.c_float)
        self.copy = fn(ax, "AXUIElementCopyAttributeValue", err, ref, ref, ctypes.POINTER(ref))
        self.set = fn(ax, "AXUIElementSetAttributeValue", err, ref, ref, ref)
        self.perform = fn(ax, "AXUIElementPerformAction", err, ref, ref)
        self.element_type = fn(ax, "AXUIElementGetTypeID", ctypes.c_ulong)
        self.value_type = fn(ax, "AXValueGetTypeID", ctypes.c_ulong)
        self.value_get = fn(ax, "AXValueGetValue", ctypes.c_bool, ref, ctypes.c_uint32, ctypes.c_void_p)
        self.retain = fn(cf, "CFRetain", ref, ref)
        self.release = fn(cf, "CFRelease", None, ref)
        self.type_of = fn(cf, "CFGetTypeID", ctypes.c_ulong, ref)
        self.string_type = fn(cf, "CFStringGetTypeID", ctypes.c_ulong)
        self.array_type = fn(cf, "CFArrayGetTypeID", ctypes.c_ulong)
        self.boolean_type = fn(cf, "CFBooleanGetTypeID", ctypes.c_ulong)
        self.boolean_value = fn(cf, "CFBooleanGetValue", ctypes.c_bool, ref)
        self.string_new = fn(cf, "CFStringCreateWithBytes", ref, ref, ctypes.c_char_p, index,
                             ctypes.c_uint32, ctypes.c_bool)
        self.string_length = fn(cf, "CFStringGetLength", index, ref)
        self.string_bytes = fn(cf, "CFStringGetBytes", index, ref, _Range, ctypes.c_uint32,
                               ctypes.c_ubyte, ctypes.c_bool, ctypes.c_char_p, index,
                               ctypes.POINTER(index))
        self.array_count = fn(cf, "CFArrayGetCount", index, ref)
        self.array_item = fn(cf, "CFArrayGetValueAtIndex", ref, ref, index)
        self.true = ref.in_dll(cf, "kCFBooleanTrue")
        self.false = ref.in_dll(cf, "kCFBooleanFalse")
        self.names: dict[str, int] = {}

    def name(self, text: str) -> int:
        """A CFString for an attribute or action name; kept for the life of the process."""
        if text not in self.names:
            self.names[text] = self.string(text)
        return self.names[text]

    def string(self, text: str) -> int:
        data = text.encode("utf-8", "surrogatepass")
        made = self.string_new(None, data, len(data), _UTF8, False)
        if not made:
            raise AXError(0, "text could not be handed to macOS")
        return made

    def text(self, string: int) -> str:
        length = self.string_length(string)
        if length <= 0:
            return ""
        size = length * 4          # UTF-8 needs at most four bytes for a UTF-16 unit
        buffer = ctypes.create_string_buffer(size)
        used = ctypes.c_long(0)
        self.string_bytes(string, _Range(0, length), _UTF8, ord("?"), False, buffer, size,
                          ctypes.byref(used))
        return buffer.raw[:used.value].decode("utf-8", "replace")


_api: _Api | None = None
_tried = False


def _load() -> _Api | None:
    global _api, _tried
    if not _tried:
        _tried = True
        if platform.system() == "Darwin":
            try:
                _api = _Api()
            except (OSError, AttributeError, ValueError):
                _api = None
    return _api


def available() -> bool:
    """Whether this process can call Accessibility at all and has been allowed to."""
    api = _load()
    return bool(api and api.trusted())


def set_timeout(seconds: float) -> None:
    """How long any call here waits for an app that does not answer (macOS: six seconds)."""
    api = _load()
    if api:
        everything = api.system_wide()
        try:
            api.set_timeout(everything, seconds)
        finally:
            api.release(everything)


class Element:
    """One window, text area or app as Accessibility knows it. Stays usable for as long as
    the thing itself exists; after that every call raises AXError (INVALID)."""

    def __init__(self, api: _Api, ref: int):
        self._api, self._ref = api, ref     # owns one reference

    def __del__(self):
        if self._ref:
            self._api.release(self._ref)
            self._ref = None

    def get(self, attribute: str):
        """The attribute as text, bool, Element, list of Elements or (width, height); None
        when it has no value."""
        api, out = self._api, ctypes.c_void_p()
        code = api.copy(self._ref, api.name(attribute), ctypes.byref(out))
        if code in NO_VALUE or (code == 0 and not out.value):
            return None
        if code != 0:
            raise AXError(code, attribute)
        return _python(api, out.value, owned=True)

    def set(self, attribute: str, value: str | bool) -> None:
        api = self._api
        if isinstance(value, bool):
            code = api.set(self._ref, api.name(attribute), api.true if value else api.false)
        else:
            made = api.string(value)
            try:
                code = api.set(self._ref, api.name(attribute), made)
            finally:
                api.release(made)
        if code != 0:
            raise AXError(code, attribute)

    def perform(self, action: str) -> None:
        code = self._api.perform(self._ref, self._api.name(action))
        if code != 0:
            raise AXError(code, action)


def _python(api: _Api, ref: int, owned: bool):
    """A value from Accessibility as a Python one. `owned`: the reference is ours to give up."""
    kind = api.type_of(ref)
    if kind == api.element_type():
        if not owned:
            api.retain(ref)
        return Element(api, ref)
    try:
        if kind == api.string_type():
            return api.text(ref)
        if kind == api.boolean_type():
            return bool(api.boolean_value(ref))
        if kind == api.array_type():
            return [_python(api, api.array_item(ref, i), owned=False)
                    for i in range(api.array_count(ref))]
        if kind == api.value_type():
            size = _Size()
            if api.value_get(ref, _SIZE, ctypes.byref(size)):
                return (size.width, size.height)
        return None
    finally:
        if owned:
            api.release(ref)


def application(pid: int) -> Element:
    """The app running as process `pid`."""
    api = _load()
    if api is None:
        raise AXError(NOT_ALLOWED, "Accessibility is not available here")
    made = api.application(pid)
    if not made:
        raise AXError(INVALID, "no such process")
    return Element(api, made)
