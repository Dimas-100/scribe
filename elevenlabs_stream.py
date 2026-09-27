"""
=============================================================================
 SCRIBE - ELEVENLABS STREAMING  (Scribe v2 Realtime)
=============================================================================

 Groq (and the local model) can only start once you let go of the hotkey:
 the whole recording is sent, then transcribed. ElevenLabs' realtime model
 listens WHILE you speak - Scribe streams the audio over a websocket as it
 is recorded - so when you let go, only the last fraction of a second is
 left to transcribe. That's how the text can land ~0.5 s after release.

 One Session per dictation:
   start()        - open the connection (on the session's own thread)
   feed(chunk)    - the mic callback hands over each chunk (never blocks)
   finish(t)      - after release: send the rest, ask for the final text
                    (a "commit"), return it - or raise StreamError
   cancel()       - drop it (a shortcut, a too-short tap): no text wanted

 Scribe keeps its own copy of the recording, so when ElevenLabs can't
 answer nothing is lost: app.py hands the same audio to Groq or the local
 model. This module only talks to ElevenLabs - no app state, no UI - and
 `connect` is injectable, so the tests use a fake server on 127.0.0.1.

 The protocol, in short (docs: elevenlabs.io/docs, "Realtime"):
   - connect to URL?model_id=...&commit_strategy=manual&... with the
     header xi-api-key; the server says "session_started"
   - send {"message_type": "input_audio_chunk", "audio_base_64": <16-bit
     PCM>, "commit": false, "sample_rate": 16000} as audio arrives
   - send one with "commit": true on release; the server answers with
     "committed_transcript" {"text": ...}
   - errors arrive as messages ("auth_error", "quota_exceeded", ...)
=============================================================================
"""

import base64
import json
import queue
import threading
import time
from urllib.parse import urlencode

import numpy as np

URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
SUBSCRIPTION_URL = "https://api.elevenlabs.io/v1/user/subscription"
KEYS_URL = "https://elevenlabs.io/app/settings/api-keys"
MODEL_ID = "scribe_v2_realtime"

SAMPLE_RATE = 16000
# Audio goes out in 100 ms messages (a backlog, recorded while the connection
# was still opening, too - just faster): little is left to send on release,
# and a long take can be committed exactly at a pause (below).
CHUNK_SAMPLES = SAMPLE_RATE // 10

CONNECT_TIMEOUT = 4.0   # seconds to open the connection and start the session
FINAL_TIMEOUT = 2.0     # seconds after release to wait for the final text
RECV_POLL = 0.02        # how long the session thread listens between sends

# Long takes. ElevenLabs commits on its own after ~36 s of audio - and the
# text of such a commit comes back with most of those 36 s MISSING (seen
# live, Sept 2026: a 3-minute take returned 88 of ~330 words). So Scribe
# commits first: once COMMIT_AFTER seconds are uncommitted, at the next pause
# (QUIET_SECONDS of audio quiet for this take - see quiet_threshold), and at COMMIT_FORCE seconds
# even without one. ElevenLabs itself recommends committing every 20-30 s.
COMMIT_AFTER = 20.0
COMMIT_FORCE = 30.0
# ...and ElevenLabs refuses a commit of less than 0.3 s of new audio
# ("commit_throttled", then it hangs up) - so a short last tail after a long
# take's commit is padded with silence up to this.
MIN_COMMIT_SECONDS = 0.3
QUIET_SECONDS = 0.3
# What counts as "quiet" is judged against the take itself (quiet_threshold):
# mics differ a hundredfold in level - a laptop's raw array puts the voice
# barely above the room, a headset puts it far above silence. QUIET_RMS is
# only the fallback while there's too little audio (or contrast) to judge by.
QUIET_RMS = 0.01
QUIET_JUDGE_PIECES = 10          # a second of audio before the take's levels count
QUIET_MIN_CONTRAST = 1.3         # loud / quiet below this = nothing to tell apart
QUIET_SHARE = 0.25               # a pause: within the bottom quarter of the range (in dB)
QUIET_FLOOR = 1e-4               # -80 dBFS: below any real mic's own hiss (gated pauses are 0)


