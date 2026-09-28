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
    app._indicator_broken_runs = 0
    app._indicator_rebuild = False
    app.overlay = FakeWindow()
    return app.overlay


def _log_size():
    return os.path.getsize(app.ERROR_LOG) if os.path.exists(app.ERROR_LOG) else 0


def test_it_opens_follows_and_closes():
    win = fresh()
    log_before = _log_size()
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
    assert _log_size() == log_before, "a normal dictation writes nothing to the error log"
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


def _appearance(seconds=1.3):
    """One dictation's worth of indicator: open, run, close."""
    app.update_overlay("recording")
    pump(seconds)
    app.update_overlay("idle")
    pump(0.5)


def test_a_failing_window_steps_aside_and_tries_again():
    # e.g. the PC was locked or a monitor unplugged while the capsule was up:
    # every frame fails for a while. It hides and waits; the next dictation
    # makes a fresh window and it works again - not switched off for good.
    first = fresh()
    made = []

    def new_window(*size):
        made.append(FakeWindow(*size))
        return made[-1]
    with mock.patch.object(app.indicator, "LayeredWindow", side_effect=new_window),          mock.patch.object(app.indicator, "fine_timer"),          mock.patch.object(app, "notify") as notify,          mock.patch.object(app, "_write_error_log") as log:
        with mock.patch.object(app.indicator, "render", side_effect=RuntimeError("locked")):
            _appearance()
        assert app.overlay is not None and app.overlay_anim_job is None, "parked, not destroyed"
        assert not first.visible
        _appearance(0.4)                                   # the next dictation: works again
        assert made and app.overlay is made[-1] and made[-1].shows, "a fresh window, drawing again"
        assert not notify.called and log.call_count <= 3
    print("PASS  a failing window hides, and the next dictation starts afresh - it recovers.")


def test_a_broken_indicator_turns_itself_off():
    fresh()
    with mock.patch.object(app.indicator, "LayeredWindow", side_effect=FakeWindow),          mock.patch.object(app.indicator, "render", side_effect=RuntimeError("always")),          mock.patch.object(app.indicator, "fine_timer"),          mock.patch.object(app, "notify") as notify,          mock.patch.object(app, "_write_error_log") as log:
        for _ in range(app.INDICATOR_RETRIES):
            _appearance()
    assert app.overlay is None and app.overlay_anim_job is None, "gave up, cleanly"
    assert notify.call_count == 1 and notify.call_args.args[0] == "indicator_off", "and said so"
    assert 1 <= log.call_count <= 4, f"logged, not flooded ({log.call_count} lines)"
    app.update_overlay("recording")                 # and later states are harmless
    print("PASS  only after failing dictation after dictation does it switch off - and it says so.")


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


def test_the_capsule_moves_before_the_tray():
    # The tray swap (a new icon for Windows) can take a few milliseconds:
    # the capsule must not wait for it when you press the hotkey.
    order = []
    with mock.patch.object(app, "update_overlay", side_effect=lambda s: order.append("capsule")),          mock.patch.object(app, "set_state", side_effect=lambda *a: order.append("tray")):
        app._apply_status("recording")
    assert order == ["capsule", "tray"], order
    print("PASS  on a state change the capsule moves first, then the tray icon.")


def _icon_size(handle):
    import ctypes
    from ctypes import wintypes

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                    ("mask", wintypes.HBITMAP), ("color", wintypes.HBITMAP)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("t", wintypes.LONG), ("w", wintypes.LONG), ("h", wintypes.LONG),
                    ("wb", wintypes.LONG), ("p", wintypes.WORD), ("bpp", wintypes.WORD),
                    ("bits", ctypes.c_void_p)]
    u32, g32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    u32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    g32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    info = ICONINFO()
    assert u32.GetIconInfo(handle, ctypes.byref(info))
    bm = BITMAP()
    g32.GetObjectW(info.color, ctypes.sizeof(BITMAP), ctypes.byref(bm))
    g32.DeleteObject(info.color)
    g32.DeleteObject(info.mask)
    return bm.w, bm.h


def test_the_tray_icon_is_crisp_and_made_in_memory():
    import ctypes
    small = ctypes.windll.user32.GetSystemMetrics(49)          # SM_CXSMICON
    fresh_images = app._tray_images(False)
    assert all(small in img.info.get("png", {}) for img in fresh_images.values()), \
        "the tray-size pictures are ready at startup, not on the first dictation"
    app._TrayIcon._crisp_failed = False
    for state in ("idle", "recording", "transcribing"):
        icon = app._TrayIcon("scribe-test", icon=app._tray_image(state), title="test")  # never shown
        with mock.patch("tempfile.mkstemp", side_effect=AssertionError("no temp files")):
            icon._assert_icon_handle()
        assert _icon_size(icon._icon_handle) == (small, small)
        icon._release_icon()
    print(f"PASS  the tray icon is made in memory at the tray's own size ({small} px).")


def test_a_failing_crisp_icon_falls_back_once():
    app._TrayIcon._crisp_failed = False
    with mock.patch.object(app, "_small_icon_handle", side_effect=OSError("refused")),          mock.patch.object(app, "_write_error_log") as log:
        for state in ("idle", "recording", "idle"):
            icon = app._TrayIcon("scribe-test", icon=app._tray_image(state), title="test")
            icon._assert_icon_handle()                       # pystray's own way
            assert icon._icon_handle
            icon._release_icon()
    assert log.call_count == 1, "logged once, then quietly pystray's way"
    app._TrayIcon._crisp_failed = False
    print("PASS  if the crisp icon ever fails, pystray's own icon is used (logged once).")


if __name__ == "__main__":
    test_it_opens_follows_and_closes()
    test_it_opens_on_your_monitor()
    test_a_bad_frame_heals_and_is_logged()
    test_a_failing_window_steps_aside_and_tries_again()
    test_a_broken_indicator_turns_itself_off()
    test_no_window_means_no_indicator()
    test_the_quota_colours_the_caret()
    test_the_tray_shows_the_state_and_follows_the_taskbar()
    test_the_capsule_moves_before_the_tray()
    test_the_tray_icon_is_crisp_and_made_in_memory()
    test_a_failing_crisp_icon_falls_back_once()
    print("\nAll overlay tests passed.")
