"""
Standalone probe for how Scribe reports status and delivers dictations:
status on the main thread, spoken order, waiting for modifiers, verified
focus (with a clipboard fallback), history credit, undo targeting and voice
commands. Transcription, focus and keystrokes are all mocked - nothing is
typed and no window is touched.

Run from the project root:   venv\\Scripts\\python tests\\delivery_test.py
"""

import os
import sys
import threading
import time
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402

app = import_app_with_mocks()


def drain(q):
    """Everything currently queued on `q`, removing it."""
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# -----------------------------------------------------------------------------
#  Status - applied only on the main thread; reflects ALL activity.
# -----------------------------------------------------------------------------

def test_update_status_only_queues():
    app.tray_icon = mock.MagicMock()
    drain(app.ui_queue)
    with mock.patch.object(app, "set_state") as set_state:
        app.update_status("recording")
    assert not set_state.called, "tray must only be touched on the main thread"
    assert ("status", "recording") in drain(app.ui_queue)
    print("PASS  update_status only queues; the main thread applies it.")


def test_refresh_status_reflects_all_activity():
    drain(app.ui_queue)
    app.recording, app._in_flight = True, 1
    app._refresh_status()                 # the mic hasn't delivered audio yet
    app._audio_flowing.set()              # ...now it has
    app._refresh_status()
    app.recording = False
    app._refresh_status()
    app._in_flight = 0
    app._refresh_status()
    app._audio_flowing.clear()
    assert [s for _, s in drain(app.ui_queue)] == ["transcribing", "recording", "transcribing", "idle"]
    print("PASS  status = recording (once audio flows) > transcribing > idle.")


def test_finishing_job_keeps_recording_status():
    drain(app.ui_queue)
    app.recording, app._in_flight = True, 0
    app._audio_flowing.set()                                   # the next take is recording
    app.process_audio(np.zeros(100, dtype=np.float32), None)   # too short: skipped
    assert drain(app.ui_queue)[-1] == ("status", "recording")
    app.recording = False
    app._audio_flowing.clear()
    print("PASS  a finishing dictation never shows idle while the next one records.")


# -----------------------------------------------------------------------------
#  Delivery - spoken order, modifiers up, verified focus, clipboard fallback.
# -----------------------------------------------------------------------------

def fresh_turns():
    app._next_turn = 0
    app._finished_seqs.clear()


def test_jobs_deliver_in_spoken_order():
    fresh_turns()
    delivered, slow = [], threading.Event()

    def transcribe(audio):
        if audio[0] == 1.0:
            slow.wait(5)
            return "first"
        return "second"
    with mock.patch.object(app, "_use_speculative_or_transcribe", side_effect=transcribe), \
         mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "deliver", side_effect=lambda t, **k: delivered.append(t.strip())):
        a = np.ones(16000, dtype=np.float32)
        b = np.full(16000, 0.5, dtype=np.float32)
        t0 = threading.Thread(target=app.process_audio, args=(a, 1, None, None, 0))
        t1 = threading.Thread(target=app.process_audio, args=(b, 1, None, None, 1))
        t0.start()
        t1.start()
        time.sleep(0.3)
        assert delivered == [], "the second must wait for the first"
        slow.set()
        t0.join(5)
        t1.join(5)
    assert delivered == ["First", "Second"], delivered
    print("PASS  overlapping dictations are delivered in the order they were spoken.")


def test_skipped_job_does_not_block_the_next():
    fresh_turns()
    delivered = []
    with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="next"), \
         mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "deliver", side_effect=lambda t, **k: delivered.append(t)):
        app.process_audio(np.zeros(10, dtype=np.float32), 1, None, None, 0)   # too short
        t = threading.Thread(target=app.process_audio,
                             args=(np.ones(16000, dtype=np.float32), 1, None, None, 1))
        t.start()
        t.join(3)
    assert delivered and not t.is_alive()
    print("PASS  a skipped (too short) dictation never holds up the next one.")


def test_waits_until_modifiers_are_released():
    fresh_turns()
    states, order = iter([True, True, True, False]), []

    def modifier_check():
        order.append("check")
        return next(states, False)
    with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="text"), \
         mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "_any_modifier_down", side_effect=modifier_check), \
         mock.patch.object(app, "deliver", side_effect=lambda t, **k: order.append("deliver")):
        app.process_audio(np.ones(16000, dtype=np.float32), 1, None, None, 0)
    assert order.index("deliver") > 3, order
    print("PASS  delivery waits until Ctrl/Win/Alt are released (no Win+V).")


