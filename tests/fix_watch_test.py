r"""
Standalone probe for fix_watch.py - watching the text box Scribe just typed
into, to learn the words you fix.

Run from the project root:   venv\Scripts\python tests\fix_watch_test.py

Safe: a scripted fake reader stands in for Windows UI Automation - no other
app is read, nothing is saved.
"""

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import fix_watch  # noqa: E402


class FakeReader:
    """Plays back what a window's text box holds, read after read: a list
    per window (the last one repeats). A window missing from `boxes` has no
    readable box (a password box, an app UI Automation can't read)."""

    def __init__(self, boxes, fail_on=()):
        self.boxes = {h: list(texts) for h, texts in boxes.items()}
        self.fail_on = set(fail_on)
        self.reads = []

    def focused_box(self, hwnd):
        return hwnd if hwnd in self.boxes else None

    def read(self, box):
        self.reads.append(box)
        if box in self.fail_on:
            raise RuntimeError("the app went away")
        texts = self.boxes[box]
        return texts.pop(0) if len(texts) > 1 else texts[0]


def _watcher(reader, **kw):
    fixes, errors = [], []
    kw.setdefault("poll", 0.02)
    kw.setdefault("watch", 0.3)
    kw.setdefault("settle", 0.0)
    w = fix_watch.FixWatcher(lambda: reader, lambda wrong, right: fixes.append((wrong, right)),
                             on_error=lambda where, exc: errors.append(where), **kw)
    w.start()
    return w, fixes, errors


def test_a_fix_is_learned():
    reader = FakeReader({1: ["Hello. ask cal she about it", "Hello. ask Kalshee about it"]})
    w, fixes, errors = _watcher(reader)
    w.watch(1, "ask cal she about it ")
    assert w.wait_idle(3)
    assert fixes == [("cal she", "Kalshee")] and not errors, (fixes, errors)
    print("PASS  a word you fix after Scribe typed it is learned.")


def test_a_fix_is_learned_when_scribes_text_ends_the_box():
    # Scribe adds a space after each dictation (the default), and the box
    # keeps it: the text is followed to the end of the box, not to the first
    # space inside it.
    for before in ("", "Hello. "):
        reader = FakeReader({1: [before + "ask cal she about it ", before + "ask Kalshee about it "]})
        w, fixes, errors = _watcher(reader)
        w.watch(1, "ask cal she about it ")
        assert w.wait_idle(3)
        assert fixes == [("cal she", "Kalshee")] and not errors, (before, fixes, errors)
    print("PASS  a fix is learned when Scribe's text (and its trailing space) ends the box.")


def test_scribes_own_typing_is_never_a_fix():
    # You select "cal she" and dictate it again: Scribe types the new take
    # over it. That is Scribe's text, not your fix.
    reader = FakeReader({1: ["ask cal she about it"]})
    w, fixes, _errors = _watcher(reader, watch=3.0)
    w.watch(1, "ask cal she about it")
    time.sleep(0.1)
    w.interrupt()                              # Scribe is about to type into the box...
    reader.boxes[1] = ["ask Kelsey about it"]  # ...and does
    time.sleep(0.1)
    w.watch(1, "Kelsey")
    time.sleep(0.1)
    w.stop()
    w.thread.join(2)
    assert fixes == [], fixes
    print("PASS  what Scribe itself types into a watched box is never learned as a fix.")


def test_an_unexpected_failure_never_kills_the_watcher():
    class Odd(FakeReader):
        def read(self, box):
            return 42 if box == 5 else super().read(box)
    reader = Odd({5: ["x"], 1: ["ask cal she now", "ask Kalshee now"]})
    w, fixes, errors = _watcher(reader)
    w.watch(5, "whatever")
    assert w.wait_idle(3), "the watch ended"
    w.watch(1, "ask cal she now")
    assert w.wait_idle(3) and w.thread.is_alive()
    assert fixes == [("cal she", "Kalshee")] and "watch" in errors, (fixes, errors)
    print("PASS  an unexpected failure ends that watch, never the watcher.")


def test_switching_off_ends_the_watch():
    reader = FakeReader({1: ["ask cal she now", "ask cal she now", "ask cal she now",
                             "ask Kalshee now"]})
    w, fixes, _errors = _watcher(reader, poll=0.05, watch=2.0)
    w.watch(1, "ask cal she now")
    time.sleep(0.02)
    w.enabled = False                          # Settings: learning switched off
    assert w.wait_idle(3)
    assert fixes == [] and len(reader.reads) <= 2, (fixes, reader.reads)
    print("PASS  switching learning off stops the watch at once - nothing more is read.")