def quiet_threshold(levels):
    """The level (RMS) under which a piece of THIS take counts as a pause:
    a quarter of the way, in decibels, from its quiet (10th percentile of the
    pieces' levels) up to its speech (90th) - loudness is heard in ratios, so
    a pause is judged the same way (a soft syllable at a fifth of the peak is
    still speech). The fixed QUIET_RMS until there's a second of audio - or
    when the take has no contrast (a steady sound, pure room noise), where
    there's nothing to tell apart."""
    if len(levels) < QUIET_JUDGE_PIECES:
        return QUIET_RMS
    low = max(float(np.percentile(levels, 10)), QUIET_FLOOR)
    high = float(np.percentile(levels, 90))
    if high < low * QUIET_MIN_CONTRAST:
        return QUIET_RMS
    return float(low * (high / low) ** QUIET_SHARE)

# ElevenLabs' realtime limits for priority words ("keyterms").
KEYTERM_MAX = 50
KEYTERM_MAX_CHARS = 20

# Credits one hour of realtime transcription uses, measured with
# tests/elevenlabs_probe.py --usage. None = not measured: the dashboard then
# shows the share of credits used, without an hours-left estimate.
CREDITS_PER_REALTIME_HOUR = None

# The server's error messages, by what Scribe should do about them. Any
# other message carrying an "error" is a "server" failure.
_ERROR_KINDS = {
    "auth_error": "auth",
    "quota_exceeded": "quota",
    "rate_limited": "busy", "queue_overflow": "busy", "resource_exhausted": "busy",
    "unaccepted_terms": "terms",
}
# Not a failure: the take held no speech, so the text is simply empty.
_NO_SPEECH = "insufficient_audio_activity"


class StreamError(Exception):
    """
    ElevenLabs couldn't give the final text. `kind` says why - "auth" (key
    rejected), "quota" (the plan's hours are used up), "busy", "terms"
    (unaccepted terms of service), "network", "timeout", "server" (anything
    else it refused) or "broken" (Scribe's websocket library is missing) -
    and `detail` has the specifics for the error log.
    """

    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


def keyterms(terms, user_name=""):
    """
    The priority words for a session - ElevenLabs listens harder for these.
    The user's name first, then the Dictionary's terms: each at most
    KEYTERM_MAX_CHARS long (longer ones are skipped - the Corrections list
    still fixes them afterwards), no duplicates ignoring case, at most
    KEYTERM_MAX. Past the limit the NEWEST terms win (the Dictionary adds
    new terms at the end), and a duplicate keeps its newest spelling.
    """
    def usable(t):
        return isinstance(t, str) and 0 < len(t.strip()) <= KEYTERM_MAX_CHARS
    name = (user_name or "").strip()
    head = [name] if usable(name) else []
    seen = {name.lower()} if head else set()
    newest_first = []
    for t in reversed(list(terms or ())):
        if usable(t) and t.strip().lower() not in seen:
            seen.add(t.strip().lower())
            newest_first.append(t.strip())
    return head + list(reversed(newest_first[:KEYTERM_MAX - len(head)]))


def build_url(terms=(), url=URL):
    """The session URL: model, audio format, a manual commit (one per take,
    on release - a pause mid-sentence never splits it), English, ElevenLabs'
    filler removal, and the priority words - repeated, as ElevenLabs expects."""
    params = [("model_id", MODEL_ID), ("audio_format", "pcm_16000"),
              ("commit_strategy", "manual"), ("language_code", "en"),
              ("no_verbatim", "true")]
    params += [("keyterms", t) for t in terms]
    return f"{url}?{urlencode(params)}"


def _pcm16(audio):
    """float32 in [-1, 1] -> 16-bit little-endian PCM bytes. Clipped first,
    like the WAV path, so a hot mic can't wrap around to noise."""
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _default_connect():
    """websockets' connect - imported lazily, so a broken install only
    matters to someone who actually uses ElevenLabs."""
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise StreamError("broken", f"websockets isn't installed ({exc})") from None
    return connect