def test_unavailable_window_goes_to_clipboard_with_notice():
    fresh_turns()
    app.tray_icon, app._tray_ready = mock.MagicMock(), True
    app._notice_last.clear()
    with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="keep me"), \
         mock.patch.object(app, "restore_target_window", return_value=False), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "deliver") as deliver, \
         mock.patch.object(app, "_copy_for_user") as copy:
        app.process_audio(np.ones(16000, dtype=np.float32), 1, "Notepad", "notepad.exe", 0)
    assert not deliver.called and copy.call_args.args[0].strip() == "Keep me"
    assert app.tray_icon.notify.call_args.args[1] == "Couldn't paste your dictation"
    assert "Notepad" in app.tray_icon.notify.call_args.args[0]
    assert app.last_output == "", "undo must not target a window we never typed into"
    print("PASS  an unavailable window: text goes to the clipboard, user told, undo not armed.")


def test_history_credits_the_right_app():
    fresh_turns()
    app.target_app_name = "Some Later App"
    with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="hello"), \
         mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "deliver"):
        app.process_audio(np.ones(16000, dtype=np.float32), 1, "Code", "code.exe", 0)
    last = app.storage.read_jsonl(app.LOG_FILE)[-1]
    assert (last["app_name"], last["app_exe"]) == ("Code", "code.exe"), last
    print("PASS  history is credited to the app the dictation was for.")


def test_restore_target_window_rejects_missing_windows():
    assert app.restore_target_window(None) is False
    assert app.restore_target_window(0x7FFF0000) is False      # not a window
    print("PASS  focus restore refuses a missing/closed window.")


# -----------------------------------------------------------------------------
#  Undo, voice commands, fillers.
# -----------------------------------------------------------------------------

def arm(text, hwnd=7, logged=True):
    with app.undo_lock:
        app.last_output, app.last_output_hwnd, app.last_output_logged = text, hwnd, logged
        app.last_output_app = ("Code", "code.exe")


def test_undo_refuses_when_its_window_is_gone():
    arm("hello ")
    app.tray_icon, app._tray_ready = mock.MagicMock(), True
    app._notice_last.clear()
    with mock.patch.object(app, "restore_target_window", return_value=False), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "kbd") as kbd, \
         mock.patch.object(app, "remove_last_log_entry") as drop:
        app.undo_last()
    assert not kbd.press.called and not drop.called
    assert app.tray_icon.notify.call_args.args[1] == "Couldn't undo"
    print("PASS  undo never backspaces into a different window.")


def test_undoing_a_line_break_keeps_history():
    arm("\n", logged=False)
    with mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "kbd"), \
         mock.patch.object(app, "remove_last_log_entry") as drop:
        app.undo_last()
    assert not drop.called
    print("PASS  undoing a voice-command line break doesn't delete older history.")


def test_voice_command_after_a_filler():
    fresh_turns()
    with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="Um, scratch that."), \
         mock.patch.object(app, "handle_whole_utterance_action") as act:
        app.process_audio(np.ones(16000, dtype=np.float32), 1, None, None, 0)
    act.assert_called_once_with("scratch that")
    print("PASS  \"Um, scratch that.\" runs the command instead of being typed.")


def test_fillers_spare_real_words():
    out = app.remove_fillers("Uh-oh, um, the UH build said mm-hmm.")
    assert "Uh-oh" in out and "UH" in out and "mm-hmm" in out and "um," not in out, out
    print("PASS  filler removal keeps Uh-oh, Mm-hmm and acronyms like UH.")


def test_history_write_failure_is_reported():
    app.tray_icon, app._tray_ready = mock.MagicMock(), True
    app._notice_last.clear()
    with mock.patch.object(app.storage, "rewrite_lines", side_effect=OSError("locked")):
        app.remove_last_log_entry()
    assert app.tray_icon.notify.call_args.args[1] == "Couldn't update your history"
    print("PASS  a failed history update is reported, not swallowed.")

# --- final-review regressions -------------------------------------------------

def test_undo_waits_for_the_dictation_ahead_of_it():
    fresh_turns()
    arm("previous ")
    backspaces = []
    with mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "kbd") as kbd, \
         mock.patch.object(app, "remove_last_log_entry"):
        kbd.press.side_effect = lambda key: backspaces.append(key)
        t = threading.Thread(target=app.undo_last, args=(1,))
        t.start()
        time.sleep(0.2)
        assert backspaces == [], "undo must wait for dictation #0 to land"
        arm("just said ")               # dictation #0 lands...
        app._finish_turn(0)             # ...and finishes its turn
        t.join(3)
    assert len(backspaces) == len("just said "), len(backspaces)
    print("PASS  undo waits for the dictation ahead of it, then removes THAT one.")


def test_spoken_undo_waits_for_keys_without_faking_releases():
    arm("hello ")
    app.UNDO_MODIFIER_WAIT = 0.1
    held_until = time.monotonic() + 0.4
    with mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "_any_modifier_down", side_effect=lambda: time.monotonic() < held_until), \
         mock.patch.object(app, "kbd") as kbd, \
         mock.patch.object(app, "remove_last_log_entry"):
        app.undo_last()                  # from "scratch that" (not the chord)
    released = [c.args[0] for c in kbd.release.call_args_list]
    assert app.keyboard.Key.ctrl not in released and app.keyboard.Key.cmd not in released
    assert time.monotonic() >= held_until - 0.05
    arm("hello ")
    with mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "_any_modifier_down", return_value=True), \
         mock.patch.object(app, "kbd") as kbd, \
         mock.patch.object(app, "remove_last_log_entry"):
        app.undo_last(from_chord=True)   # the Ctrl+Win+Z chord, still held
    released = [c.args[0] for c in kbd.release.call_args_list]
    assert app.keyboard.Key.ctrl in released
    app.UNDO_MODIFIER_WAIT = 3.0
    print("PASS  spoken undo waits for your keys; only the chord releases them virtually.")


