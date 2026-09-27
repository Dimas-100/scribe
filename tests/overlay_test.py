"""
Faithful verification for the status-pill animation fix.

This imports the REAL app module (so we exercise the exact production code,
not a copy) and drives the overlay through its three states the same way
update_status() does in normal use - via update_overlay() on the Tk main
thread. It checks three things:

  1. EXPAND   - on "recording", the pill grows from the collapsed idle
                height (OVERLAY_H_IDLE) toward the active height (OVERLAY_H).
  2. SELF-HEAL - we force ONE _draw_pill() call to raise mid-recording.
                The old code would freeze the pill permanently here; the
                fixed code must log the error and keep animating.
  3. COLLAPSE - on "idle", the pill eases back to its idle height and the loop parks
                (overlay_anim_job goes back to None).

Run from the project root:  python tests/overlay_test.py
Safe to run while Scribe is running - tests/harness.py mocks the port, the
model and the mic, and points app.py at a temp data folder.
"""

import os
import sys
import tkinter as tk

# Import app.py through the shared harness: its top-level setup runs with
# the model, mic and port mocked. It does NOT call main(), so no listener or
# tray threads start - we drive the overlay ourselves below.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402
app = import_app_with_mocks()

ERR_BEFORE = os.path.getsize(app.ERROR_LOG) if os.path.exists(app.ERROR_LOG) else 0

results = {}

# Stand up a real (hidden) root + the real overlay, exactly like main() does.
app.root = tk.Tk()
app.root.withdraw()
app.build_overlay()

# --- One-shot fault injection to prove self-healing -------------------------
_real_draw_pill = app._draw_pill
_state = {"boom_armed": False, "boom_fired": False}

def _maybe_boom(w, h, *args, **kwargs):
    # Pass everything through: _draw_pill also takes the live waveform
    # (bars=, bar_color=), and a wrapper that drops them breaks every frame.
    if _state["boom_armed"] and not _state["boom_fired"]:
        _state["boom_fired"] = True
        raise RuntimeError("injected transient draw failure (test)")
    return _real_draw_pill(w, h, *args, **kwargs)

app._draw_pill = _maybe_boom

def sample(label):
    results[label] = (round(app._pill_h, 2), app.overlay_anim_job is not None)
    print(f"  {label:22} pill_h={app._pill_h:5.2f}  loop_alive={app.overlay_anim_job is not None}")

print("\n=== driving the real overlay ===")
sample("idle@start")

# t=200ms: enter recording -> pill should start expanding toward OVERLAY_H.
app.root.after(200, lambda: (print("-> update_overlay('recording')"), app.update_overlay("recording")))
app.root.after(1200, lambda: sample("recording@1.2s"))

# t=1400ms: arm the fault so the NEXT tick's _draw_pill raises exactly once.
app.root.after(1400, lambda: _state.__setitem__("boom_armed", True))
app.root.after(2000, lambda: sample("after-injected-fault"))   # must still be alive + expanded

# t=2400ms: collapse back to idle.
app.root.after(2400, lambda: (print("-> update_overlay('idle')"), app.update_overlay("idle")))
app.root.after(3800, lambda: sample("idle@3.8s"))

app.root.after(4000, app.root.quit)
app.root.mainloop()

# --- verdict ---------------------------------------------------------------
err_after = os.path.getsize(app.ERROR_LOG) if os.path.exists(app.ERROR_LOG) else 0
logged = err_after > ERR_BEFORE

rec_h, rec_alive = results["recording@1.2s"]
heal_h, heal_alive = results["after-injected-fault"]
idle_h, idle_alive = results["idle@3.8s"]

print("\n=== verdict ===")
# Sizes come from app.py itself, so a design tweak can't silently break this.
ACTIVE_H, IDLE_H = app.OVERLAY_H, app.OVERLAY_H_IDLE
ok_expand = rec_h >= ACTIVE_H - 2             # grew to (about) the active height
ok_fired  = _state["boom_fired"]              # the fault actually triggered
ok_heal   = heal_alive and heal_h >= ACTIVE_H - 2   # survived the fault, still expanded
ok_logged = logged                            # fault was logged, not silent
ok_park   = idle_h <= IDLE_H + 2 and not idle_alive  # collapsed and loop parked

print(f"  EXPAND  to active size .......... {'PASS' if ok_expand else 'FAIL'} (h={rec_h})")
print(f"  fault injected .................. {'yes' if ok_fired else 'NO'}")
print(f"  SELF-HEAL after fault .......... {'PASS' if ok_heal else 'FAIL'} (alive={heal_alive}, h={heal_h})")
print(f"  fault was LOGGED (not silent) ... {'PASS' if ok_logged else 'FAIL'}")
print(f"  COLLAPSE + park ................. {'PASS' if ok_park else 'FAIL'} (h={idle_h}, alive={idle_alive})")

all_ok = ok_expand and ok_fired and ok_heal and ok_logged and ok_park
print(f"\n  OVERALL: {'ALL PASS' if all_ok else 'FAILURE'}")