def test_a_sent_message_keeps_the_last_good_read():
    reader = FakeReader({1: ["ask cal she about it", "ask Kalshee about it", ""]})
    w, fixes, _errors = _watcher(reader, watch=5.0)
    started = time.monotonic()
    w.watch(1, "ask cal she about it")
    assert w.wait_idle(3)
    assert fixes == [("cal she", "Kalshee")], fixes
    assert time.monotonic() - started < 2, "an empty box ends the watch early"
    print("PASS  a message sent right after a fix: the fix still counts; the watch ends.")


def test_words_you_add_are_not_fixes():
    reader = FakeReader({1: ["Note: ask cal she", "Note: ask cal she and then more words"]})
    w, fixes, _errors = _watcher(reader)
    w.watch(1, "ask cal she")
    assert w.wait_idle(3)
    assert fixes == [], fixes
    print("PASS  words you type after Scribe's text aren't taken for fixes.")


def test_a_new_dictation_ends_the_old_watch():
    reader = FakeReader({1: ["ask cal she now", "ask Kalshee now"],
                         2: ["web bull rocks", "Webull rocks"]})
    w, fixes, _errors = _watcher(reader, watch=3.0)
    started = time.monotonic()
    w.watch(1, "ask cal she now")
    time.sleep(0.15)
    w.watch(2, "web bull rocks")
    time.sleep(0.15)
    w.stop()                                   # ends the second watch too (on its reads so far)
    w.thread.join(2)
    assert not w.thread.is_alive()
    assert fixes == [("cal she", "Kalshee"), ("web bull", "Webull")], fixes
    assert time.monotonic() - started < 2, "neither watch ran its full time"
    print("PASS  a new dictation ends the previous watch (on its reads so far); stop ends it all.")


def test_unreadable_boxes_are_skipped():
    reader = FakeReader({1: ["ask cal she now"]})
    w, fixes, errors = _watcher(reader)
    w.watch(7, "ask cal she now")              # no readable box in window 7
    w.watch(1, "something Scribe never typed here")   # not in the box
    assert w.wait_idle(3)
    assert fixes == [] and not errors and 7 not in reader.reads
    print("PASS  a password or unreadable box, or text that isn't there, is left alone.")


def test_a_failing_reader_never_stops_the_watcher():
    reader = FakeReader({1: ["ask cal she now"], 2: ["ask cal she now", "ask Kalshee now"]},
                        fail_on={1})
    w, fixes, errors = _watcher(reader)
    w.watch(1, "ask cal she now")
    assert w.wait_idle(3)
    w.watch(2, "ask cal she now")
    assert w.wait_idle(3)
    assert errors == ["read"] and fixes == [("cal she", "Kalshee")], (errors, fixes)
    w2 = fix_watch.FixWatcher(lambda: reader, lambda *_: 1 / 0, poll=0.02, watch=0.1, settle=0.0,
                              on_error=lambda where, exc: errors.append(where))
    w2.start()
    w2.watch(2, "ask Kalshee now")
    reader.boxes[2] = ["ask Kalshee now", "ask Kalsheee now"]
    assert w2.wait_idle(3) and w2.thread.is_alive()
    print("PASS  a failing read or a failing learn ends that watch - never the watcher.")


def test_off_and_broken():
    reader = FakeReader({1: ["ask cal she now", "ask Kalshee now"]})
    w, fixes, _errors = _watcher(reader)
    w.enabled = False
    w.watch(1, "ask cal she now")
    assert w.wait_idle(1) and fixes == [] and reader.reads == []
    errors = []

    def no_uia():
        raise OSError("UI Automation unavailable")
    w3 = fix_watch.FixWatcher(no_uia, lambda *_: None, on_error=lambda where, exc: errors.append(where))
    w3.start()
    w3.thread.join(2)
    assert errors == ["start"] and not w3.enabled and not w3.thread.is_alive()
    w3.watch(1, "anything")                    # ignored: nothing to watch with
    print("PASS  switched off it reads nothing; without UI Automation it stops quietly.")


if __name__ == "__main__":
    test_a_fix_is_learned()
    test_a_fix_is_learned_when_scribes_text_ends_the_box()
    test_scribes_own_typing_is_never_a_fix()
    test_an_unexpected_failure_never_kills_the_watcher()
    test_switching_off_ends_the_watch()
    test_a_sent_message_keeps_the_last_good_read()
    test_words_you_add_are_not_fixes()
    test_a_new_dictation_ends_the_old_watch()
    test_unreadable_boxes_are_skipped()
    test_a_failing_reader_never_stops_the_watcher()
    test_off_and_broken()
    print("\nAll fix-watch tests passed.")
