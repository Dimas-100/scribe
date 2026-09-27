r"""
Standalone probe for autostart.py - "Start Scribe when I sign in".

Run from the project root:   venv\Scripts\python tests\autostart_test.py

Safe: uses throwaway registry keys under HKCU\\Software\\ScribeTest (deleted
at the end) - never the real Run key.
"""

import os
import sys
import tempfile
import uuid
import winreg
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["SCRIBE_DATA_DIR"] = tempfile.mkdtemp(prefix="scribe-auto-test-")
for name in ("storage", "autostart"):
    sys.modules.pop(name, None)
import autostart  # noqa: E402

TAG = uuid.uuid4().hex[:10]
PARENT = r"Software\ScribeTest"
RUN = rf"{PARENT}\Run-{TAG}"
APPROVED = rf"{PARENT}\Approved-{TAG}"


def _raw(path):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
        return winreg.QueryValueEx(key, autostart.VALUE_NAME)[0]


def _cleanup():
    for path in (RUN, APPROVED, PARENT):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass          # already gone, or PARENT still holds another run's keys


def test_off_by_default():
    assert autostart.is_enabled(RUN, APPROVED) is False
    print("PASS  off until turned on.")


def test_enable_and_disable():
    autostart.set_enabled(True, RUN, APPROVED)
    assert autostart.is_enabled(RUN, APPROVED) is True
    value = _raw(RUN)
    assert os.path.normcase(autostart.APP_PY) in os.path.normcase(value)
    assert "pythonw" in value.lower() and "--show" not in value, \
        "sign-in starts quietly in the tray"
    autostart.set_enabled(False, RUN, APPROVED)
    assert autostart.is_enabled(RUN, APPROVED) is False
    autostart.set_enabled(False, RUN, APPROVED)          # twice is fine
    print("PASS  on/off writes and removes the Run value (no --show).")


def test_task_manager_switch_is_respected():
    autostart.set_enabled(True, RUN, APPROVED)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APPROVED) as key:
        winreg.SetValueEx(key, autostart.VALUE_NAME, 0, winreg.REG_BINARY,
                          bytes([3] + [0] * 11))           # "disabled"
    assert autostart.is_enabled(RUN, APPROVED) is False
    autostart.set_enabled(True, RUN, APPROVED)             # turning it on again...
    assert autostart.is_enabled(RUN, APPROVED) is True     # ...clears the switch
    print("PASS  Task Manager's Startup-apps switch is honored and reset.")


def test_another_install_is_not_ours():
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN) as key:
        winreg.SetValueEx(key, autostart.VALUE_NAME, 0, winreg.REG_SZ,
                          r'"C:\other\venv\Scripts\pythonw.exe" "C:\other\app.py"')
    assert autostart.is_enabled(RUN, APPROVED) is False
    autostart.set_enabled(False, RUN, APPROVED)      # turning OURS off...
    assert "other" in _raw(RUN), "...must not delete another copy's entry"
    print("PASS  a Run value for a different Scribe folder is neither ours nor deleted.")


def test_override_mode_leaves_the_real_run_key_alone():
    assert autostart.available() is False                  # SCRIBE_DATA_DIR is set
    with mock.patch.object(autostart.winreg, "SetValueEx") as setv, \
         mock.patch.object(autostart, "_delete_value") as delv:
        autostart.set_enabled(True)                         # the REAL key path
        autostart.set_enabled(False)
    assert not setv.called and not delv.called
    assert autostart.is_enabled() is False
    print("PASS  a test/portable copy never touches the real sign-in entry.")


if __name__ == "__main__":
    try:
        test_off_by_default()
        test_enable_and_disable()
        test_task_manager_switch_is_respected()
        test_another_install_is_not_ours()
        test_override_mode_leaves_the_real_run_key_alone()
    finally:
        _cleanup()
    print("\nAll autostart tests passed.")
