r"""
Interactive probe: can Scribe read back the text box you're typing in?
(What "learn from your fixes" needs - see uia_text.py and fix_watch.py.)

Run from the project root:   venv\Scripts\python tests\uia_probe.py

Within 5 seconds, click into a text box in the app you want to check (a
browser, Word, a chat app) and type a few words. The probe then says whether
Scribe could read that box, and shows its first characters. Nothing is saved.
"""

import ctypes
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import uia_text  # noqa: E402

print("Click into a text box and type something - reading it in 5 seconds...")
time.sleep(5)
hwnd = ctypes.windll.user32.GetForegroundWindow()
title = ctypes.create_unicode_buffer(256)
ctypes.windll.user32.GetWindowTextW(hwnd, title, 256)
reader = uia_text.Reader()
started = time.perf_counter()
box = reader.focused_box(hwnd)
text = reader.read(box) if box is not None else None
took = (time.perf_counter() - started) * 1000
print(f"Window: {title.value!r}")
if text is None:
    print("Not readable here - Scribe won't learn fixes in this app (dictation still works).")
else:
    print(f"Readable in {took:.0f} ms ({len(text)} characters). It starts:")
    print("   " + text[:120].replace("\n", " "))
