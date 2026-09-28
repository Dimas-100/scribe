r"""
Standalone probe for the status visuals inside app.py: the indicator (the
capsule that appears while you dictate - drawn by indicator.py) and the tray
icon (brand.py), driven through the app's own code on a real Tk root.

Run from the project root:   venv\Scripts\python tests\overlay_test.py

Safe: tests/harness.py (temp data folder, mocked model and mic). The
indicator's window is a stand-in that records what it was asked to show -
nothing appears on screen - and the tray icon is a mock.
"""

import os
import sys
import time
import tkinter as tk
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402

app = import_app_with_mocks()
app.root = tk.Tk()
app.root.withdraw()


class FakeWindow:
    """Stands in for indicator.LayeredWindow: records every show/hide."""

    def __init__(self, *_size):
        self.shows, self.hides, self.visible = [], 0, False

    def show(self, image, x, y, alpha):
        self.shows.append((x, y, alpha, image.size))
        self.visible = True

    def hide(self):
        self.hides += 1
        self.visible = False

    def close(self):
        self.visible = False


def pump(seconds):
    """Run Tk's loop for `seconds` (the indicator's frames are after() calls)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.root.update()
        time.sleep(0.002)


def fresh():
    """A new stand-in window and a parked indicator."""
    if app.overlay_anim_job is not None:
        app.root.after_cancel(app.overlay_anim_job)
        app.overlay_anim_job = None
    app.indicator_motion = app.indicator.Motion()
    app.overlay_state = "idle"
    app.quota_state = "ok"
    app._indicator_failures = 0
    app._indicator_logged_at = -1e9
    app.overlay = FakeWindow()
    return app.overlay


def test_it_opens_follows_and_closes():
    win = fresh()
    with mock.patch.object(app.indicator, "fine_timer") as timer:
        app.update_overlay("recording")
        pump(0.45)
        assert win.visible and win.shows[-1][2] > 0.99, "open and fully visible"
        assert len(win.shows) > 15, f"~60 frames a second ({len(win.shows)} in 0.45 s)"
        app.update_overlay("transcribing")
        pump(0.3)
        assert win.visible
        app.update_overlay("idle")
        pump(0.6)
        assert not win.visible and win.hides >= 1, "folded away"
        assert app.overlay_anim_job is None, "and nothing runs while hidden"
        on = [c.args[0] for c in timer.call_args_list]
        assert on == [True, False], f"fine timer only while animating: {on}"
    print("PASS  the indicator opens on 'recording', stays while working, folds away on 'idle'.")


def test_it_opens_on_your_monitor():
    win = fresh()
    with mock.patch.object(app.indicator, "work_area", return_value=(1920, 0, 3840, 1040)), \
         mock.patch.object(app.indicator, "fine_timer"):
        app.update_overlay("recording")
        pump(0.1)
        app.update_overlay("idle")
        pump(0.6)
    x, y = win.shows[0][:2]
    assert (x, y) == app.indicator.place((1920, 0, 3840, 1040), app.UI_SCALE), (x, y)
    print("PASS  it opens on the monitor you're working on, just above the taskbar.")


def test_a_bad_frame_heals_and_is_logged():
    win = fresh()
    before = os.path.getsize(app.ERROR_LOG) if os.path.exists(app.ERROR_LOG) else 0
    real = app.indicator.render
    calls = {"n": 0}

    def flaky(frame, scale):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("injected draw failure (test)")
        return real(frame, scale)
    with mock.patch.object(app.indicator, "render", side_effect=flaky), \
         mock.patch.object(app.indicator, "fine_timer"):
        app.update_overlay("recording")
        pump(0.4)
        shown_after = len(win.shows)
        app.update_overlay("idle")
        pump(0.6)
    assert calls["n"] > 10 and shown_after > 10, "kept animating past the bad frame"
    assert os.path.getsize(app.ERROR_LOG) > before, "the bad frame was logged"
    assert app.overlay is win, "one bad frame doesn't turn it off"
    print("PASS  one bad frame is logged and the indicator carries on.")


def test_a_broken_indicator_turns_itself_off():
    fresh()
    with mock.patch.object(app.indicator, "render", side_effect=RuntimeError("always")), \
         mock.patch.object(app.indicator, "fine_timer"), \
         mock.patch.object(app, "_write_error_log") as log:
        app.update_overlay("recording")
        pump(1.5)
    assert app.overlay is None and app.overlay_anim_job is None, "gave up, cleanly"
    assert 1 <= log.call_count <= 3, f"logged, not flooded ({log.call_count} lines)"
    app.update_overlay("recording")                 # and later states are harmless
    print("PASS  a broken indicator logs once, turns itself off, and dictation goes on.")


def test_no_window_means_no_indicator():
    with mock.patch.object(app.indicator, "LayeredWindow", side_effect=OSError("refused")), \
         mock.patch.object(app, "_write_error_log") as log:
        app.build_overlay()
    assert app.overlay is None and log.called
    app.update_overlay("recording")
    assert app.overlay_anim_job is None
    print("PASS  if Windows won't make the window, Scribe runs without it (and says why in the log).")


def test_the_quota_colours_the_caret():
    fresh()
    seen = []
    real = app.indicator.render

    def spy(frame, scale):
        seen.append(frame.caret)
        return real(frame, scale)
    with mock.patch.object(app.indicator, "render", side_effect=spy), \
         mock.patch.object(app.indicator, "fine_timer"):
        app.quota_state = "warn"
        app.update_overlay("recording")
        pump(0.15)
        app.quota_state = "danger"
        pump(0.15)
        app.update_overlay("idle")
        pump(0.6)
    assert app.indicator.CARET_WARN in seen and app.indicator.CARET_DANGER in seen
    app.quota_state = "ok"
    print("PASS  near the Groq daily limit the caret turns amber, then rose.")


def test_the_tray_shows_the_state_and_follows_the_taskbar():
    app.tray_icon = mock.MagicMock()
    app.tray_icon.icon = None
    app.tray_icon.title = ""
    with mock.patch.object(app, "update_overlay"):
        app._apply_status("recording")
        assert app.tray_icon.icon is app._tray_image("recording")
        app._apply_status("idle")
        assert app.tray_icon.icon is app._tray_image("idle")
        light_before = app.TRAY_LIGHT
        with mock.patch.object(app.brand, "taskbar_is_light", return_value=not light_before), \
             mock.patch.object(app, "_refresh_status") as refresh:
            app._tray_theme_tick()
            assert app.TRAY_LIGHT is (not light_before) and refresh.called
        app._apply_status("idle")
        assert app.tray_icon.icon is app._tray_image("idle")
    app.tray_icon = None
    print("PASS  the tray mark shows the state and turns white/ink with the taskbar.")


if __name__ == "__main__":
    test_it_opens_follows_and_closes()
    test_it_opens_on_your_monitor()
    test_a_bad_frame_heals_and_is_logged()
    test_a_broken_indicator_turns_itself_off()
    test_no_window_means_no_indicator()
    test_the_quota_colours_the_caret()
    test_the_tray_shows_the_state_and_follows_the_taskbar()
    print("\nAll overlay tests passed.")
