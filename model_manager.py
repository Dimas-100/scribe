"""
=============================================================================
 SCRIBE MODEL MANAGER - the local speech model, handled in the background.
=============================================================================

 Loading Whisper takes seconds, and the very first run has to download it
 (about 150 MB to 1.5 GB). Scribe used to do both before its tray icon even
 appeared - a silent wait with no sign of life. Now the tray comes up at
 once and this module does the slow part on its own thread:

     request("small.en")
         -> already on disk?  load it                 state: loading -> ready
         -> not yet?          download it (bytes      state: downloading ->
                              reported as they arrive)       loading -> ready
         -> anything failed?  a short, plain reason   state: failed (retry())

 Other states: "waiting" (first run - the welcome hasn't finished) and "off"
 (cloud-only: no local model wanted).

 A size change is just a new request(): the new model loads while the old
 one keeps working, and replaces it only when it is fully loaded. A newer
 request supersedes an older one still in progress.

 The real find/download/load functions are looked up at CALL time, so the
 tests (and tests/harness.py) can swap in fakes - no Whisper, no network.
=============================================================================
"""

import errno
import os
import threading
import time

import storage

# The files faster-whisper needs from a model repo (same list it uses).
ALLOW_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin",
                  "tokenizer.json", "vocabulary.*"]

# CTranslate2 otherwise defaults to ~4 threads and under-uses a big CPU. Use
# most cores but leave a couple free for the mic stream, overlay and tray.
CPU_THREADS = max(4, min(8, (os.cpu_count() or 8) - 2))

# The error shown when a downloaded model's files won't load (a damaged or
# half-written file on disk): Retry then downloads the model again.
LOAD_FAILED = "The speech model on disk couldn't be loaded — Retry downloads it again."

# Progress is reported at most this often (seconds) - enough for a smooth
# bar without flooding the tray tooltip. State changes are always reported.
PROGRESS_INTERVAL = 0.5


# =============================================================================
#  THE REAL WORK  -  find, download, load.
# =============================================================================

def find_cached(name):
    """The folder of an already-downloaded model, or None. Never touches the
    network (faster-whisper's own loader would check Hugging Face for
    updates on every start)."""
    try:
        import faster_whisper.utils
        path = faster_whisper.utils.download_model(name, local_files_only=True)
    except Exception:
        return None
    # An interrupted download can leave the folder without its weights.
    if path and os.path.isfile(os.path.join(path, "model.bin")):
        return path
    return None


class _NullWriter:
    """Where the progress bar 'draws': nowhere (pythonw has no console)."""
    def write(self, text):
        pass

    def flush(self):
        pass