def _handshake_error(exc):
    """What a failed connection attempt means. A refused handshake carries
    the HTTP status; anything else (DNS, refused, TLS, timeout) is network."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return StreamError("auth", f"HTTP {status}")
    if status == 402:               # "payment required": the plan's credits are gone
        return StreamError("quota", "HTTP 402")
    if status == 429:
        return StreamError("busy", "HTTP 429")
    if status is not None and status < 500:
        return StreamError("server", f"HTTP {status}")
    return StreamError("network", f"HTTP {status}" if status else f"{type(exc).__name__}: {exc}")


def _recv(ws, timeout):
    """The next server message as a dict, None if nothing arrived within
    `timeout` seconds, {} for one that isn't a JSON object. A closed
    connection is a network error."""
    try:
        raw = ws.recv(timeout=timeout)
    except TimeoutError:
        return None
    except Exception as exc:              # ConnectionClosed and friends
        raise StreamError("network", f"connection closed ({exc})") from None
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return msg if isinstance(msg, dict) else {}


def _raise_if_error(msg):
    """Raise the StreamError a server message stands for, if it's an error."""
    kind = str(msg.get("message_type") or "")
    if kind in _ERROR_KINDS:
        raise StreamError(_ERROR_KINDS[kind], str(msg.get("error") or kind))
    if msg.get("error") and kind != _NO_SPEECH:      # "error": null isn't one
        raise StreamError("server", f"{kind}: {msg.get('error')}")


def _pending_error(ws):
    """After a failed send: the error the server explained before it closed
    the connection (say, quota_exceeded), if it sent one. That reason beats
    a plain "the connection closed"."""
    for _ in range(50):
        try:
            msg = _recv(ws, 0)
        except StreamError:
            return None
        if msg is None:
            return None
        try:
            _raise_if_error(msg)
        except StreamError as exc:
            return exc
    return None


def _close(ws):
    try:
        ws.close()
    except Exception:
        pass


def _open(url, api_key, connect, timeout):
    """Connect and wait for "session_started" - both within `timeout`
    seconds, together. Returns the open connection; raises StreamError (and
    leaves nothing open) otherwise."""
    deadline = time.monotonic() + timeout
    try:
        ws = connect(url, additional_headers={"xi-api-key": api_key},
                     open_timeout=timeout, close_timeout=1)
    except Exception as exc:              # refused, DNS, TLS, timeout, HTTP 401...
        raise _handshake_error(exc) from None
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise StreamError("network", "the session didn't start in time")
            msg = _recv(ws, left)
            if msg is None:
                continue
            if msg.get("message_type") == "session_started":
                return ws
            _raise_if_error(msg)
    except BaseException:
        _close(ws)
        raise


# =============================================================================
#  SESSION  -  one dictation's stream.
# =============================================================================

