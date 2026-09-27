"""
=============================================================================
 SCRIBE FIX WATCH - learn from the words you correct.
=============================================================================

 After Scribe types a dictation, app.py calls watch(window, text). This
 module's one thread then:

   1. waits a moment for the paste to land, and reads that window's text box
      (through a Reader - uia_text.py; a fake in the tests). It finds the text
      Scribe typed and remembers a few characters around it (anchors);
   2. reads the box again every POLL_SECONDS, for up to WATCH_SECONDS,
      following Scribe's text between its anchors as you edit it;
   3. when the watch ends, compares what Scribe typed with what you left
      (learning.find_fixes) and hands each fix to on_fix(wrong, right).

 The watch ends early when Scribe is about to type again - a new dictation,
 "scratch that", "fix that" call interrupt() first, and only the reads made
 BEFORE it count (a later one could hold Scribe's own words, not your fix) -
 when the box is cleared or gone (a chat message was sent - the last good
 read still counts), when Scribe's text can no longer be found, or when
 learning is switched off (then nothing is learned).

 Rules that keep this safe:
   - Everything happens on THIS thread: UI Automation (COM) objects must stay
     on the thread that made them, and a slow app can only slow a watch down.
   - watch() is only a queue put, safe from any thread; nothing here touches
     Tk or the keyboard hook.
   - Nothing raises into Scribe: a failed read ends that watch, a failed
     learn is reported, and both go to on_error(where, exc) - as does any
     other failure inside a watch ("watch"), which never ends the thread.
   - What is read stays in memory, only for the watch. Only the fixes leave.
=============================================================================
"""

import queue
import threading
import time

import learning

SETTLE_SECONDS = 0.25     # let the paste land before the first read
POLL_SECONDS = 2.0        # how often the box is read again
WATCH_SECONDS = 60.0      # how long a dictation is watched at most

_STOP = object()          # stop(): end the thread (after the current watch)


class FixWatcher:
    """
    One thread watching one text box at a time. `make_reader()` is called ON
    the thread (COM objects belong to the thread that made them) and must
    return a Reader: focused_box(hwnd) -> box or None, read(box) -> text or
    None. `on_fix(wrong, right)` learns a fix; `on_error(where, exc)` hears
    about failures ("start", "read", "learn", "watch"). The times are
    parameters so the tests can run a watch in a fraction of a second.
    """

    def __init__(self, make_reader, on_fix, on_error=None, poll=POLL_SECONDS,
                 watch=WATCH_SECONDS, settle=SETTLE_SECONDS):
        self.make_reader = make_reader
        self.on_fix = on_fix
        self.on_error = on_error or (lambda where, exc: None)
        self.poll, self.watch_seconds, self.settle = poll, watch, settle
        # Off (the setting, or no UI Automation): watch() does nothing, and
        # a watch already running stops at its next look, learning nothing.
        self.enabled = True
        self.thread = None
        self._jobs = queue.Queue()
        self._pending = None       # a job that arrived during a watch: the next one
        # interrupt() counts up: a watch belongs to the count it was asked
        # for under, and ends once Scribe types again.
        self._typed_count = 0
        self._count_lock = threading.Lock()

    # --- Called from other threads ----------------------------------------

    def start(self):
        self.thread = threading.Thread(target=self._run, name="fix-watch", daemon=True)
        self.thread.start()
        return self.thread

    def watch(self, hwnd, typed):
        """Scribe just typed `typed` into window `hwnd`: watch it. A queue put."""
        if self.enabled and isinstance(typed, str) and typed.strip():
            self._jobs.put((hwnd, typed.strip(), self._typed_count))

    def interrupt(self):
        """Scribe is about to type into a window (a dictation, "scratch
        that", "fix that"): the current watch ends on the reads it already
        has - any read finishing after this may hold Scribe's own text. Call
        it BEFORE typing. Never blocks."""
        with self._count_lock:
            self._typed_count += 1

    def stop(self):
        """End the thread (the current watch ends on its last read)."""
        self._jobs.put(_STOP)

    def wait_idle(self, timeout):
        """For tests: True once every watch asked for so far has finished."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._jobs.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    # --- The thread ---------------------------------------------------------

    def _run(self):
        try:
            reader = self.make_reader()
        except Exception as exc:          # no comtypes, UI Automation refused...
            self.enabled = False
            self.on_error("start", exc)
            self._drain()
            return
        while True:
            item, self._pending = self._pending or self._jobs.get(), None
            if item is _STOP:
                self._jobs.task_done()
                return
            try:
                self._watch_one(reader, *item)
            except Exception as exc:      # a bug ends this watch - never the watcher
                self.on_error("watch", exc)
            self._jobs.task_done()

    def _drain(self):
        """Mark anything already queued as done (there's nothing to watch with)."""
        while True:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                return
            self._jobs.task_done()

    def _wait(self, seconds):
        """Wait up to `seconds` - but a new job ends the wait at once (it is
        kept in _pending, the next to handle). True if one came."""
        if seconds <= 0:
            return False
        try:
            self._pending = self._jobs.get(timeout=seconds)
            return True
        except queue.Empty:
            return False

    def _still_on(self, count):
        """May this watch go on? Not once learning is switched off, and not
        once Scribe typed again (interrupt() since it was asked for)."""
        return self.enabled and self._typed_count == count

    def _watch_one(self, reader, hwnd, typed, count):
        """Watch one dictation, asked for when interrupt() stood at `count`."""
        if self._wait(self.settle) or not self._still_on(count):
            return                            # dictated again before it even landed
        try:
            box = reader.focused_box(hwnd)
            text = reader.read(box) if box is not None else None
        except Exception as exc:
            self.on_error("read", exc)
            return
        if not text or not self._still_on(count):
            return                            # a password box, an app we can't read
        at = text.rfind(typed)
        if at < 0:
            return                            # not where we can see it: leave it
        end = at + len(typed)
        before = text[max(0, at - learning.ANCHOR_CHARS):at]
        after = text[end:end + learning.ANCHOR_CHARS]
        last = typed
        deadline = time.monotonic() + self.watch_seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if self._wait(min(self.poll, left)) or not self._still_on(count):
                break                         # Scribe types again: the reads so far count
            try:
                text = reader.read(box)
            except Exception as exc:
                self.on_error("read", exc)
                break
            if not self._still_on(count):
                break                         # this read may hold Scribe's own typing
            if not text or not text.strip():
                break                         # sent or cleared: the last good read counts
            region = learning.find_region(before, after, text)
            if region is None:
                break                         # rewritten around it: stop following
            last = region
        if not self.enabled:
            return                            # switched off meanwhile: learn nothing
        for wrong, right in learning.find_fixes(typed, last):
            try:
                self.on_fix(wrong, right)
            except Exception as exc:
                self.on_error("learn", exc)
