r"""
Standalone probe for Scribe's user-facing notifications and crash handling.

Run from the project root:   venv\Scripts\python tests\notify_test.py
"""

import os
import sys
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402

app = import_app_with_mocks()
app.NOTICE_SPACING_SECONDS = 0


def test_queued_until_tray_exists_then_flushed():
    app.tray_icon = None
    app._pending_notices.clear()
    app._notice_last.clear()
    assert app.notify("t1", "Title one", "Message one") is True
    assert app._pending_notices == [("Title one", "Message one")]
    app.tray_icon = mock.MagicMock()
    app._flush_pending_notices()
    app.tray_icon.notify.assert_called_once_with("Message one", "Title one")
    assert app._pending_notices == []
    print("PASS  notices raised before the tray exists are shown once it does.")


def test_rate_limited_per_key():
    app.tray_icon = mock.MagicMock()
    app._notice_last.clear()
    assert app.notify("same", "A", "a", cooldown=600) is True
    assert app.notify("same", "A", "a", cooldown=600) is False
    assert app.notify("other", "B", "b", cooldown=600) is True
    assert app.tray_icon.notify.call_count == 2
    print("PASS  a repeating problem notifies once per cooldown, per key.")


def test_startup_notices_shown():
    app.tray_icon = mock.MagicMock()
    app._notice_last.clear()
    app._show_notices([{"key": "settings_reset", "title": "Settings reset", "message": "m"}])
    app.tray_icon.notify.assert_called_with("m", "Settings reset")
    print("PASS  storage notices are shown through notify().")


def test_background_thread_error_is_logged_and_reported():
    app.tray_icon = mock.MagicMock()
    app._notice_last.clear()
    t = threading.Thread(target=lambda: 1 / 0, name="probe-thread")
    t.start()
    t.join()
    with open(app.ERROR_LOG, encoding="utf-8") as f:
        assert "ZeroDivisionError" in f.read()
    titles = [c.args[1] for c in app.tray_icon.notify.call_args_list]
    assert "Something went wrong" in titles, titles
    print("PASS  an uncaught background-thread error is logged and reported.")


def test_startup_crash_shows_message_box():
    with mock.patch.object(app, "_message_box") as box:
        app.startup_complete = False
        try:
            raise RuntimeError("model folder missing")
        except RuntimeError:
            app._handle_uncaught(*sys.exc_info())
    title, text = box.call_args.args
    assert title == "Scribe couldn't start" and "model folder missing" in text
    assert app.ERROR_LOG in text
    print("PASS  an uncaught startup error explains itself in a message box.")


def test_dashboard_crash_is_reported_with_reason():
    app.tray_icon = mock.MagicMock()
    app._notice_last.clear()
    with open(app.storage.DASHBOARD_LOG, "w", encoding="utf-8") as f:
        f.write("some pywebview noise\nRuntimeError: WebView2 failed to start\n")
    app.dashboard_proc = mock.MagicMock()
    app.dashboard_proc.poll.return_value = 1
    app._check_dashboard_started()
    app.tray_icon.notify.assert_called_with("RuntimeError: WebView2 failed to start",
                                            "Dashboard didn't open")
    print("PASS  a dashboard that crashes on launch is reported with its reason.")


def test_dashboard_webview2_exit_is_not_double_reported():
    app.tray_icon = mock.MagicMock()
    app._notice_last.clear()
    app.dashboard_proc = mock.MagicMock()
    app.dashboard_proc.poll.return_value = 2       # it already showed its own dialog
    app._check_dashboard_started()
    assert not app.tray_icon.notify.called
    print("PASS  the WebView2 dialog isn't followed by a duplicate notice.")


def test_dashboard_opens_even_if_its_log_cannot_be_created():
    import subprocess
    app.root = mock.MagicMock()
    app.dashboard_proc = None
    real_log = app.storage.DASHBOARD_LOG
    app.storage.DASHBOARD_LOG = os.path.join(app._test_data_dir, "no", "such", "dir", "log.txt")
    try:
        with mock.patch.object(subprocess, "Popen") as popen:
            app.open_dashboard()
    finally:
        app.storage.DASHBOARD_LOG = real_log
    assert popen.called, "the dashboard must still launch"
    assert popen.call_args.kwargs["stdout"] == subprocess.DEVNULL
    print("PASS  the dashboard still opens when its log file can't be created.")

if __name__ == "__main__":
    test_queued_until_tray_exists_then_flushed()
    test_rate_limited_per_key()
    test_startup_notices_shown()
    test_background_thread_error_is_logged_and_reported()
    test_startup_crash_shows_message_box()
    test_dashboard_crash_is_reported_with_reason()
    test_dashboard_webview2_exit_is_not_double_reported()
    test_dashboard_opens_even_if_its_log_cannot_be_created()
    print("\nAll notification tests passed.")