def test_modifiers_pressed_just_before_paste_are_waited_out():
    fresh_turns()
    readings = iter([False, True, True, False])      # clear, then pressed at the last moment
    order = []
    with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="text"), \
         mock.patch.object(app, "restore_target_window", side_effect=lambda h: order.append("focus") or True), \
         mock.patch.object(app, "_any_modifier_down", side_effect=lambda: next(readings, False)), \
         mock.patch.object(app, "deliver", side_effect=lambda t, **k: order.append("deliver")):
        app.process_audio(np.ones(16000, dtype=np.float32), 1, None, None, 0)
    assert order == ["focus", "deliver"], order
    print("PASS  a modifier pressed just before the paste is waited out, too.")


def test_keys_held_too_long_means_clipboard_not_win_v():
    fresh_turns()
    app.tray_icon, app._tray_ready = mock.MagicMock(), True
    app._notice_last.clear()
    app.MODIFIER_WAIT_MAX = 0.1
    try:
        with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="text"), \
             mock.patch.object(app, "restore_target_window", return_value=True), \
             mock.patch.object(app, "_any_modifier_down", return_value=True), \
             mock.patch.object(app, "deliver") as deliver, \
             mock.patch.object(app, "_copy_for_user", return_value=True) as copy:
            app.process_audio(np.ones(16000, dtype=np.float32), 1, "Code", "code.exe", 0)
    finally:
        app.MODIFIER_WAIT_MAX = app.MAX_RECORDING_SECONDS + 10
    assert not deliver.called and copy.called
    assert app.tray_icon.notify.call_args.args[1] == "Couldn't paste your dictation"
    print("PASS  keys held past the wait: clipboard + notice, never a paste with Win held.")


def test_fallback_is_honest_and_skips_line_breaks():
    fresh_turns()
    app.tray_icon, app._tray_ready = mock.MagicMock(), True
    app._notice_last.clear()
    with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="new line"), \
         mock.patch.object(app, "restore_target_window", return_value=False), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "_copy_for_user") as copy:
        app.process_audio(np.ones(16000, dtype=np.float32), 1, None, None, 0)
    assert not copy.called and not app.tray_icon.notify.called, "a lone line break isn't worth a notice"
    fresh_turns()
    with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="keep me"), \
         mock.patch.object(app, "restore_target_window", return_value=False), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "_copy_for_user", return_value=False):
        app.process_audio(np.ones(16000, dtype=np.float32), 1, None, None, 0)
    msg = app.tray_icon.notify.call_args.args[0]
    assert "clipboard" not in msg and "history" in msg, msg
    print("PASS  the fallback notice tells the truth, and line breaks don't trigger it.")


def test_remote_desktop_targets_get_a_normal_clipboard_borrow():
    fresh_turns()
    with mock.patch.object(app, "_use_speculative_or_transcribe", return_value="text"), \
         mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "deliver") as deliver:
        app.process_audio(np.ones(16000, dtype=np.float32), 1, "Remote Desktop",
                          "C:\\Windows\\System32\\mstsc.exe", 0)
    assert deliver.call_args.kwargs.get("private") is False, deliver.call_args
    print("PASS  Remote Desktop / VM windows get a normal clipboard borrow they can sync.")

if __name__ == "__main__":
    test_undo_refuses_when_its_window_is_gone()
    test_undoing_a_line_break_keeps_history()
    test_voice_command_after_a_filler()
    test_fillers_spare_real_words()
    test_update_status_only_queues()
    test_refresh_status_reflects_all_activity()
    test_finishing_job_keeps_recording_status()
    test_jobs_deliver_in_spoken_order()
    test_skipped_job_does_not_block_the_next()
    test_waits_until_modifiers_are_released()
    test_unavailable_window_goes_to_clipboard_with_notice()
    test_history_credits_the_right_app()
    test_restore_target_window_rejects_missing_windows()
    test_history_write_failure_is_reported()
    test_undo_waits_for_the_dictation_ahead_of_it()
    test_spoken_undo_waits_for_keys_without_faking_releases()
    test_modifiers_pressed_just_before_paste_are_waited_out()
    test_keys_held_too_long_means_clipboard_not_win_v()
    test_fallback_is_honest_and_skips_line_breaks()
    test_remote_desktop_targets_get_a_normal_clipboard_borrow()
    print("\nAll delivery tests passed.")
