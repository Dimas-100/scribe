r"""
Standalone probe for how app.py uses the background model manager: the
model loads after startup, the hotkey is refused (with a reason) while
nothing can transcribe, a loading model is waited for, size changes apply
live, and the tray tooltip shows progress.

Run from the project root:   venv\Scripts\python tests\model_flow_test.py
"""

import os
import sys
import threading
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402


def _snap(**fields):
    base = {"state": "ready", "name": "small.en", "done": 0, "total": 0, "error": None}
    base.update(fields)
    return base


def _with_tray(app):
    app.tray_icon = mock.MagicMock()
    app._tray_ready = True
    return app


def test_model_loads_in_the_background():
    app = import_app_with_mocks()
    assert app._local_model() is app._fake_local_model
    assert app.models.snapshot()["name"] == "small.en"
    assert app._status_snapshot()["model"]["state"] == "ready"
    print("PASS  the local model loads on the background thread.")


def test_cloud_only_has_no_local_model():
    app = import_app_with_mocks(config={"use_cloud": True, "groq_api_key": "gsk_x",
                                        "local_model": False, "sound_cues": False})
    assert app.models.state == "off" and app._local_model() is None
    assert app._dictation_blocker() is None
    import storage
    storage.save_config_changes({"use_cloud": False})
    app.reload_config()
    assert app.models.wait_ready(5) and app._local_model() is not None
    print("PASS  cloud-only loads nothing; turning the cloud off brings local back.")


def test_model_size_applies_live():
    app = import_app_with_mocks()
    import storage
    storage.save_config_changes({"model_size": "base.en"})
    app.reload_config()
    assert app.models.wait_ready(5)
    assert app.models.snapshot()["name"] == "base.en"
    print("PASS  a new model size applies without a restart.")


def test_transcription_waits_for_a_loading_model():
    app = import_app_with_mocks()
    gate = threading.Event()

    def slow_load(path):
        gate.wait(5)
        return app._fake_local_model
    app.models = app.model_manager.ModelManager(
        find_cached=lambda n: "x", load=slow_load, on_change=app._on_model_change)
    app.models.request("small.en")
    threading.Timer(0.2, gate.set).start()
    assert app.transcribe_audio(np.zeros(16000, dtype=np.float32)) == "local stub said hello"
    print("PASS  a dictation recorded while the model loads waits for it.")


def test_ready_notice_only_after_a_wait_notice():
    app = _with_tray(import_app_with_mocks())
    app._model_wait_told = True
    app._on_model_change(_snap())
    assert app.tray_icon.notify.call_args.args[1] == "Scribe is ready"
    assert app._model_wait_told is False
    app.tray_icon.notify.reset_mock()
    app._on_model_change(_snap())
    assert not app.tray_icon.notify.called, "no 'ready' pop-up on a normal start"
    print("PASS  'Scribe is ready' only follows a 'please wait'.")


def test_idle_tooltip():
    app = import_app_with_mocks()
    tip = app._idle_tooltip
    assert tip(_snap(state="downloading", done=1, total=4), True, "ok", 0) == \
        "Scribe - downloading speech model (25%)"
    assert tip(_snap(state="loading"), False, "ok", 0) == "Scribe - loading speech model..."
    assert tip(_snap(state="loading"), True, "ok", 0) == "Scribe - ready"
    assert tip(_snap(state="failed", error="x"), False, "ok", 0) == \
        "Scribe - couldn't download the speech model"
    assert tip(_snap(), True, "warn", 0.78) == "Scribe - ready · 78% of daily Groq quota used"
    for s in ("downloading", "loading", "failed", "ready"):
        assert len(tip(_snap(state=s, done=1, total=3, error="e"), False, "danger", 0.99)) < 64
    print("PASS  the tray tooltip says what is still getting ready.")


def _press_release(app, *, other=None):
    """Press the hotkey (plus `other` while held), then release it all."""
    with mock.patch.object(app, "_key_is_down", return_value=True):
        for k in app.HOTKEY:
            app._handle_press(k)
        if other is not None:
            app._handle_press(other)
            app._handle_release(other)
        for k in app.HOTKEY:
            app._handle_release(k)


def test_blocked_press_notice_shows_on_release():
    app = _with_tray(import_app_with_mocks())
    with mock.patch.object(app, "_local_model", return_value=None), \
         mock.patch.object(app.models, "snapshot",
                           return_value=_snap(state="downloading", done=50, total=200)), \
         mock.patch.object(app, "start_recording") as start:
        _press_release(app)
    assert not start.called and app.recording is False
    msg, title = app.tray_icon.notify.call_args.args
    assert title == "Scribe is still getting ready" and "25%" in msg, (title, msg)
    assert app._model_wait_told is True
    print("PASS  a blocked press explains itself when the hotkey is released.")