def _progress_bar_class(report):
    """
    A tqdm class for huggingface_hub.snapshot_download. It creates one bar
    counting BYTES across all files (unit "B") and one counting files; we
    draw neither, and pass the byte counter to report(done, total).
    """
    from tqdm import tqdm

    class _Progress(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["disable"] = False          # keep counting even undrawn
            kwargs["file"] = _NullWriter()
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            shown = super().update(n)
            if self.unit == "B":
                report(int(self.n), int(self.total or 0))
            return shown

    return _Progress


def download(name, report, force=False):
    """Download model `name` (e.g. "small.en") from Hugging Face into the
    standard cache, calling report(done_bytes, total_bytes) as it goes.
    Returns the model's folder. Resumes a partial download; force=True
    fetches every file again (the copy on disk wouldn't load)."""
    from faster_whisper.utils import _MODELS
    import huggingface_hub
    from huggingface_hub import constants
    repo_id = _MODELS.get(name)
    if repo_id is None:
        raise ValueError(f"unknown model {name!r}")
    # Plain HTTPS, not Hugging Face's newer "Xet" transfer. Xet reports its
    # progress in a few big jumps - the bar would sit near 0% for most of a
    # slow download - while plain HTTPS reports every 10 MB. (Measured on
    # small.en: 50 steady reports in 9.0 s, vs 5 jumps in 10.3 s with Xet.)
    # Read at call time by huggingface_hub, and nothing else in Scribe uses it.
    constants.HF_HUB_DISABLE_XET = True
    return huggingface_hub.snapshot_download(repo_id, allow_patterns=ALLOW_PATTERNS,
                                             tqdm_class=_progress_bar_class(report),
                                             force_download=force)


def load(path):
    """Load a downloaded model for CPU transcription.
    compute_type="int8" -> compressed math: fast and light on CPU (int8 is
    the right CPU choice: int8_float16 is a GPU type, and float32 is ~2-3x
    slower for a sub-half-point accuracy gain)."""
    import faster_whisper
    return faster_whisper.WhisperModel(path, device="cpu", compute_type="int8",
                                       cpu_threads=CPU_THREADS)


# Exception class names (anywhere in the chain) that mean "no connection".
# httpx's TransportError / NetworkError are the bases of ReadError and
# RemoteProtocolError - a connection lost MIDWAY through a download, which
# Hugging Face doesn't retry (it only retries connect errors and timeouts).
_NETWORK_ERRORS = {"ConnectionError", "ConnectError", "ConnectTimeout",
                   "ReadTimeout", "Timeout", "TimeoutError", "LocalEntryNotFoundError",
                   "OfflineModeIsEnabled", "MaxRetryError", "NameResolutionError",
                   "gaierror", "TransportError", "NetworkError", "RemoteProtocolError",
                   "ChunkedEncodingError", "IncompleteRead"}


def describe_error(exc):
    """A failure as one short, plain sentence for a notice or the welcome."""
    text = str(exc)
    if (isinstance(exc, OSError) and exc.errno == errno.ENOSPC) \
            or "no space left" in text.lower():
        return "Your disk is full."
    names = {cls.__name__ for cls in type(exc).__mro__}
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        names |= {cls.__name__ for cls in type(cause).__mro__}
    if names & _NETWORK_ERRORS:
        return "Couldn't reach the download server — check your internet connection."
    return f"{type(exc).__name__}: {text[:120]}"


# =============================================================================
#  THE MANAGER  -  one background worker, a small state machine.
# =============================================================================

class _Superseded(Exception):
    """Raised from a download's progress report when that model is no longer
    wanted (another size was chosen, or cloud-only). It aborts the download
    at its next chunk; Hugging Face resumes the partial file if it's ever
    wanted again."""


class ModelManager:
    def __init__(self, find_cached=None, download=None, load=None, on_change=None):
        # None = the module's real function, looked up at call time.
        self._fns = {"find_cached": find_cached, "download": download, "load": load}
        self._on_change = on_change
        self.progress_interval = PROGRESS_INTERVAL
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._desired = None        # the model name we want, or None
        self._model = None          # the loaded model in use
        self._loaded_name = None
        self._state = "off"
        self._done = self._total = 0
        self._error = None
        self._busy = False          # a worker thread is running
        self._attempt = None        # the model name the worker is working on
        self._redownload = set()    # cached models whose files failed to load
        self._redownloaded = set()  # ...already fetched again this session (once is enough)
        self._failed_stage = None   # "find" / "download" / "load" of the last failure
        self._thread = None
        self._last_notify = 0.0

    # --- reading -------------------------------------------------------

    def snapshot(self):
        """The state as a plain dict (safe to send over the control pipe)."""
        with self._lock:
            return {"state": self._state, "name": self._desired or self._loaded_name,
                    "done": self._done, "total": self._total, "error": self._error,
                    "failed_stage": self._failed_stage if self._state == "failed" else None}

    @property
    def state(self):
        with self._lock:
            return self._state

    def get(self):
        """The loaded model, or None. (A plain attribute read - atomic.)"""
        return self._model

    def wait_ready(self, timeout):
        """Wait (up to `timeout` s) while the worker is busy; True if a model
        is loaded and ready at the end."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._settled.wait(remaining)
            return self._model is not None and self._state == "ready"

    def wait_loaded(self, timeout):
        """Wait (up to `timeout` s) only while the model is LOADING - a
        download can take minutes, and a dictation shouldn't wait for it.
        True if a model is ready at the end."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._state == "loading" and self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._settled.wait(remaining)
            return self._model is not None and self._state == "ready"

    def join(self, timeout=None):
        """Wait for the worker thread to finish (tests)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # --- asking --------------------------------------------------------

    def request(self, name):
        """Make `name` the loaded model (loading/downloading it if needed)."""
        start = False
        with self._lock:
            self._desired = name
            if self._model is not None and self._loaded_name == name and not self._busy:
                self._state, self._error = "ready", None
            elif not self._busy:
                start = self._busy = True
                self._state, self._error, self._done, self._total = "loading", None, 0, 0
                self._thread = threading.Thread(target=self._run, name="model-loader",
                                                daemon=True)
            elif name != self._attempt:
                # The worker is busy with another model. It gives that one up
                # at its next progress report (a download) or before loading
                # it, then moves on to this one - so show THIS one as pending,
                # not the other model's bytes under this name.
                self._state, self._error, self._done, self._total = "loading", None, 0, 0
            # else: already working on exactly this model - leave it be
        if start:
            self._thread.start()
        self._notify(force=True)

    def disable(self):
        """No local model (cloud-only): drop it and stop wanting one."""
        with self._lock:
            self._desired = None
            self._model = self._loaded_name = None
            self._state, self._error, self._done, self._total = "off", None, 0, 0
            self._settled.notify_all()
        self._notify(force=True)

    def set_waiting(self):
        """First run: nothing is wanted until the welcome is finished."""
        with self._lock:
            if self._desired is None and self._model is None:
                self._state = "waiting"
        self._notify(force=True)

    def retry(self):
        """Try a failed model again."""
        with self._lock:
            name = self._desired if self._state == "failed" else None
        if name:
            self.request(name)

    # --- the worker ----------------------------------------------------

    def _call(self, which, *args):
        fn = self._fns[which] or globals()[which]
        return fn(*args)

    def _set_state(self, name, state):
        """Set the state for `name`'s attempt - unless it was superseded."""
        with self._lock:
            if self._desired != name:
                return
            self._state = state
            self._settled.notify_all()        # wait_loaded watches the state
        self._notify(force=True)

    def _progress(self, name, done, total):
        """A download's byte counter for model `name`. Raises _Superseded -
        which stops the download - once `name` is no longer wanted."""
        done, total = int(done), int(total)
        # Files whose size isn't known up front (a compressed tokenizer.json)
        # add bytes without adding to the total, so the raw counter overshoots
        # by a few MB. Never show more than 100%.
        if total:
            done = min(done, total)
        with self._lock:
            if self._desired != name:
                raise _Superseded(name)
            self._state, self._done, self._total = "downloading", done, total
            self._settled.notify_all()        # a "loading" wait ends here
        self._notify()

    def _still_wanted(self, name):
        with self._lock:
            return self._desired == name

    def _run(self):
        while True:
            with self._lock:
                name = self._desired
                if name is None or (self._model is not None and self._loaded_name == name):
                    if name is not None:
                        self._state, self._error = "ready", None
                    self._busy = False
                    self._attempt = None
                    self._settled.notify_all()
                    break
                self._attempt = name
                self._state, self._error, self._done, self._total = "loading", None, 0, 0
            self._notify(force=True)
            stage = "find"
            cached = False
            try:
                # A cached model whose files wouldn't load last time is fetched
                # again rather than found in the cache (the same bad files).
                force = name in self._redownload
                if force:
                    self._redownload.discard(name)
                    self._redownloaded.add(name)
                path = None if force else self._call("find_cached", name)
                cached = path is not None
                if path is None:
                    stage = "download"
                    self._set_state(name, "downloading")
                    report = lambda done, total: self._progress(name, done, total)
                    path = (self._call("download", name, report, True) if force
                            else self._call("download", name, report))
                    self._set_state(name, "loading")
                if not self._still_wanted(name):
                    continue                  # superseded: never load it
                stage = "load"
                model = self._call("load", path)
                self._redownload.discard(name)
            except _Superseded:
                continue                      # given up on purpose - not an error
            except Exception as exc:
                storage.log_error(f"speech model {name} ({stage})", exc)
                # Damaged files are the likely cause only when a model from the
                # CACHE won't load with an ordinary error - not a missing DLL
                # or memory (downloading again wouldn't help), not files we
                # just downloaded, and only once per session.
                damaged = (stage == "load" and cached
                           and not isinstance(exc, (ImportError, MemoryError))
                           and name not in self._redownloaded)
                with self._lock:
                    superseded = self._desired != name
                    if not superseded:
                        if damaged:
                            self._redownload.add(name)    # bad files: fetch them again
                        self._state = "failed"
                        self._failed_stage = stage
                        self._error = LOAD_FAILED if damaged else describe_error(exc)
                        self._busy = False
                        self._attempt = None
                        self._settled.notify_all()
                if superseded:
                    continue                  # try the newer request instead
                self._notify(force=True)
                return
            with self._lock:
                if self._desired == name:     # else superseded: drop it, loop
                    self._model, self._loaded_name = model, name
        self._notify(force=True)

    def _notify(self, force=False):
        """Tell on_change (throttled for progress). Never raises."""
        if self._on_change is None:
            return
        now = time.monotonic()
        if not force and now - self._last_notify < self.progress_interval:
            return
        self._last_notify = now
        try:
            self._on_change(self.snapshot())
        except Exception as exc:
            storage.log_error("model state callback", exc)