class Session:
    """
    One dictation streamed to ElevenLabs. The session's own thread does all
    the network work; the mic callback only drops chunks into a queue, and
    the dictation's worker thread calls finish() after release.
    """

    def __init__(self, api_key, terms=(), *, url=URL, connect=None,
                 connect_timeout=CONNECT_TIMEOUT):
        self._api_key = api_key
        self.api_key = api_key            # which key this take used (app.py's rejections)
        self._url = build_url(terms, url)
        self._connect = connect           # None = websockets' (see _default_connect)
        self._connect_timeout = connect_timeout
        self._audio = queue.SimpleQueue()   # float32 chunks from the mic
        self._finishing = threading.Event()  # released: send the rest + commit
        self._cancelled = threading.Event()  # no text wanted: just close
        self._opened = threading.Event()     # connected - or gave up trying
        self._done = threading.Event()       # the text (or the error) is ready
        self._segments = []                  # committed texts, in order
        self._text = None
        self._error = None
        self._thread = None
        self._started = time.monotonic()      # (reset by start())
        # For the console, the usage log and the tests:
        self.streamed_seconds = 0.0       # audio actually sent (what's billed)
        self.connect_ms = None            # how long connecting took
        self.final_ms = None              # release -> final text
        self.last_partial = ""            # ElevenLabs' latest running guess
        # For committing a long take in pieces (see COMMIT_AFTER):
        self._since_commit = 0            # samples sent since the last commit
        self._quiet_run = 0               # quiet samples at the end of those
        self._levels = []                 # every sent piece's RMS, for quiet_threshold
        self._commits = 0                 # commits sent...
        self._awaiting = 0                # ...whose text hasn't come back yet

    def start(self):
        """Open the connection on the session's own thread (never waits)."""
        self._started = time.monotonic()      # opening must be done by + connect timeout
        self._thread = threading.Thread(target=self._run, name="elevenlabs-stream",
                                        daemon=True)
        self._thread.start()
        return self

    def feed(self, chunk):
        """Called from the mic callback with each new chunk. A queue put -
        never blocks, never touches the network."""
        if not (self._finishing.is_set() or self._cancelled.is_set()):
            self._audio.put(chunk)

    def finish(self, timeout=FINAL_TIMEOUT):
        """
        After release (the mic has stopped, so every chunk is queued): send
        what's left, ask for the final text and return it. If the
        connection is still opening, that is waited out first - only until
        the opening's own deadline (start + connect timeout); then the
        answer gets `timeout` seconds. Raises StreamError.
        """
        released = time.monotonic()
        self._finishing.set()
        opening_left = self._started + self._connect_timeout + 0.25 - released
        self._opened.wait(max(0.0, opening_left))
        if not self._done.wait(timeout):
            self._cancelled.set()             # the thread closes the socket
            raise StreamError("timeout", f"no final text within {timeout:.1f} s")
        if self._error is not None:
            raise self._error
        self.final_ms = (time.monotonic() - released) * 1000
        return self._text

    def cancel(self):
        """Drop the stream: close without asking for any text."""
        self._cancelled.set()

    # --- the session thread --------------------------------------------------

    def _run(self):
        ws = None
        try:
            t0 = time.monotonic()
            try:
                ws = _open(self._url, self._api_key,
                           self._connect or _default_connect(), self._connect_timeout)
                self.connect_ms = (time.monotonic() - t0) * 1000
            finally:
                self._opened.set()
            self._stream(ws)
        except StreamError as exc:
            self._error = exc
        except Exception as exc:              # never let the thread die silently
            self._error = StreamError("server", f"{type(exc).__name__}: {exc}")
        finally:
            # The answer is ready: finish() returns NOW - the goodbye
            # handshake below never adds to the time until your text appears.
            self._done.set()
            if ws is not None:
                _close(ws)

    def _take_audio(self, pending):
        """`pending` plus every chunk the mic has queued since, as one array."""
        parts = [pending]
        while True:
            try:
                parts.append(np.asarray(self._audio.get_nowait(), np.float32).reshape(-1))
            except queue.Empty:
                break
        return np.concatenate(parts) if len(parts) > 1 else pending

    def _send(self, ws, audio, commit):
        """Send one audio message (None = no audio, just the commit)."""
        data = b"" if audio is None else _pcm16(audio)
        message = {"message_type": "input_audio_chunk",
                   "audio_base_64": base64.b64encode(data).decode("ascii"),
                   "commit": commit, "sample_rate": SAMPLE_RATE}
        try:
            ws.send(json.dumps(message))
        except Exception as exc:
            raise _pending_error(ws) or StreamError("network", f"send failed ({exc})") from None
        if audio is not None:
            self.streamed_seconds += len(audio) / SAMPLE_RATE
            self._since_commit += len(audio)
            level = float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0
            self._levels.append(level)
            quiet = len(audio) and level < quiet_threshold(self._levels)
            self._quiet_run = self._quiet_run + len(audio) if quiet else 0

    def _commit(self, ws):
        """Ask for the text of everything sent since the last commit."""
        self._send(ws, None, commit=True)
        self._commits += 1
        self._awaiting += 1
        self._since_commit = self._quiet_run = 0

    def _time_to_commit(self):
        """A long take's piece is due: 20 s+ uncommitted and a pause just
        happened - or 30 s, pause or not (ElevenLabs' own commit at ~36 s
        loses text)."""
        uncommitted = self._since_commit / SAMPLE_RATE
        return (uncommitted >= COMMIT_FORCE
                or (uncommitted >= COMMIT_AFTER
                    and self._quiet_run >= QUIET_SECONDS * SAMPLE_RATE))

    def _finished(self):
        self._text = " ".join(self._segments)

    def _stream(self, ws):
        """Send audio as it arrives and read what the server says - until
        the final text arrives, a failure, or a cancel."""
        pending = np.zeros(0, dtype=np.float32)
        committed = False
        while not self._cancelled.is_set():
            # Read the release flag BEFORE draining the queue: every chunk
            # was queued before release, so once this is set the drain below
            # is guaranteed to hold the whole take.
            finishing = self._finishing.is_set()
            pending = self._take_audio(pending)
            while len(pending) >= CHUNK_SAMPLES or (finishing and len(pending)):
                piece, pending = pending[:CHUNK_SAMPLES], pending[CHUNK_SAMPLES:]
                self._send(ws, piece, commit=False)
                if self._time_to_commit():
                    self._commit(ws)              # a long take: this piece of it, now
            if finishing and not committed:
                # "That's all - the rest of the text, please." (Nothing new
                # since a long take's last commit: nothing to ask for.)
                if self._since_commit or not self._commits:
                    short = int(MIN_COMMIT_SECONDS * SAMPLE_RATE) - self._since_commit
                    if short > 0:
                        # Too little to commit (you let go a moment after a
                        # long take's pause commit): pad it with silence.
                        self._send(ws, np.zeros(short, dtype=np.float32), commit=False)
                    self._commit(ws)
                committed = True
            # Done once every commit has been answered - a long take's
            # earlier piece may still be on its way when the last one is sent.
            if committed and not self._awaiting:
                self._finished()
                return
            msg = _recv(ws, RECV_POLL)
            if not msg:
                continue
            kind = msg.get("message_type")
            if kind == "partial_transcript":
                self.last_partial = str(msg.get("text") or "")
            elif kind == "committed_transcript":
                text = str(msg.get("text") or "").strip()
                if text:
                    self._segments.append(text)
                self._awaiting = max(0, self._awaiting - 1)
            elif kind == _NO_SPEECH:
                self._awaiting = max(0, self._awaiting - 1)
            elif kind == "warning":
                print(f"[STREAM] ElevenLabs warning: {msg.get('warning')}")
            else:
                _raise_if_error(msg)