def test_a_blocked_notice_is_rechecked_on_release():
    app = _with_tray(import_app_with_mocks())
    downloading = _snap(state="downloading", done=50, total=200)
    with mock.patch.object(app.models, "snapshot", return_value=downloading), \
         mock.patch.object(app, "start_recording", return_value=True):
        with mock.patch.object(app, "_local_model", return_value=None), \
             mock.patch.object(app, "_key_is_down", return_value=True):
            for k in app.HOTKEY:
                app._handle_press(k)           # blocked: the model is downloading
        # The download finishes while the keys are still held.
        for k in app.HOTKEY:
            app._handle_release(k)
    titles = [c.args[1] for c in app.tray_icon.notify.call_args_list]
    assert titles == ["Scribe is ready"], titles           # not a stale "still getting ready"
    assert "speak again" in app.tray_icon.notify.call_args.args[0]
    print("PASS  if the model got ready while the key was held: 'Scribe is ready - speak again'.")


def test_a_hold_that_ended_unseen_drops_its_notice():
    app = _with_tray(import_app_with_mocks())
    downloading = _snap(state="downloading", done=50, total=200)
    with mock.patch.object(app, "_local_model", return_value=None), \
         mock.patch.object(app.models, "snapshot", return_value=downloading), \
         mock.patch.object(app, "_key_is_down", return_value=True):
        for k in app.HOTKEY:
            app._handle_press(k)               # blocked
    with mock.patch.object(app, "_key_is_down", return_value=False):
        app._handle_check()                    # the keys came up where we couldn't see
    for k in app.HOTKEY:
        app.pressed.discard(k)
    with mock.patch.object(app, "start_recording", return_value=True), \
         mock.patch.object(app, "_finish_recording"):
        _press_release(app)                    # later: a normal dictation
    titles = [c.args[1] for c in app.tray_icon.notify.call_args_list]
    assert "Scribe is still getting ready" not in titles, titles
    print("PASS  a blocked hold that ended unseen never explains itself later.")


def test_a_windows_shortcut_during_the_download_is_silent():
    from pynput import keyboard
    app = _with_tray(import_app_with_mocks())
    with mock.patch.object(app, "_local_model", return_value=None), \
         mock.patch.object(app.models, "snapshot", return_value=_snap(state="downloading")):
        _press_release(app, other=keyboard.Key.right)      # Ctrl + Win + right: switch desktops
    assert not app.tray_icon.notify.called
    print("PASS  Ctrl + Win + arrow during a download pops up nothing.")


def test_blocker_reasons():
    app = import_app_with_mocks()
    with mock.patch.object(app, "_local_model", return_value=None):
        with mock.patch.object(app.models, "snapshot", return_value=_snap(state="loading")):
            assert app._dictation_blocker() is None, "a loading model is seconds away"
        with mock.patch.object(app.models, "snapshot",
                               return_value=_snap(state="failed", error="Your disk is full.")):
            key, title, msg, _cd = app._dictation_blocker()
            assert key == "model_failed" and title == "Couldn't download the speech model"
            assert "disk is full" in msg
        with mock.patch.object(app.models, "snapshot",
                               return_value=_snap(state="failed", error="ImportError: x",
                                                  failed_stage="load")):
            assert app._dictation_blocker()[1] == "Couldn't load the speech model"
    print("PASS  the blocker names the reason (or none while loading).")


def test_transcription_during_a_download_says_so():
    app = import_app_with_mocks()
    with mock.patch.object(app, "_local_model", return_value=None), \
         mock.patch.object(app.models, "snapshot",
                           return_value=_snap(state="downloading", done=43, total=100)), \
         mock.patch.object(app.models, "wait_loaded", return_value=False):
        try:
            app.transcribe_audio(np.zeros(16000, dtype=np.float32))
        except app.TranscriptionFailed as exc:
            assert "still downloading" in str(exc) and "43%" in str(exc), str(exc)
        else:
            raise AssertionError("expected TranscriptionFailed")
    print("PASS  a failed transcription during a download says why.")


if __name__ == "__main__":
    test_model_loads_in_the_background()
    test_cloud_only_has_no_local_model()
    test_model_size_applies_live()
    test_transcription_waits_for_a_loading_model()
    test_ready_notice_only_after_a_wait_notice()
    test_idle_tooltip()
    test_blocked_press_notice_shows_on_release()
    test_a_blocked_notice_is_rechecked_on_release()
    test_a_hold_that_ended_unseen_drops_its_notice()
    test_a_windows_shortcut_during_the_download_is_silent()
    test_blocker_reasons()
    test_transcription_during_a_download_says_so()
    print("\nAll model flow tests passed.")
