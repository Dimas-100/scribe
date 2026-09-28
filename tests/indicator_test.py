r"""
Standalone probe for indicator.py - the capsule that appears while you
dictate: its motion (open, listen, work, close), its look, where it sits,
and the see-through Windows window it is shown in.

Run from the project root:   venv\Scripts\python tests\indicator_test.py

Safe: the motion and drawing are pure; the one real window is shown fully
transparent, far off screen, for a moment.
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import numpy as np  # noqa: E402

import indicator  # noqa: E402

FRAME = 1 / 60


def run(motion, start, seconds, level=0.0):
    """Step `motion` at 60 fps from `start` for `seconds`: [(t, frame)]."""
    out, t = [], start
    while t <= start + seconds + 1e-9:
        out.append((t, motion.step(t, level)))
        t += FRAME
    return out


def test_idle_draws_nothing():
    m = indicator.Motion()
    assert m.step(0.0) is None and m.step(1.0) is None
    print("PASS  idle: nothing on screen, nothing drawn.")


def test_it_opens_at_once_and_smoothly():
    m = indicator.Motion()
    m.set_state("recording", 10.0)
    frames = run(m, 10.0 + FRAME, 0.5)
    first = frames[0][1]
    assert first is not None and first.alpha > 0.1, "visible on the very first frame"
    assert first.width < indicator.CAP_W / 2, "it starts small, around the caret"
    t, f = next((t, f) for t, f in frames if t >= 10.35)
    assert abs(f.width - indicator.CAP_W) < 1 and f.alpha > 0.99 and f.draw_in > 0.99, f
    assert max(f.width for _t, f in frames) < indicator.CAP_W * 1.06, "a slight overshoot at most"
    print("PASS  the capsule appears on the first frame and is fully open in ~300 ms.")


def test_it_closes_to_nothing():
    m = indicator.Motion()
    m.set_state("recording", 0.0)
    run(m, FRAME, 0.6)
    m.set_state("idle", 0.7)
    frames = run(m, 0.7 + FRAME, 0.6)
    gone = next(t for t, f in frames if f is None)
    assert gone - 0.7 < 0.45, f"closed in {gone - 0.7:.2f} s"
    assert all(f is None for t, f in frames if t >= gone), "and stays closed"
    print("PASS  on release it folds up and fades out in under half a second.")


def test_nothing_jumps():
    # Press, let go mid-opening, press again mid-fade, work, finish: at each
    # change the next frame continues from the last one (a restart would
    # snap the width back to the dot or the fade back to nothing).
    m = indicator.Motion()
    changes = ((0.0, "recording"), (0.12, "idle"), (0.40, "recording"),
               (0.80, "transcribing"), (1.30, "idle"))
    frames, t, i = [], FRAME, 0
    while t < 2.0:
        if i < len(changes) and t >= changes[i][0]:
            before = frames[-1] if frames else None
            m.set_state(changes[i][1], changes[i][0])
            after = m.step(t)
            if before is not None and before[1] is not None and after is not None:
                a = before[1]
                # (an opening moves up to ~18 px a frame; a restart would snap ~70)
                assert abs(a.width - after.width) < 20, (changes[i], a.width, after.width)
                assert abs(a.alpha - after.alpha) < 0.35, (changes[i], a.alpha, after.alpha)
                assert abs(a.draw_in - after.draw_in) < 0.25, (changes[i], a.draw_in, after.draw_in)
            frames.append((t, after))
            i += 1
        else:
            frames.append((t, m.step(t)))
        t += FRAME
    shown = [f for _t, f in frames if f is not None]
    for a, b in zip(shown, shown[1:]):
        assert abs(a.width - b.width) < 22, "never a snap"
    print("PASS  changing state mid-animation continues from where it is - nothing jumps.")


def test_your_voice_moves_the_wave():
    m = indicator.Motion()
    m.set_state("recording", 0.0)
    quiet = run(m, FRAME, 0.5, level=0.0)[-1][1]
    assert 0.03 < quiet.amp < 0.15, "listening: never dead flat, but calm"
    loud = run(m, 0.5 + FRAME, 0.3, level=0.15)[-1][1]
    assert loud.amp > 0.8, loud.amp
    m.set_state("transcribing", 0.9)
    after = run(m, 0.9 + FRAME, 0.3, level=0.15)[-1][1]
    assert after.amp < 0.02, "let go: the wave settles into a straight line"
    print("PASS  the wave follows your voice, and settles flat when you let go.")


def test_working_glides_a_light_along_the_line():
    m = indicator.Motion()
    m.set_state("recording", 0.0)
    run(m, FRAME, 0.4)
    m.set_state("transcribing", 0.4)
    frames = run(m, 0.4 + FRAME, 2.0)
    late = [f for t, f in frames if t > 0.9]
    assert all(f.shimmer_mix > 0.95 for f in late)
    spots = [f.shimmer for f in late]
    assert min(spots) < 0.2 and max(spots) > 0.8, "it glides the whole line, again and again"
    print("PASS  while Scribe works, a soft light glides toward the caret.")


def test_the_look():
    f = indicator.Frame(width=indicator.CAP_W, alpha=1.0, scale=1.0, draw_in=1.0, amp=0.0,
                        phase=0.0, shimmer=0.0, shimmer_mix=0.0, caret=indicator.CARET)
    for scale in (1.0, 1.5):
        img = indicator.render(f, scale)
        w, h = indicator.window_size(scale)
        assert img.size == (w, h) and img.mode == "RGBA"
        a = np.asarray(img)
        assert a[0, 0, 3] == 0 and a[-1, -1, 3] == 0, "outside the capsule: see-through"
        cy, cx = h // 2, w // 2
        above = cy - int(8 * scale)                      # above the (flat) line
        assert a[above, cx, 3] > 230 and a[above, cx, :3].max() < 60, "a solid dark capsule"
        assert a[cy, cx, :3].min() > 200, "the line runs through the middle"
        caret_x = cx + int(round((indicator.CAP_W / 2 - indicator.CARET_INSET) * scale))
        assert a[cy, caret_x - 1:caret_x + 2, :3].max() > 220, "a bright caret"
        below = a[cy + int((indicator.CAP_H / 2 + 5) * scale), cx]
        assert 5 < below[3] < 140, ("a soft shadow under it", below)
    waving = indicator.render(f._replace(amp=1.0, phase=1.0), 1.0)
    assert not np.array_equal(np.asarray(waving), np.asarray(indicator.render(f, 1.0)))
    print("PASS  a dark capsule with a soft shadow, a bright caret, see-through around it.")


def test_it_renders_fast_enough_for_60fps():
    f = indicator.Frame(width=indicator.CAP_W, alpha=1.0, scale=1.0, draw_in=1.0, amp=0.7,
                        phase=0.0, shimmer=0.4, shimmer_mix=1.0, caret=indicator.CARET)
    indicator.render(f, 2.0)
    t0 = time.perf_counter()
    for i in range(20):
        indicator.render(f._replace(phase=i * 0.1), 2.0)
    ms = (time.perf_counter() - t0) / 20 * 1000
    assert ms < 25, f"{ms:.1f} ms a frame at 200% scaling"
    print(f"PASS  a frame renders in {ms:.1f} ms at 200% scaling.")


def test_it_sits_just_above_the_taskbar():
    x, y = indicator.place((0, 0, 1920, 1040), 1.0)
    w, h = indicator.window_size(1.0)
    assert x == (1920 - w) // 2
    capsule_bottom = y + indicator.MARGIN + indicator.CAP_H
    assert capsule_bottom == 1040 - indicator.BOTTOM_GAP, (capsule_bottom, y)
    x2, _ = indicator.place((1920, 0, 3840, 1040), 1.0)
    assert x2 == 1920 + (1920 - w) // 2, "on the monitor you're working on"
    print("PASS  it sits centred, just above the taskbar, on your monitor.")


def test_the_real_window():
    if sys.platform != "win32":
        print("SKIP  the see-through window is Windows-only.")
        return
    w, h = indicator.window_size(1.0)
    win = indicator.LayeredWindow(w, h)
    try:
        f = indicator.Frame(width=indicator.CAP_W, alpha=1.0, scale=1.0, draw_in=1.0, amp=0.5,
                            phase=0.0, shimmer=0.0, shimmer_mix=0.0, caret=indicator.CARET)
        win.show(indicator.render(f, 1.0), -32000, -32000, 0)   # invisible, off screen
        assert win.visible
        win.hide()
        assert not win.visible
    finally:
        win.close()
    print("PASS  the see-through window opens, shows, hides and closes.")


if __name__ == "__main__":
    test_idle_draws_nothing()
    test_it_opens_at_once_and_smoothly()
    test_it_closes_to_nothing()
    test_nothing_jumps()
    test_your_voice_moves_the_wave()
    test_working_glides_a_light_along_the_line()
    test_the_look()
    test_it_renders_fast_enough_for_60fps()
    test_it_sits_just_above_the_taskbar()
    test_the_real_window()
    print("\nAll indicator tests passed.")
