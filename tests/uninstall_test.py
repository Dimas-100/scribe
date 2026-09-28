r"""
Standalone probe for uninstall.py - removing Scribe from a PC.

Run from the project root:   venv\Scripts\python tests\uninstall_test.py

Safe: a temp SCRIBE_DATA_DIR is set before anything is imported, so the
registry, Credential Manager and your real data folder are out of reach
(autostart and keystore keep their hands off an override folder); shortcuts
are made and removed in temp folders only.
"""

import os
import sys
import tempfile
from unittest import mock

TMP = tempfile.mkdtemp(prefix="scribe-uninst-test-")
os.environ["SCRIBE_DATA_DIR"] = TMP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import install_shortcut  # noqa: E402
import storage  # noqa: E402
import uninstall  # noqa: E402
from win32com.client import Dispatch  # noqa: E402


def _other_shortcut(path):
    lnk = Dispatch("WScript.Shell").CreateShortcut(path)
    lnk.TargetPath = sys.executable
    lnk.Arguments = r'"C:\somewhere\else\app.py" --show'
    lnk.Save()


def test_only_this_copys_shortcuts_go():
    start, pins = tempfile.mkdtemp(), tempfile.mkdtemp()
    ours = os.path.join(start, "Scribe.lnk")
    install_shortcut.write_shortcut(ours, sys.executable)
    pin = os.path.join(pins, "Scribe.lnk")
    install_shortcut.write_shortcut(pin, sys.executable)
    theirs = os.path.join(start, "Other Scribe.lnk")
    _other_shortcut(theirs)
    open(os.path.join(start, "notes.txt"), "w").close()
    removed = uninstall.remove_shortcuts((start, pins, os.path.join(start, "missing")))
    assert sorted(removed) == sorted([ours, pin]), removed
    assert os.path.exists(theirs), "another copy's shortcut stays"
    print("PASS  only the shortcuts that start THIS copy are removed.")


def test_only_a_real_data_folder_can_be_deleted():
    assert storage.DATA_DIR == TMP, "the test must run on its own data folder"
    empty = tempfile.mkdtemp()
    assert not uninstall.looks_like_data_folder(empty), "no Scribe files: not ours"
    open(os.path.join(empty, "config.json"), "w").close()
    assert uninstall.looks_like_data_folder(empty)
    for never in (os.path.expanduser("~"), "C:\\", storage.APP_DIR, os.path.join(TMP, "nope")):
        assert not uninstall.looks_like_data_folder(never), never
    print("PASS  only a folder holding Scribe's own files is ever offered for deletion.")


def test_main_asks_before_deleting():
    with open(os.path.join(TMP, "dictation_log.jsonl"), "w") as f:
        f.write("{}\n")
    common = [mock.patch.object(uninstall.updates, "stop_scribe", return_value=True),
              mock.patch.object(uninstall, "remove_shortcuts", return_value=[]),
              mock.patch.object(uninstall, "model_folders", return_value=[]),
              mock.patch.object(uninstall.subprocess, "Popen")]
    for p in common:
        p.start()
    try:
        with mock.patch("builtins.input", side_effect=["n", "n"]), \
             mock.patch.object(uninstall.keystore, "delete_key") as delete_key:
            assert uninstall.main() == 0
        assert os.path.exists(os.path.join(TMP, "dictation_log.jsonl")), "no means no"
        assert not delete_key.called
        with mock.patch("builtins.input", side_effect=["y", "y"]), \
             mock.patch.object(uninstall.keystore, "delete_key") as delete_key:
            assert uninstall.main() == 0
        assert not os.path.exists(TMP), "yes deletes the data folder"
        assert sorted(c.args[0] for c in delete_key.call_args_list) == ["elevenlabs", "groq"]
    finally:
        for p in common:
            p.stop()
    with mock.patch.object(uninstall.updates, "stop_scribe", return_value=False), \
         mock.patch.object(uninstall, "remove_shortcuts") as rs:
        assert uninstall.main() == 1 and not rs.called, "nothing happens while Scribe runs"
    print("PASS  uninstall asks before deleting data or keys, and waits for Scribe to quit.")


def test_new_icons_show_at_once():
    # After an update brings a new scribe.ico, Windows keeps showing the old
    # picture on the Start menu and taskbar until it is told icons changed.
    with mock.patch.object(install_shortcut.ctypes.windll.shell32, "SHChangeNotify") as notify:
        install_shortcut.refresh_icons()
    assert notify.call_args.args[0] == 0x08000000, "SHCNE_ASSOCCHANGED"
    start = tempfile.mkdtemp()
    with mock.patch.object(install_shortcut, "SHORTCUT_PATH", os.path.join(start, "Scribe.lnk")),          mock.patch.object(install_shortcut, "PINNED_TASKBAR", os.path.join(start, "none")),          mock.patch.object(install_shortcut, "find_pythonw", return_value=sys.executable),          mock.patch.dict(os.environ, {"SCRIBE_DATA_DIR": ""}),          mock.patch.object(install_shortcut, "refresh_icons") as refresh:
        install_shortcut.main()
    assert refresh.called, "setup refreshes the icons after writing the shortcuts"
    print("PASS  after (re)writing the shortcuts, Windows is told the icons changed.")


if __name__ == "__main__":
    test_only_this_copys_shortcuts_go()
    test_only_a_real_data_folder_can_be_deleted()
    test_main_asks_before_deleting()
    test_new_icons_show_at_once()
    print("\nAll uninstall tests passed.")
