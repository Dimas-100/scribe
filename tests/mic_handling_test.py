r"""
Standalone probe for microphone handling: no mic, unplugged mics, a changed
Windows default, and the start cue. All audio is mocked (tests/harness.py).

Run from the project root:   venv\Scripts\python tests\mic_handling_test.py
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402


def hold_all_keys(app):
    """The input controller asks which keys are physically held; these tests
    simulate a user holding the hotkey, so report every key as down."""
    app._key_is_down = lambda k: True


def no_mic_stream():
    return mock.MagicMock(side_effect=Exception("Error querying device -1"))


def test_no_microphone_at_startup_is_not_fatal():
    app = import_app_with_mocks(input_stream=no_mic_stream())
    assert app.stream is None and app._mic_status == "none"
    assert ("No microphone found",
            "Plug one in — Scribe will pick it up automatically.") in app._pending_notices
    print("PASS  no microphone at startup: Scribe starts and says so.")


def test_press_without_mic_records_nothing_and_no_cue():
    app = import_app_with_mocks(input_stream=no_mic_stream())
    app._notice_last.clear()
    hold_all_keys(app)
    with mock.patch.object(app, "play_cue") as cue, \
         mock.patch.object(app.devices, "refresh_portaudio"):
        for k in app.HOTKEY:
            app._handle_press(k)
    assert app.recording is False, "no mic -> not recording"
    assert not cue.called, "the start cue must not play without a mic"
    print("PASS  hotkey without a mic: no recording, no start cue.")


def test_unplugged_between_dictations_recovers_on_next_press():
    app = import_app_with_mocks()
    first = mock.MagicMock()
    first.start.side_effect = Exception("device unavailable")
    app.stream = first

    def live_stream(**kw):          # a working mic: audio flows once it's started
        import numpy as np
        s = mock.MagicMock()
        s.start.side_effect = lambda: kw["callback"](np.zeros((160, 1), np.float32), 160, None, None)
        return s
    with mock.patch.object(app, "play_cue") as cue, \
         mock.patch.object(app.sd, "InputStream", side_effect=live_stream), \
         mock.patch.object(app.devices, "refresh_portaudio") as refresh:
        started = app.start_recording()
        assert started is True and refresh.called
        assert app.stream is not first and cue.called
        app.stop_recording()
    print("PASS  a mic that vanished is refreshed and reopened on the next press.")


def test_chosen_mic_missing_falls_back():
    app = import_app_with_mocks(config={"mic_device": "USB Mic", "sound_cues": False})
    assert app._mic_status == "fallback"
    assert any(t == "Microphone unavailable" for t, _ in app._pending_notices)
    print("PASS  a chosen mic that isn't connected falls back and says so.")


def test_watcher_reopens_on_default_change_while_idle():
    app = import_app_with_mocks()
    app.tray_icon = mock.MagicMock()
    app._tray_ready = True
    app._notice_last.clear()
    app.recording = False
    with mock.patch.object(app.devices, "default_capture_id", return_value="{new}"), \
         mock.patch.object(app.devices, "input_device_count", return_value=3), \
         mock.patch.object(app.devices, "refresh_portaudio") as refresh, \
         mock.patch.object(app.devices, "default_input_name", return_value="Headset (AirPods)"):
        last = app._device_watch_tick(("{old}", 3))
    assert last == ("{new}", 3) and refresh.called and app.mic_dirty is False
    app.tray_icon.notify.assert_called_with("Now listening with Headset (AirPods).",
                                            "Microphone changed")
    print("PASS  a new Windows default mic is picked up while idle, with a notice.")


def test_watcher_waits_while_recording():
    app = import_app_with_mocks()
    app.recording = True
    with mock.patch.object(app.devices, "default_capture_id", return_value="{new}"), \
         mock.patch.object(app.devices, "input_device_count", return_value=3), \
         mock.patch.object(app.devices, "refresh_portaudio") as refresh:
        app._device_watch_tick(("{old}", 3))
    assert app.mic_dirty is True and not refresh.called
    app.recording = False
    print("PASS  the watcher never reopens the mic mid-recording.")


def test_failed_start_is_not_retried_by_key_repeat():
    app = import_app_with_mocks(input_stream=no_mic_stream())
    hold_all_keys(app)
    with mock.patch.object(app, "play_cue"), \
         mock.patch.object(app.devices, "refresh_portaudio") as refresh:
        for k in app.HOTKEY:
            app._handle_press(k)
        first = refresh.call_count
        for _ in range(5):                     # Windows auto-repeat while held
            app._handle_press(next(iter(app.HOTKEY)))
        assert refresh.call_count == first, "held keys must not retry every repeat"
        app._handle_release(next(iter(app.HOTKEY)))  # let go...
        for k in app.HOTKEY:                    # ...and press again: tries again
            app._handle_press(k)
        assert refresh.call_count > first
    print("PASS  a failed start is tried once per press, not on every key repeat.")


def test_cues_and_device_names_only_under_mic_lock():
    app = import_app_with_mocks()
    seen = []

    def cue(kind):
        seen.append(("cue", kind, app.mic_lock.locked()))

    def name():
        seen.append(("name", None, app.mic_lock.locked()))
        return "Headset"
    app.tray_icon, app._tray_ready = mock.MagicMock(), True
    with mock.patch.object(app, "play_cue", side_effect=cue), \
         mock.patch.object(app.devices, "refresh_portaudio"), \
         mock.patch.object(app.devices, "default_input_name", side_effect=name), \
         mock.patch.object(app.devices, "default_capture_id", return_value="{new}"), \
         mock.patch.object(app.devices, "input_device_count", return_value=1):
        app.start_recording()
        app.stop_recording()
        app._device_watch_tick(("{old}", 1))
    assert seen and all(locked for _, _, locked in seen), seen
    print("PASS  sound cues and device lookups never race a device refresh.")

def test_no_mic_press_renotifies_quickly():
    app = import_app_with_mocks(input_stream=no_mic_stream())
    app.tray_icon, app._tray_ready = mock.MagicMock(), True
    app._notice_last["no_mic"] = app.time.monotonic() - 16
    app._announce_mic("none", "none", False, None, press=True)
    assert app.tray_icon.notify.called
    print("PASS  pressing the hotkey without a mic re-explains after 15 s.")

def test_the_mic_opens_raw_first():
    """The mic is opened for its RAW audio (bypassing laptop noise suppression
    such as Dolby's, which chops speech so badly ElevenLabs misses most of
    it) - and the usual way when the mic has no raw mode or no WASAPI entry."""
    app = import_app_with_mocks()
    calls, refuse_raw = [], []

    def fake_stream(**kw):
        calls.append(kw)
        if kw.get("extra_settings") is not None and refuse_raw:
            raise app.sd.PortAudioError("raw mode not supported")
        return mock.MagicMock()

    with mock.patch.object(app.devices, "raw_twin", return_value=14), \
         mock.patch.object(app.devices, "raw_settings", return_value="RAW"), \
         mock.patch.object(app.sd, "InputStream", side_effect=fake_stream):
        app.make_stream(3)
        assert len(calls) == 1 and calls[0]["device"] == 14 and calls[0]["extra_settings"] == "RAW", calls
        assert calls[0]["samplerate"] == app.SAMPLE_RATE and calls[0]["channels"] == 1
        refuse_raw.append(True)
        calls.clear()
        app.make_stream(3)                       # raw refused -> the usual route
        assert [c["device"] for c in calls] == [14, 3], calls
        assert calls[1].get("extra_settings") is None
    with mock.patch.object(app.devices, "raw_twin", return_value=None), \
         mock.patch.object(app.sd, "InputStream", side_effect=fake_stream):
        calls.clear()
        app.make_stream(None)                    # no WASAPI twin -> the usual route
        assert [c["device"] for c in calls] == [None], calls
    print("PASS  the mic opens in raw mode, and the usual way when raw isn't offered.")


def test_speak_now_waits_for_the_audio():
    """A Bluetooth headset (AirPods) delivers no audio for ~1 s after the mic
    starts - it switches to call mode - and whatever is said meanwhile is
    lost (measured). So "speak now" - the pill, the start sound - waits for
    the audio itself. A built-in / USB mic delivers at once: no change."""
    import time
    import numpy as np
    app = import_app_with_mocks(config={"sound_cues": True})
    statuses, cues = [], []
    chunk = np.zeros((160, 1), np.float32)

    def wait_for(cond):
        deadline = time.time() + 2
        while not cond() and time.time() < deadline:
            time.sleep(0.01)

    with mock.patch.object(app, "update_status", side_effect=statuses.append), \
         mock.patch.object(app, "play_cue", side_effect=cues.append), \
         mock.patch.object(app, "LISTEN_WAIT_INLINE", 0.05):
        # A slow (Bluetooth) mic: nothing has arrived when recording starts.
        app.recording = True
        assert app.start_recording()
        app._refresh_status()                          # e.g. an earlier job finishing
        assert "recording" not in statuses and "start" not in cues, (statuses, cues)
        assert app._status_snapshot()["activity"] == "connecting"
        app.audio_callback(chunk, 160, None, None)     # the audio arrives
        wait_for(lambda: "recording" in statuses)
        assert statuses[-1] == "recording" and cues.count("start") == 1, (statuses, cues)
        assert app._status_snapshot()["activity"] == "recording"
        app.stop_recording()
        app.recording = False

        # A quick tap that's over before any audio arrives: no "speak now".
        statuses.clear(); cues.clear()
        app.recording = True
        assert app.start_recording()
        app.stop_recording()
        app.recording = False
        app.audio_callback(chunk, 160, None, None)
        time.sleep(0.2)
        assert "recording" not in statuses and "start" not in cues, (statuses, cues)

        # A built-in / USB mic: audio flows as it starts -> "speak now" at once.
        statuses.clear(); cues.clear()
        app.stream.start.side_effect = lambda: app.audio_callback(chunk, 160, None, None)
        app.recording = True
        assert app.start_recording()
        assert statuses and statuses[-1] == "recording" and cues == ["start"], (statuses, cues)
        app.stop_recording()
        app.recording = False
    print("PASS  'speak now' waits until the mic's audio really arrives (Bluetooth).")


if __name__ == "__main__":
    test_speak_now_waits_for_the_audio()
    test_the_mic_opens_raw_first()
    test_no_microphone_at_startup_is_not_fatal()
    test_press_without_mic_records_nothing_and_no_cue()
    test_unplugged_between_dictations_recovers_on_next_press()
    test_chosen_mic_missing_falls_back()
    test_watcher_reopens_on_default_change_while_idle()
    test_watcher_waits_while_recording()
    test_failed_start_is_not_retried_by_key_repeat()
    test_cues_and_device_names_only_under_mic_lock()
    test_no_mic_press_renotifies_quickly()
    print("\nAll microphone-handling tests passed.")
