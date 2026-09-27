"""
Standalone probe for clipboard hygiene. Runs against the REAL Windows
clipboard - and puts back whatever you had on it afterwards. No keystrokes
are sent (app.kbd is mocked).

Run from the project root:   venv\\Scripts\\python tests\\clipboard_test.py
"""

import ctypes
import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402

app = import_app_with_mocks()
PRIVATE = {"ExcludeClipboardContentFromMonitorProcessing",
           "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"}


def clipboard_state():
    """(text, set of registered format names) currently on the clipboard."""
    u = ctypes.windll.user32
    for _ in range(50):
        if u.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("clipboard busy")
    try:
        names, fmt = set(), u.EnumClipboardFormats(0)
        while fmt:
            buf = ctypes.create_unicode_buffer(256)
            if u.GetClipboardFormatNameW(fmt, buf, 256):
                names.add(buf.value)
            fmt = u.EnumClipboardFormats(fmt)
    finally:
        u.CloseClipboard()
    return (app.pyperclip.paste(), names)


def preserved(fn):
    """Run fn, then put the user's own clipboard back exactly as it was."""
    snap = app._clipboard_snapshot()
    try:
        fn()
    finally:
        app._clipboard_restore(snap)


def test_dictation_is_kept_out_of_clipboard_history():
    def body():
        assert app._clipboard_set_text("scribe private test", private=True)
        text, names = clipboard_state()
        assert text == "scribe private test" and PRIVATE <= names, names
    preserved(body)
    print("PASS  a pasted dictation is marked private (no Win+V history, no cloud sync).")


def test_copy_for_user_is_a_normal_copy():
    # Checked without touching the real clipboard: a real normal copy would
    # leave a test entry in the user's Win+V history.
    with mock.patch.object(app, "_clipboard_set_text", return_value=True) as put:
        assert app._copy_for_user("for the user") is True
    put.assert_called_once_with("for the user", private=False)
    print("PASS  text left for the user to paste is a normal copy.")


def test_failed_borrow_gives_back_the_users_clipboard():
    snapshot = {13: "what the user had\0".encode("utf-16-le")}
    with mock.patch.object(app, "_clipboard_snapshot", return_value=snapshot), \
         mock.patch.object(app, "_clipboard_set_text", return_value=False), \
         mock.patch.object(app, "_clipboard_restore") as restore, \
         mock.patch.object(app, "kbd") as kbd:
        app.paste_text("dictated")
    assert restore.call_count == 1 and restore.call_args.args[0] is snapshot
    kbd.type.assert_called_once_with("dictated")
    print("PASS  a failed clipboard borrow gives the user's clipboard back, then types.")


def test_paste_restores_previous_clipboard_after_a_pause():
    def body():
        app._clipboard_set_text("what the user had", private=True)
        with mock.patch.object(app, "kbd") as kbd:
            t0 = time.perf_counter()
            app.paste_text("dictated")
            took = time.perf_counter() - t0
        assert kbd.press.called and took >= app.CLIPBOARD_RESTORE_DELAY
        assert clipboard_state()[0] == "what the user had"
    preserved(body)
    print("PASS  paste gives slow apps time, then hands the old clipboard back.")


def test_busy_clipboard_types_instead_of_dropping():
    with mock.patch.object(app, "_clipboard_set_text", return_value=False), \
         mock.patch.object(app, "kbd") as kbd:
        app.paste_text("still delivered")
    kbd.type.assert_called_once_with("still delivered")
    print("PASS  a busy clipboard means typing, never a lost dictation.")


if __name__ == "__main__":
    test_dictation_is_kept_out_of_clipboard_history()
    test_copy_for_user_is_a_normal_copy()
    test_paste_restores_previous_clipboard_after_a_pause()
    test_busy_clipboard_types_instead_of_dropping()
    test_failed_borrow_gives_back_the_users_clipboard()
    print("\nAll clipboard tests passed.")
