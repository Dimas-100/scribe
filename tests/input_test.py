"""
Standalone probe for the input controller - the one thread that owns all
hotkey logic. No real keys are sent or read: which keys are physically held
is simulated through app._key_is_down.

Run from the project root:   venv\\Scripts\\python tests\\input_test.py
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402

app = import_app_with_mocks()
K = app.keyboard.Key
HELD = set()          # the keys the simulated user is physically holding


def reset():
    app.recording, app.recording_started_at = False, None
    app._hold_consumed, app._missed_release_checks = False, 0
    app.pressed.clear()
    HELD.clear()
    app._key_is_down = lambda k: (k in HELD) if k in app.MODIFIER_KEYS else None


def press(k):
    HELD.add(app.normalize(k))
    app._handle_press(k)


def release(k):
    HELD.discard(app.normalize(k))
    app._handle_release(k)


def test_injected_keys_are_ignored():
    while not app.key_events.empty():
        app.key_events.get_nowait()
    with app._injecting():                # Scribe is sending keys right now
        app.on_press(K.ctrl_l, True)
        app.on_release(K.ctrl_l, True)
    assert app.key_events.empty(), "Scribe's own keystrokes must be ignored"
    app.on_press(K.ctrl_l)
    assert app.key_events.get_nowait() == ("press", K.ctrl_l)
    print("PASS  injected (synthetic) keys are ignored; real ones are queued.")


def test_stale_modifier_cannot_start_recording():
    reset()
    app.pressed.add(K.cmd)          # left behind by Win+L: never saw its key-up
    with mock.patch.object(app, "start_recording", return_value=True) as start:
        press(K.ctrl_l)             # Win is NOT physically down
    assert not start.called and app.recording is False
    print("PASS  a stale Win in `pressed` can't turn a Ctrl hold into a recording.")


def test_extra_key_cancels_and_holds_off_until_release():
    reset()
    with mock.patch.object(app, "start_recording", return_value=True) as start, \
         mock.patch.object(app, "process_audio") as job:
        press(K.ctrl_l)
        press(K.cmd)
        assert app.recording and start.call_count == 1
        press(K.right)                      # Ctrl+Win+Right = switch desktop
        assert app.recording is False and not job.called and app._hold_consumed
        app._handle_press(K.cmd)            # auto-repeat of the held Win key
        assert start.call_count == 1, "no restart while the combo is still held"
        release(K.right)
        release(K.cmd)
        press(K.cmd)                        # a genuine new press
        assert start.call_count == 2
    print("PASS  hotkey + another key cancels; no restart until the hotkey is released.")


def test_scratch_gesture_still_undoes():
    reset()
    app.last_output = "hello "
    with mock.patch.object(app, "start_recording", return_value=True), \
         mock.patch.object(app, "undo_last") as undo, \
         mock.patch.object(app.threading, "Thread") as thread:
        press(K.ctrl_l)
        press(K.cmd)
        app._handle_press(app.keyboard.KeyCode(vk=0x5A))
    assert app.recording is False
    assert thread.call_args.kwargs["target"] is undo
    app.last_output = ""
    print("PASS  holding the hotkey and tapping Z still scratches the last dictation.")


def test_missed_key_up_is_finished_not_lost():
    reset()
    with mock.patch.object(app, "start_recording", return_value=True), \
         mock.patch.object(app, "_finish_recording") as finish:
        press(K.ctrl_l)
        press(K.cmd)
        HELD.clear()                         # keys up, but no release event arrived
        app._handle_check()
        assert not finish.called, "one reading isn't enough"
        app._handle_check()
    assert finish.called
    print("PASS  a missed key-up is detected within ~2 s and the take is transcribed.")


def test_time_limit_transcribes_and_tells_user():
    reset()
    app.tray_icon, app._tray_ready = mock.MagicMock(), True
    app._notice_last.clear()
    with mock.patch.object(app, "start_recording", return_value=True), \
         mock.patch.object(app, "_finish_recording") as finish:
        press(K.ctrl_l)
        press(K.cmd)
        app.recording_started_at -= app.MAX_RECORDING_SECONDS + 1
        app._handle_check()
    assert finish.called and app._hold_consumed
    assert app.tray_icon.notify.call_args.args[1] == "Recording limit reached"
    print("PASS  at the 5-minute limit the take is transcribed (not discarded) and the user told.")


def test_controller_survives_a_handler_error():
    reset()
    with mock.patch.object(app, "_handle_press", side_effect=RuntimeError("boom")):
        app._process_key_event(("press", K.ctrl_l))      # must not raise
    print("PASS  a handler error is logged and the controller keeps running.")


# --- final-review regressions -------------------------------------------------

def test_chord_undo_gets_a_place_in_line():
    reset()
    app.last_output = "hello "
    seq_before = app._next_job_seq
    with mock.patch.object(app, "start_recording", return_value=True), \
         mock.patch.object(app.threading, "Thread") as thread:
        press(K.ctrl_l)
        press(K.cmd)
        app._handle_press(app.keyboard.KeyCode(vk=0x5A))
    kwargs = thread.call_args.kwargs
    assert kwargs["target"] is app.undo_last
    assert kwargs["args"] == (seq_before,) and kwargs["kwargs"] == {"from_chord": True}, kwargs
    assert app._next_job_seq == seq_before + 1
    app.last_output = ""
    print("PASS  Ctrl+Win+Z takes a place in line, after dictations still in flight.")


def test_hold_latch_released_when_stuck_hotkey_key_is_noticed():
    reset()
    app.HOTKEY = app.HOTKEY_CHOICES["Ctrl + Alt"]
    try:
        # After Ctrl+Alt+Del: a recording was cancelled by Del (latch set) and
        # the key-ups happened on the secure desktop, where we can't see them.
        app.pressed.update({K.ctrl, K.alt, K.delete})
        app._hold_consumed = True
        with mock.patch.object(app, "start_recording", return_value=True) as start:
            press(K.ctrl_l)            # Alt reads physically up -> stale
            press(K.alt_l)
        assert start.called, "the first real press after coming back must work"
        reset()
        app._hold_consumed = True     # the same, noticed by the watchdog instead
        app._handle_check()
        assert app._hold_consumed is False
    finally:
        app.HOTKEY = app.HOTKEY_CHOICES["Ctrl + Win"]
    print("PASS  a hold left over from the secure desktop is released, not stuck.")


def test_only_scribes_own_keystrokes_are_ignored():
    while not app.key_events.empty():
        app.key_events.get_nowait()
    app._injecting_until = 0.0
    app.on_press(K.ctrl_l, True)        # on-screen keyboard / AutoHotkey / remote tool
    assert app.key_events.get_nowait() == ("press", K.ctrl_l)
    with app._injecting():
        app.on_press(K.ctrl_l, True)    # Scribe's own Ctrl+V
    assert app.key_events.empty()
    print("PASS  only Scribe's own synthetic keys are ignored; other injectors still work.")

if __name__ == "__main__":
    test_injected_keys_are_ignored()
    test_stale_modifier_cannot_start_recording()
    test_extra_key_cancels_and_holds_off_until_release()
    test_scratch_gesture_still_undoes()
    test_missed_key_up_is_finished_not_lost()
    test_time_limit_transcribes_and_tells_user()
    test_controller_survives_a_handler_error()
    test_chord_undo_gets_a_place_in_line()
    test_hold_latch_released_when_stuck_hotkey_key_is_noticed()
    test_only_scribes_own_keystrokes_are_ignored()
    print("\nAll input tests passed.")
