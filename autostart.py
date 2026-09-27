"""
=============================================================================
 SCRIBE AUTOSTART - "Start Scribe when I sign in".
=============================================================================

 Windows starts every program listed under
     HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
 when you sign in. Scribe adds (or removes) one value there, "Scribe", that
 runs this folder's app.py with pythonw - WITHOUT --show, so it starts
 quietly in the tray. It also shows up in Task Manager -> Startup apps, and
 if you switch it off there, Windows records that in a second key
 (StartupApproved) - which is_enabled() honors.

 The registry is the source of truth: nothing about this is kept in
 config.json, so the setting can never disagree with what Windows will do.
=============================================================================
"""

import os
import sys

try:
    import winreg
except ImportError:          # not Windows
    winreg = None

import storage

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "Scribe"
APP_PY = os.path.join(storage.APP_DIR, "app.py")


def _pythonw():
    """pythonw.exe next to the running interpreter (no console window)."""
    exe = sys.executable
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return candidate if os.path.exists(candidate) else exe


def command():
    """The command line Windows runs at sign-in."""
    return f'"{_pythonw()}" "{APP_PY}"'


def available():
    """Can Scribe manage start-at-sign-in here? Not off Windows, and not for
    a copy running on its own data folder (SCRIBE_DATA_DIR: the tests, a
    portable copy) - that must never repoint the real "Scribe" entry."""
    return winreg is not None and not storage.USING_OVERRIDE


def _turned_off_in_task_manager(approved_path):
    """Task Manager keeps its on/off switch as binary data whose first byte
    is odd when the app is disabled."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved_path) as key:
            data, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return isinstance(data, bytes) and len(data) > 0 and data[0] % 2 == 1


def _points_here(key_path):
    """True if the Run value exists and starts THIS folder's app.py (not
    another copy of Scribe's)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    return os.path.normcase(APP_PY) in os.path.normcase(str(value))


def is_enabled(key_path=RUN_KEY, approved_path=APPROVED_KEY):
    """True if Windows will start THIS Scribe (this folder's app.py) at sign-in."""
    if winreg is None or (key_path == RUN_KEY and not available()):
        return False
    return _points_here(key_path) and not _turned_off_in_task_manager(approved_path)


def set_enabled(enabled, key_path=RUN_KEY, approved_path=APPROVED_KEY):
    """Turn start-at-sign-in on or off. Raises storage.StorageError with a
    readable reason if Windows refuses."""
    if winreg is None:
        raise storage.StorageError("Starting at sign-in is only available on Windows.")
    if key_path == RUN_KEY and not available():
        return                              # a test / portable copy: hands off
    try:
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
            _delete_value(approved_path)  # clear a Task Manager "off" switch
        elif _points_here(key_path):
            # Only remove OUR entry - another copy of Scribe (a second clone)
            # may own the "Scribe" value, and turning this one off must not
            # silently turn that one off too.
            _delete_value(key_path)
    except OSError as exc:
        raise storage.StorageError(
            f"Couldn't change the sign-in setting ({exc.strerror or exc}).")


def _delete_value(path):
    """Remove our value from `path`; already gone is fine."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
