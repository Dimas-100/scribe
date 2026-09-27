r"""
Standalone probe for uia_text.py - reading a text box through Windows UI
Automation, on the thread that made the reader.

Run from the project root:   venv\Scripts\python tests\uia_text_test.py

Safe: it starts a real Reader but only asks about a window that doesn't
exist - no other app's text is read. (tests\uia_probe.py reads a real box,
on purpose.)
"""

import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import uia_text  # noqa: E402


def test_the_reader_starts_and_never_raises():
    out = {}

    def on_its_own_thread():                  # how the fix watcher uses it
        reader = uia_text.Reader()
        out["box"] = reader.focused_box(0x7FFFFFF0)       # no such window
        out["none"] = reader.read(None)
        out["junk"] = reader.read(("not", "a box"))
    t = threading.Thread(target=on_its_own_thread)
    t.start()
    t.join(20)
    assert out == {"box": None, "none": None, "junk": None}, out
    print("PASS  the UI Automation reader starts on its own thread and never raises.")


if __name__ == "__main__":
    test_the_reader_starts_and_never_raises()
    print("\nAll UI Automation reader tests passed.")
