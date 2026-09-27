"""
=============================================================================
 SCRIBE UIA TEXT - read back a text box in another app.
=============================================================================

 Windows UI Automation is how screen readers read apps: most text boxes -
 in browsers (and so Gmail, ChatGPT, Slack on the web), Office, and most
 chat apps - tell it what they contain. fix_watch.py uses this Reader to
 look at the box Scribe just typed into, so it can learn the words you fix.

   Reader()              - start UI Automation (raises if it can't: no
                           comtypes, or Windows refuses). Make it, and use
                           it, on ONE thread - COM objects belong to the
                           thread that created them.
   focused_box(hwnd)     - the focused text box, if it is in window `hwnd`'s
                           app, is NOT a password box, and can be read.
                           None otherwise.
   read(box)             - its text (at most MAX_CHARS), or None if it's
                           gone. Never raises.

 Some apps don't share their text (VS Code's editor unless its screen-reader
 mode is on, games, remote desktops): then focused_box() is None and Scribe
 simply doesn't learn there. Nothing read here is kept or sent anywhere.
=============================================================================
"""

import ctypes
from ctypes import wintypes

MAX_CHARS = 20000            # enough for any chat box; a huge document is cut
_TEXT_PATTERN = 10014        # UIA_TextPatternId
_VALUE_PATTERN = 10002       # UIA_ValuePatternId
_CUIAUTOMATION = "{ff48dba4-60ef-4201-aa87-54103eef594e}"


def _window_process(hwnd):
    """The process id that owns window `hwnd` (0 if there's no such window)."""
    pid = wintypes.DWORD()
    try:
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    except Exception:
        return 0
    return pid.value


class Reader:
    def __init__(self):
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()              # this thread's COM apartment
        # The UI Automation type library - comtypes generates its Python
        # wrapper once and caches it.
        self._uia_lib = comtypes.client.GetModule("UIAutomationCore.dll")
        self._uia = comtypes.client.CreateObject(
            _CUIAUTOMATION, interface=self._uia_lib.IUIAutomation)

    def focused_box(self, hwnd):
        """The focused, readable, non-password text box of `hwnd`'s app - as
        (pattern id, pattern) - or None."""
        try:
            pid = _window_process(hwnd)
            if not pid:
                return None
            element = self._uia.GetFocusedElement()
            if element is None or element.CurrentProcessId != pid:
                return None                  # focus moved to another app
            if element.CurrentIsPassword:
                return None                  # never read a password box
            for pattern_id, interface in (
                    (_TEXT_PATTERN, self._uia_lib.IUIAutomationTextPattern),
                    (_VALUE_PATTERN, self._uia_lib.IUIAutomationValuePattern)):
                pattern = element.GetCurrentPattern(pattern_id)
                if pattern:
                    return (pattern_id, pattern.QueryInterface(interface))
        except Exception:
            return None
        return None

    def read(self, box):
        """The box's text now, at most MAX_CHARS - or None if it can't be read."""
        try:
            pattern_id, pattern = box
            if pattern_id == _TEXT_PATTERN:
                return pattern.DocumentRange.GetText(MAX_CHARS)
            if pattern_id == _VALUE_PATTERN:
                return (pattern.CurrentValue or "")[:MAX_CHARS]
        except Exception:
            return None
        return None