# =============================================================================
#  FOR THE DASHBOARD  -  "does this key work?" and "how much is left?"
# =============================================================================

# What a failed check means, for the welcome's and Settings' key box.
_CHECK_RESULTS = {
    "auth":    ("rejected", "ElevenLabs rejected this key. Check that you copied all of "
                            "it and that it's allowed to use Speech to Text."),
    "network": ("offline", "Couldn't reach ElevenLabs — check your internet connection."),
    "quota":   ("ok", "Key works, but this month's hours are used up."),
    "terms":   ("error", "Sign in at elevenlabs.io and accept the terms first."),
    "broken":  ("error", "Scribe's streaming component isn't installed. Run setup.bat "
                         "again to repair it."),
}


def check_key(key, *, url=URL, connect=None, timeout=8):
    """
    Ask ElevenLabs whether `key` can open a realtime session - then close it
    without sending any audio, so the check costs nothing. Returns
    {"status": "ok" | "rejected" | "offline" | "error", "message": one short
    sentence for the page}, like dashboard.check_groq_key.
    """
    key = (key or "").strip()
    if not key:
        return {"status": "rejected", "message": "Paste your key first."}
    try:
        ws = _open(build_url((), url), key, connect or _default_connect(), timeout)
    except StreamError as exc:
        status, message = _CHECK_RESULTS.get(
            exc.kind, ("error", "ElevenLabs returned an error. Try again in a moment."))
        return {"status": status, "message": message}
    _close(ws)
    return {"status": "ok", "message": "Key works."}


def get_usage(key, *, get=None, timeout=4.0):
    """
    How much of this billing period's credits the account has used:
    {"status": "ok", "used", "limit", "resets_at" (unix seconds, or None),
    "tier"} - or {"status": "no_permission"} (the key isn't allowed to read
    account info), "rejected", "offline" or "error". Never raises. `get`
    is injectable for tests (httpx.get's signature).
    """
    import httpx      # lazy, like the rest of the cloud code
    key = (key or "").strip()
    if not key:
        return {"status": "rejected"}
    try:
        resp = (get or httpx.get)(SUBSCRIPTION_URL, headers={"xi-api-key": key},
                                  timeout=timeout)
    except httpx.HTTPError:
        return {"status": "offline"}
    except Exception:
        return {"status": "error"}
    if resp.status_code in (401, 403):
        # A restricted key without "User: Read" gets a 401 that says which
        # permission is missing - that's a hint, not a bad key.
        return {"status": "no_permission" if "permission" in resp.text.lower() else "rejected"}
    if resp.status_code != 200:
        return {"status": "error"}
    try:
        data = resp.json()
        used, limit = int(data["character_count"]), int(data["character_limit"])
    except (ValueError, KeyError, TypeError):
        return {"status": "error"}
    resets = data.get("next_character_count_reset_unix")
    return {"status": "ok", "used": used, "limit": limit,
            "resets_at": int(resets) if isinstance(resets, (int, float)) else None,
            "tier": str(data.get("tier") or "")}
