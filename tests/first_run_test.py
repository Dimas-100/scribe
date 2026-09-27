r"""
Standalone probe for the first run: nothing is loaded or saved until the
welcome is finished; "setup-done" applies the choices; a welcome that can't
open falls back to the defaults; --show decides whether the dashboard opens.

Run from the project root:   venv\Scripts\python tests\first_run_test.py
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402


def _with_tray(app):
    app.tray_icon = mock.MagicMock()
    app._tray_ready = True
    return app


def test_first_run_waits_for_the_welcome():
    app = import_app_with_mocks(config=None)
    assert app.FIRST_RUN and app.setup_pending
    assert app.models.state == "waiting" and app._local_model() is None
    assert app._should_open_dashboard_at_start() is True
    assert not os.path.exists(app.CONFIG_FILE), "nothing is saved before the user chooses"
    assert app._handle_control({"cmd": "status"})["setup_complete"] is False
    print("PASS  a first run loads and saves nothing until the welcome is done.")


def test_press_before_setup_points_to_the_welcome():
    app = _with_tray(import_app_with_mocks(config=None))
    with mock.patch.object(app, "_key_is_down", return_value=True), \
         mock.patch.object(app, "start_recording") as start:
        for k in app.HOTKEY:
            app._handle_press(k)
        assert not app.tray_icon.notify.called, "the notice waits for the release"
        for k in app.HOTKEY:
            app._handle_release(k)
    assert not start.called
    assert app.tray_icon.notify.call_args.args[1] == "Finish setting up Scribe"
    assert app._idle_tooltip(app.models.snapshot(), False, "ok", 0, pending=True) == \
        "Scribe - finish setup to start dictating"
    print("PASS  a press before setup says to finish it.")


def test_setup_done_applies_the_choices():
    app = import_app_with_mocks(config=None)
    import storage
    storage.save_config_changes({"model_size": "base.en", "hotkey": "Ctrl + Alt"})
    while not app.ui_queue.empty():
        app.ui_queue.get_nowait()
    app._handle_control({"cmd": "setup-done"})
    routed = []
    while not app.ui_queue.empty():
        item = app.ui_queue.get_nowait()
        if not isinstance(item, tuple):
            routed.append(item)
    assert routed == ["setup-done"], routed          # handed to the main thread
    app.complete_setup()
    assert not app.setup_pending and app.current_hotkey_name == "Ctrl + Alt"
    assert app.models.wait_ready(5) and app.models.snapshot()["name"] == "base.en"
    print("PASS  'setup-done' applies the saved choices and starts the model.")


def test_setup_done_without_saved_choices_is_ignored():
    app = import_app_with_mocks(config=None)
    app.complete_setup()
    assert app.setup_pending and app.models.state == "waiting"
    print("PASS  a stray 'setup-done' with nothing saved changes nothing.")


def test_welcome_that_cannot_open_falls_back_to_defaults():
    app = _with_tray(import_app_with_mocks(config=None))
    app.dashboard_proc = mock.MagicMock()
    app.dashboard_proc.poll.return_value = 2          # "WebView2 is missing"
    app.dashboard_welcome = True
    app._check_dashboard_started()
    import storage
    assert not app.setup_pending and os.path.exists(app.CONFIG_FILE)
    cfg = storage.load_config()[0]
    assert cfg["model_size"] == "small.en" and cfg["use_cloud"] is False
    assert cfg["check_updates"] is False, "on this PC: no daily GitHub check by default"
    assert app.models.wait_ready(5)
    app.models.join(5)
    titles = [c.args[1] for c in app.tray_icon.notify.call_args_list]
    assert titles[0] == "Setting up Scribe" and "Scribe is ready" in titles, titles
    print("PASS  no welcome (no WebView2) -> defaults, a notice, then 'ready'.")


def test_welcome_fallback_waits_for_a_slow_webview2_dialog():
    """The "WebView2 is missing" dialog stays up for as long as the user
    reads it - the dashboard only exits (code 2) after they answer. The
    first-run fallback must still happen then, not just at the 4-second
    check."""
    app = _with_tray(import_app_with_mocks(config=None))
    app.root = mock.MagicMock()
    app.dashboard_proc = mock.MagicMock()
    app.dashboard_proc.poll.side_effect = [None, None, 2]
    app.dashboard_welcome = True
    app._check_dashboard_started()                   # dialog still up
    assert app.setup_pending and app.root.after.call_count == 1
    app._check_dashboard_started()                   # ...still up
    assert app.setup_pending and app.root.after.call_count == 2
    app._check_dashboard_started()                   # answered: exit code 2
    assert not app.setup_pending, "no welcome -> Scribe sets itself up anyway"
    # A welcome the user simply closed (exit 0) is not a failure: keep waiting
    # for them, nothing is set up behind their back.
    app2 = import_app_with_mocks(config=None)
    app2.root = mock.MagicMock()
    app2.dashboard_proc = mock.MagicMock()
    app2.dashboard_proc.poll.return_value = 0
    app2.dashboard_welcome = True
    app2._check_dashboard_started()
    assert app2.setup_pending and app2.root.after.call_count == 0
    print("PASS  the no-WebView2 fallback waits for the dialog to be answered.")


def test_open_dashboard_shows_the_welcome_until_setup_is_done():
    app = import_app_with_mocks(config=None)
    app.root = mock.MagicMock()
    with mock.patch.object(app.subprocess, "Popen") as popen:
        app.open_dashboard()
    assert popen.call_args.args[0][-1] == "--welcome" and app.dashboard_welcome
    app.dashboard_proc = None
    app.setup_pending = False
    with mock.patch.object(app.subprocess, "Popen") as popen:
        app.open_dashboard()
    assert "--welcome" not in popen.call_args.args[0] and not app.dashboard_welcome
    print("PASS  the dashboard opens as the welcome until setup is done.")


def test_background_start_does_not_open_the_dashboard():
    app = import_app_with_mocks()
    app.SHOW_REQUESTED = False
    assert app._should_open_dashboard_at_start() is False
    app.SHOW_REQUESTED = True
    assert app._should_open_dashboard_at_start() is True
    print("PASS  sign-in starts quietly; --show opens the dashboard.")


def test_retry_model_command():
    app = import_app_with_mocks()
    with mock.patch.object(app.models, "retry") as retry:
        assert app._handle_control({"cmd": "retry-model"}) is None
    retry.assert_called_once_with()
    print("PASS  the welcome's Retry reaches the model manager.")


def test_key_comes_from_the_keystore_and_is_never_saved():
    app = import_app_with_mocks(config={"use_cloud": True, "sound_cues": False})
    with mock.patch.object(app.keystore, "get_key", return_value="gsk_from_vault"):
        app.load_config()
    assert app.GROQ_API_KEY == "gsk_from_vault"
    app.save_config()
    import storage
    assert storage.load_config()[0]["groq_api_key"] == "", \
        "save_config must never write the key back into config.json"
    print("PASS  the key comes from the keystore and never goes back to config.json.")


if __name__ == "__main__":
    test_first_run_waits_for_the_welcome()
    test_press_before_setup_points_to_the_welcome()
    test_setup_done_applies_the_choices()
    test_setup_done_without_saved_choices_is_ignored()
    test_welcome_that_cannot_open_falls_back_to_defaults()
    test_welcome_fallback_waits_for_a_slow_webview2_dialog()
    test_open_dashboard_shows_the_welcome_until_setup_is_done()
    test_background_start_does_not_open_the_dashboard()
    test_retry_model_command()
    test_key_comes_from_the_keystore_and_is_never_saved()
    print("\nAll first-run tests passed.")
