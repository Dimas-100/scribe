"""
=============================================================================
 SCRIBE DASHBOARD - the HTML/CSS/JS face of the app.
=============================================================================

 This file is the glue between the dashboard's web UI (dashboard.html) and
 the Python app: it boots a native pywebview window that loads the HTML,
 reads the same on-disk files app.py writes (config.json, vocabulary.json,
 dictation_log.jsonl), and exposes a small JsApi object that the page's
 JavaScript calls to get data and save settings.

 Why a separate process?
   - Tkinter (used by app.py for the floating overlay) and pywebview both
     want to own the main thread. Splitting the dashboard into its own
     process side-steps the conflict and keeps each event loop happy.
   - app.py spawns dashboard.py as a subprocess on demand; the dashboard
     exits when its window closes.

 Communication with app.py:
   - Data flow IN  -> we just read the JSON files app.py writes.
   - Data flow OUT -> save_settings writes config.json, then sends a
     small message over Scribe's control pipe (instance.py) so the running
     app.py reloads its in-memory config without waiting for a restart.

 The HTML and CSS that make this look modern live in dashboard.html.
=============================================================================
"""

import base64
import calendar
import ctypes
import hashlib
import io
import math
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime

# webview is the pywebview module - it bundles Edge WebView2 on Windows.
import webview

# Scribe's own shared modules (also used by app.py): where your data lives
# and how it's saved safely, and which microphones can actually record.
# devices.py imports sounddevice softly, so a missing audio backend just
# means an empty mic list, never a dead dashboard.
import autostart
import devices
import elevenlabs_stream
import instance
import keystore
import learning
import storage
import updates
from version import VERSION

# Pillow is already a project dependency (app.py uses it for the tray
# icon). Imported softly so a partial install doesn't kill the dashboard -
# we just fall back to no app icons in that case.
try:
    from PIL import Image
except ImportError:
    Image = None


# =============================================================================
#  WINDOWS APP IDENTITY  -  make the taskbar show our icon, not Python's.
# =============================================================================

# Set BEFORE any window is created. We deliberately use the SAME AUMID as
# app.py and the pinned Start Menu shortcut ("Scribe.VoiceDictation"), so
# Windows groups the dashboard window with the pinned tile - clicking the
# pinned icon either launches Scribe or brings the existing dashboard to
# the front, depending on what's running.
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "Scribe.VoiceDictation"
    )
except Exception:
    pass  # not on Windows / call unavailable - harmless to skip


# =============================================================================
#  FILE LOCATIONS  -  mirror app.py's layout so we read from the same place.
# =============================================================================

APP_DIR     = os.path.dirname(os.path.abspath(__file__))
HTML_FILE   = os.path.join(APP_DIR, "dashboard.html")   # code assets live here
ICO_FILE    = os.path.join(APP_DIR, "scribe.ico")
# Your data lives in the per-user data folder (%APPDATA%\Scribe), shared with
# app.py through storage.py - so the two can never disagree about paths.
CONFIG_FILE      = storage.CONFIG_FILE
VOCAB_FILE       = storage.VOCAB_FILE
LOG_FILE         = storage.LOG_FILE
CLOUD_USAGE_FILE = storage.CLOUD_USAGE_LOG

# =============================================================================
#  GROQ FREE-TIER RATE LIMITS  -  for whisper-large-v3-turbo. Hard-coded
#  because they change only when Groq announces a tier update; the dashboard
#  shows today's usage as a fraction of these.
# =============================================================================

GROQ_LIMITS = {
    "minute_requests":    20,
    "day_requests":       2000,
    "hour_audio_seconds": 7200,    # 2 hours of audio per hour
    "day_audio_seconds":  28800,   # 8 hours of audio per day
}


# =============================================================================
#  CHOICES  -  the same lists app.py uses for its dropdowns, from storage.py
#  (the single source of truth) so the two can never drift apart.
# =============================================================================

HOTKEY_CHOICES = storage.HOTKEY_NAMES
MODEL_CHOICES  = storage.MODEL_CHOICES
MIC_DEFAULT_LABEL = "(system default)"

# Defaults used when config.json is missing or partial, so the dashboard
# never shows a blank/None for a setting the user hasn't explicitly chosen.
DEFAULT_CONFIG = storage.DEFAULT_CONFIG

# =============================================================================
#  FILE READERS  -  tiny, defensive: every file is optional.
# =============================================================================

# Recovery notices (a damaged settings/vocabulary file restored or reset)
# waiting to be shown on the page - once each.
_page_notices = []


def _remember(notices):
    for n in notices:
        if all(n["key"] != m["key"] for m in _page_notices):
            _page_notices.append(n)


def _take_notices():
    """Hand the pending notices to the page (and forget them)."""
    out = list(_page_notices)
    _page_notices.clear()
    return out


def read_config():
    """The validated settings. storage recovers a damaged file first (restores
    the last good copy, keeps the broken one) - so the dashboard can never
    write defaults over your real settings again."""
    cfg, notices = storage.load_config()
    _remember(notices)
    return cfg


# The window's colour before the page paints - the same as the page's
# background in each theme, so opening the dashboard never flashes.
WINDOW_BACKGROUND = {"light": "#f7f7f5", "dark": "#111113"}


def _windows_uses_light_theme():
    """True when Windows' apps are set to light mode (the default when the
    setting can't be read)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except OSError:
        return True


def window_background(cfg):
    """The pre-paint colour for the user's theme ("system" asks Windows)."""
    theme = (cfg or {}).get("theme", "system")
    if theme not in WINDOW_BACKGROUND:
        theme = "light" if _windows_uses_light_theme() else "dark"
    return WINDOW_BACKGROUND[theme]


def page_html(cfg):
    """dashboard.html with the saved theme stamped on <html>, so the page's
    very first paint is already in the right colours (its pre-paint script
    reads it; "system" there asks Windows via the browser)."""
    theme = (cfg or {}).get("theme")
    if theme not in storage.THEMES:
        theme = "system"
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    return html.replace('<html lang="en">', f'<html lang="en" data-saved-theme="{theme}">', 1)


def _watch_minimize(window):
    """Tell the page when the window is minimized or comes back. The page
    polls the app for live status only while it can be seen, and WebView2
    doesn't always report a minimized window as hidden (document.hidden) -
    so this says so directly. Best effort: a failed call just means the page
    keeps polling a little longer."""
    def say(minimized):
        code = f"window.scribeMinimized = {'true' if minimized else 'false'}"

        def handler(*_):
            try:
                window.evaluate_js(code)
            except Exception:
                pass
        return handler
    window.events.minimized += say(True)
    window.events.restored += say(False)
    window.events.maximized += say(False)


def page_config(cfg):
    """
    The settings as the page may see them. Never an API key itself - the
    page runs web code, so it only learns THAT a key is saved ("ends in
    1a2b", one hint per service) - plus the start-at-sign-in switch, which
    lives in the registry.
    """
    key_fields = {field for _target, field in keystore.SERVICES.values()}
    out = {k: v for k, v in cfg.items() if k not in key_fields}
    out["groq_key_hint"] = keystore.key_hint(keystore.resolve_key(cfg))
    out["elevenlabs_key_hint"] = keystore.key_hint(keystore.resolve_key(cfg, "elevenlabs"))
    # None = not ours to manage here (a test / portable copy): the page
    # hides the switch.
    out["start_at_login"] = autostart.is_enabled() if autostart.available() else None
    return out


# =============================================================================
#  GROQ KEY CHECK  -  "does this key work?", asked before it is saved.
# =============================================================================

GROQ_KEYS_URL = "https://console.groq.com/keys"
KEY_CHECK_TIMEOUT = 8        # seconds; the welcome shows "Checking..." meanwhile


def check_groq_key(key):
    """
    Ask Groq whether `key` works by listing its models (free - no audio, no
    quota). Returns {"status": "ok" | "rejected" | "offline" | "error",
    "message": one short sentence for the page}.
    """
    key = (key or "").strip()
    if not key:
        return {"status": "rejected", "message": "Paste your key first."}
    try:
        import groq
        import httpx
    except ImportError:
        return {"status": "error", "message": "Scribe's cloud component isn't "
                "installed. Run setup.bat again to repair it."}
    client = groq.Groq(api_key=key, max_retries=0,
                       timeout=httpx.Timeout(KEY_CHECK_TIMEOUT, connect=4))
    try:
        client.models.list()
    except (groq.AuthenticationError, groq.PermissionDeniedError):
        return {"status": "rejected",
                "message": "Groq rejected this key. Check that you copied all of it."}
    except groq.APIConnectionError:            # includes timeouts
        return {"status": "offline",
                "message": "Couldn't reach Groq — check your internet connection."}
    except groq.APIStatusError as exc:
        return {"status": "error",
                "message": f"Groq returned an error ({exc.status_code}). Try again in a moment."}
    except Exception as exc:
        storage.log_error("check Groq key", exc)
        return {"status": "error",
                "message": "Couldn't check the key — details are in the error log."}
    return {"status": "ok", "message": "Key works."}


def _read_vocab_raw():
    """The validated vocabulary as {terms, corrections, dismissed, learned}.
    The single source of truth for the read-modify-write bridge methods, so
    an external hand-edit (or a word Scribe just learned) is always re-read
    before we write back over it."""
    vocab, notices = storage.load_vocab()
    _remember(notices)
    return vocab


def _write_vocab_raw(raw):
    """Save the vocabulary safely. Raises storage.StorageError on failure -
    which reaches the page as an error toast, never a false "Added"."""
    storage.save_vocab(raw)


def read_vocab():
    """Return {terms, corrections, dismissed} for the UI. Corrections arrive as
    a dict in the file but the page wants ordered pairs (longest 'wrong' first,
    matching app.py's apply_vocabulary() ordering), so we convert here."""
    raw = _read_vocab_raw()
    pairs = sorted(raw["corrections"].items(), key=lambda p: len(p[0]), reverse=True)
    return {"terms": raw["terms"], "corrections": pairs, "dismissed": raw["dismissed"]}


def _local_timestamp(value):
    """An ISO timestamp as a naive local-time ISO string, or None."""
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone().replace(tzinfo=None)
    return ts.isoformat(timespec="seconds")


def _number(value, default, cast):
    """A finite, non-negative number from `value`, else `default`."""
    if isinstance(value, bool):
        return default
    try:
        n = cast(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return n if math.isfinite(n) and n >= 0 else default


def _clean_log_entry(e):
    """A dictation entry with sane types, or None if it can't be shown."""
    text = e.get("text")
    ts = _local_timestamp(e.get("timestamp"))
    if not isinstance(text, str) or ts is None:
        return None
    out = dict(e)
    out["timestamp"] = ts
    out["words"] = _number(e.get("words"), len(text.split()), int)
    out["duration"] = _number(e.get("duration"), 0.0, float)
    for key in ("app_name", "app_exe"):
        if key in out and not isinstance(out[key], str):
            del out[key]
    # The newer, optional fields - which service, the time to text, what you
    # said before polish - are kept only with the right type: the page does
    # arithmetic and DOM work with them, and a hand-edited line must never be
    # able to stop it.
    if not isinstance(out.get("engine"), str) or not out.get("engine"):
        out.pop("engine", None)
    latency = out.get("latency")
    if (isinstance(latency, bool) or not isinstance(latency, (int, float))
            or not math.isfinite(latency) or latency < 0):
        out.pop("latency", None)
    if not isinstance(out.get("raw"), str) or not out.get("raw"):
        out.pop("raw", None)
    if out.get("polished") is not True or "raw" not in out:
        out.pop("polished", None)            # "polished" means: here's what you said
    return out


class HistoryCache:
    """
    The dictation history, read once and then only as it grows. It remembers
    how far it has read (a byte offset), which file that was (its id) and the
    last bytes before the offset. New dictations are appended, so normally
    only the new lines are read. If the file shrank, was replaced (an undo
    rewrites it) or the bytes before the offset changed, it re-reads it all -
    when in doubt, a full read, never a guess.

    Several bridge calls refresh the cache (polls, vocabulary edits), so the
    page is never told "what changed since the last refresh" - it says what
    it HAS (the history generation and how many entries) and gets exactly
    what it's missing (snapshot_since). `gen` goes up on every full reload.
    A read that fails (another program holding the file) changes nothing.
    """
    TAIL = 64

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.gen = 0
        self._reset_state()

    def _reset_state(self):
        self.gen += 1
        self.entries = []
        self._offset = 0
        self._ino = None
        self._tail = b""
        self._stamp = None                  # (mtime, size) at the last read

    def _tail_at(self, offset):
        """The TAIL bytes before `offset`, or None if the file can't be read."""
        start = max(0, offset - self.TAIL)
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                return f.read(offset - start)
        except OSError:
            return None

    def _take(self, raw, new_offset, st):
        new = [c for c in map(_clean_log_entry, raw) if c]
        self.entries.extend(new)
        self._offset = new_offset
        self._ino = st.st_ino
        # None if it can't be read now: the next check then can't match, and
        # the next change is read in full - safe, never a duplicate.
        self._tail = self._tail_at(new_offset)
        self._stamp = (st.st_mtime, st.st_size)
        return new

    def refresh(self):
        """Catch up with the file: ("same", []), ("appended", new entries),
        ("reset", all entries), or ("unreadable", []) when the file couldn't
        be read just now - then nothing changed and the next call retries."""
        with self._lock:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                had = bool(self.entries) or self._stamp is not None
                if had:
                    self._reset_state()
                return ("reset", []) if had else ("same", [])
            except OSError:
                return ("unreadable", [])
            if self._stamp == (st.st_mtime, st.st_size) and self._ino == st.st_ino:
                return ("same", [])
            if (self._stamp is None or st.st_ino != self._ino or st.st_size < self._offset
                    or st.st_size == self._stamp[1]):
                # A new file, a shorter one - or the same size with a new
                # modified time: edited in place (Notepad), so read it again.
                rewritten = True
            else:
                tail = self._tail_at(self._offset)
                if tail is None:
                    return ("unreadable", [])
                rewritten = tail != self._tail
            if rewritten:
                read = storage.read_jsonl_from(self.path, 0)
                if read is None:
                    return ("unreadable", [])
                self._reset_state()
                self._take(read[0], read[1], st)
                return ("reset", list(self.entries))
            read = storage.read_jsonl_from(self.path, self._offset)
            if read is None:
                return ("unreadable", [])
            new = self._take(read[0], read[1], st)
            return ("appended", new) if new else ("same", [])

    def snapshot_since(self, gen, count):
        """What a page holding `count` entries of generation `gen` is
        missing: {"reset": False, "appended": [...]} - or, if its copy is from
        another generation (the file was rewritten), {"reset": True,
        "entries": [...]}. Either way with the current "history_gen"."""
        with self._lock:
            n = len(self.entries)
            if gen == self.gen and isinstance(count, int) and 0 <= count <= n:
                return {"reset": False, "appended": self.entries[count:],
                        "history_gen": self.gen}
            return {"reset": True, "entries": list(self.entries),
                    "history_gen": self.gen}

    def entries_copy(self):
        with self._lock:
            return list(self.entries)


HISTORY = HistoryCache(LOG_FILE)


def read_log():
    """Every showable dictation, oldest first (via the incremental cache).
    Malformed lines and wrong types are skipped, so a hand-edited or
    half-written log can never freeze the page on "Loading…"."""
    HISTORY.refresh()
    return HISTORY.entries_copy()


def _log_signature():
    """Return a fingerprint of the on-disk logs the dashboard cares about,
    so the poll loop can short-circuit when nothing has changed. The
    fingerprint is a flat [mtime, size, mtime, size, ...] list - one
    (mtime, size) pair per file, in a fixed order: dictation log first,
    cloud-usage log second. Cheap (one stat() per file) and we include
    size as well as mtime to catch two writes inside the same
    filesystem-mtime tick (~1-2s on Windows)."""
    sig = []
    for path in (LOG_FILE, CLOUD_USAGE_FILE):
        try:
            st = os.stat(path)
            sig.extend([st.st_mtime, st.st_size])
        except OSError:
            sig.extend([0.0, 0])
    return sig


def read_cloud_usage():
    """Cloud-usage entries, oldest first (objects only - storage skips blank
    and malformed lines; summarize_cloud_usage checks each field)."""
    return storage.read_jsonl(CLOUD_USAGE_FILE)


def summarize_cloud_usage():
    """Bucket the cloud-usage entries into the four counters the
    dashboard renders: requests-this-minute, requests-today, audio-
    seconds-this-hour, audio-seconds-today. All bucketing is done in
    local time, matching the time the user sees in History. Returns a
    dict ready to JSON-serialize into the page."""
    # ElevenLabs' streamed time has its own meter (get_elevenlabs_usage) -
    # it isn't Groq quota.
    entries = [e for e in read_cloud_usage() if e.get("provider") != "elevenlabs"]
    now = datetime.now()
    today = now.date()
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    minute_start = now.replace(second=0, microsecond=0)

    minute_requests = 0
    day_requests = 0
    hour_audio = 0.0
    day_audio = 0.0
    for e in entries:
        ts_str = _local_timestamp(e.get("timestamp"))
        if ts_str is None:
            continue
        ts = datetime.fromisoformat(ts_str)
        audio = _number(e.get("audio_seconds"), 0.0, float)
        if ts.date() != today:
            continue
        day_requests += 1
        day_audio += audio
        if ts >= hour_start:
            hour_audio += audio
        if ts >= minute_start:
            minute_requests += 1

    return {
        "minute_requests":    minute_requests,
        "day_requests":       day_requests,
        "hour_audio_seconds": round(hour_audio, 1),
        "day_audio_seconds":  round(day_audio, 1),
        "limits":             GROQ_LIMITS,
        "total_calls":        len(entries),
    }


def cycle_start(resets_at, now=None):
    """
    When the current ElevenLabs billing month began: one month before the
    next reset (same day and time, clamped to a shorter month - a Mar 31
    reset means the cycle began Feb 28), or the 1st of this month when the
    reset time isn't known (the key can't read account info).
    """
    if isinstance(resets_at, (int, float)) and resets_at > 0:
        try:
            reset = datetime.fromtimestamp(resets_at)
        except (OverflowError, OSError, ValueError):
            reset = None
        if reset is not None:
            year, month = ((reset.year, reset.month - 1) if reset.month > 1
                           else (reset.year - 1, 12))
            day = min(reset.day, calendar.monthrange(year, month)[1])
            return reset.replace(year=year, month=month, day=day)
    now = now or datetime.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def stream_seconds_since(start):
    """Seconds of audio streamed to ElevenLabs since `start` (a local
    datetime), from the cloud-usage log."""
    total = 0.0
    for e in read_cloud_usage():
        if e.get("provider") != "elevenlabs":
            continue
        ts_str = _local_timestamp(e.get("timestamp"))
        if ts_str is not None and datetime.fromisoformat(ts_str) >= start:
            total += _number(e.get("audio_seconds"), 0.0, float)
    return round(total, 1)


# =============================================================================
#  APP ICONS  -  pull the icon out of each .exe the user has dictated into,
#  so the dashboard can render a per-app breakdown with real logos.
#
#  Why right here, in the dashboard process?
#    - It is a UI feature - app.py would just dump bytes for nothing if
#      the dashboard never opened.
#    - Extracting an icon takes a few ms per .exe. Doing it at dictation
#      time would add latency to every hotkey press; doing it once at
#      dashboard open is fine.
#
#  Output format: data: URIs (base64 PNG). The dashboard's HTML is loaded
#  from a string, not from disk, so file:// references would not resolve -
#  inlining the bytes is the one approach that works in every WebView2
#  build. Cached for the dashboard process's lifetime.
# =============================================================================

# {exe_path -> data: URI or None}. None entries mean "we tried and failed" -
# so we don't retry every render.
_icon_cache = {}

# On-disk cache. Once we've ever successfully extracted an icon for an
# .exe, we keep the PNG here forever - so a Store app that was running
# when the dashboard first saw it keeps its icon even after it's closed.
APP_ICONS_DIR = storage.APP_ICONS_DIR

# Win32 argtypes/restypes for the GDI + shell calls we make below.
#
# Why this matters: on 64-bit Python, ctypes treats undeclared args as
# c_int (32-bit), so any HANDLE/HICON/HBITMAP pointer passed in would be
# silently truncated to 32 bits before reaching the Win32 call. The
# call usually still "succeeds" - just on a garbage pointer - and
# returns nonsense. Setting argtypes once at import time makes every
# subsequent call type-safe, which is the difference between getting a
# real icon and getting None for modern apps (their HICONs sit above
# the 32-bit boundary). This is the single most common ctypes-on-Windows
# bug; don't remove this block.
try:
    _u32 = ctypes.windll.user32
    _g32 = ctypes.windll.gdi32
    _s32 = ctypes.windll.shell32
    _k32 = ctypes.windll.kernel32

    _s32.ExtractIconExW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint,
    ]
    _s32.ExtractIconExW.restype = ctypes.c_uint

    _u32.DrawIconEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
    ]
    _u32.DrawIconEx.restype = ctypes.c_int

    _u32.DestroyIcon.argtypes = [ctypes.c_void_p]
    _u32.DestroyIcon.restype  = ctypes.c_int
    _u32.GetDC.argtypes       = [ctypes.c_void_p]
    _u32.GetDC.restype        = ctypes.c_void_p
    _u32.ReleaseDC.argtypes   = [ctypes.c_void_p, ctypes.c_void_p]
    _u32.ReleaseDC.restype    = ctypes.c_int

    _g32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _g32.CreateCompatibleDC.restype  = ctypes.c_void_p
    _g32.DeleteDC.argtypes           = [ctypes.c_void_p]
    _g32.DeleteDC.restype            = ctypes.c_int
    _g32.CreateDIBSection.argtypes   = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint,
    ]
    _g32.CreateDIBSection.restype    = ctypes.c_void_p
    _g32.SelectObject.argtypes       = [ctypes.c_void_p, ctypes.c_void_p]
    _g32.SelectObject.restype        = ctypes.c_void_p
    _g32.DeleteObject.argtypes       = [ctypes.c_void_p]
    _g32.DeleteObject.restype        = ctypes.c_int

    # The Store-app fallback chain needs to enumerate running windows
    # and resolve each one's owning process to a path.
    _u32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _u32.EnumWindows.restype  = ctypes.c_int
    _u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _u32.IsWindowVisible.restype  = ctypes.c_int
    _u32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
    ]
    _u32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    _u32.SendMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _u32.SendMessageW.restype = ctypes.c_void_p
    _u32.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p),
    ]
    _u32.SendMessageTimeoutW.restype = ctypes.c_void_p
    # GetClassLongPtrW only exists on 64-bit; on 32-bit Python it's
    # exported as GetClassLongW. Pick whichever is present.
    _getclasslong = getattr(_u32, "GetClassLongPtrW", None) or _u32.GetClassLongW
    _getclasslong.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _getclasslong.restype  = ctypes.c_void_p

    _k32.OpenProcess.argtypes = [
        ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong,
    ]
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype  = ctypes.c_int
    _k32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong),
    ]
    _k32.QueryFullProcessImageNameW.restype = ctypes.c_bool
except Exception:
    _u32 = _g32 = _s32 = _k32 = None
    _getclasslong = None


# Asking another app's window for its icon is a message to THAT app. A hung
# app never answers a plain SendMessage - and the dashboard would hang with
# it on "Loading…". So the question has a deadline.
_SMTO_BLOCK, _SMTO_ABORTIFHUNG = 0x0001, 0x0002
ICON_QUERY_TIMEOUT_MS = 100   # a responsive window answers in well under 1 ms


def _query_icon(hwnd, which):
    """Ask a window for its icon (WM_GETICON) without ever hanging: a hung
    app, or one that doesn't answer within ICON_QUERY_TIMEOUT_MS, gives 0."""
    result = ctypes.c_void_p()
    ok = _u32.SendMessageTimeoutW(hwnd, 0x007F, which, 0,
                                  _SMTO_BLOCK | _SMTO_ABORTIFHUNG,
                                  ICON_QUERY_TIMEOUT_MS, ctypes.byref(result))
    return (result.value or 0) if ok else 0


# A standalone copy of BITMAPINFOHEADER, used to ask CreateDIBSection
# for a 32-bit top-down BGRA buffer. Defined at module level so the
# extraction function isn't redeclaring it on every call.
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",          ctypes.c_ulong),
        ("biWidth",         ctypes.c_long),
        ("biHeight",        ctypes.c_long),
        ("biPlanes",        ctypes.c_ushort),
        ("biBitCount",      ctypes.c_ushort),
        ("biCompression",   ctypes.c_ulong),
        ("biSizeImage",     ctypes.c_ulong),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed",       ctypes.c_ulong),
        ("biClrImportant",  ctypes.c_ulong),
    ]


def _icon_cache_path(exe_path):
    """Stable on-disk filename for an exe's cached icon PNG. We hash
    the path so it survives Unicode / long-path / drive-letter quirks
    that would tangle a humanized filename."""
    safe = hashlib.sha1(exe_path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(APP_ICONS_DIR, safe + ".png")


def _read_cached_icon_uri(exe_path):
    """If we've already cached a PNG for this exe, return it as a
    data: URI ready to embed. Returns None on miss or read error."""
    path = _icon_cache_path(exe_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    except OSError:
        return None


def _write_icon_cache(exe_path, png_bytes):
    """Save PNG bytes to the on-disk cache. Silently no-ops on any
    filesystem error - the data URI was already returned to the page,
    so a missing cache just means we re-extract next time."""
    try:
        os.makedirs(APP_ICONS_DIR, exist_ok=True)
        with open(_icon_cache_path(exe_path), "wb") as f:
            f.write(png_bytes)
    except OSError:
        pass


def _store_package_id(path):
    """
    For a Microsoft Store packaged-app .exe path of the form

        C:\\Program Files\\WindowsApps\\Name_Version_Arch__PublisherHash\\sub\\exe

    return a tuple (name, arch, publisher_hash, relative_subpath) that
    stays the same across app updates - the version part is the bit
    that changes when an app upgrades, and the rest identifies the
    package. Returns None for non-store paths.

    Why we need this: when Claude updates from 1.8555 to 1.9255, the
    old log entries still point at the 1.8555 path. Without this
    identity-based match, the icon fallback wouldn't recognize the
    1.9255 running window as the same app and would give up.
    """
    norm = path.replace("/", "\\")
    parts = norm.split("\\")
    try:
        idx = next(i for i, s in enumerate(parts) if s.lower() == "windowsapps")
    except StopIteration:
        return None
    if idx + 1 >= len(parts):
        return None
    pkg = parts[idx + 1]
    # "Name_Version_Arch__PublisherHash" - the publisher hash is set off
    # by a DOUBLE underscore, so we split on that first.
    if "__" not in pkg:
        return None
    main_part, pub_hash = pkg.rsplit("__", 1)
    pieces = main_part.rsplit("_", 2)
    if len(pieces) != 3:
        return None
    name, _version, arch = pieces
    relpath = "\\".join(parts[idx + 2:]).lower()
    return (name.lower(), arch.lower(), pub_hash.lower(), relpath)


def _find_hicon_via_running_window(exe_path):
    """
    Walk every visible top-level window, find one whose owning process
    has the same .exe path, and ask THAT WINDOW for its icon.

    This is the fallback for Microsoft Store / packaged apps: their
    .exe in C:\\Program Files\\WindowsApps has zero icon resources
    (the icon lives in the package manifest, which is ACL-locked from
    normal processes). The running window, however, knows its own
    icon - that's what Windows shows in Alt+Tab and the taskbar - and
    will hand it to us via WM_GETICON.

    Matching is two-tiered: exact path first, then (for Store apps)
    by stable package identity so an old log entry still matches the
    same app after a version bump.

    Caller does NOT own the returned HICON: WM_GETICON / class icons
    belong to the window. Calling DestroyIcon on them is a bug.
    """
    if _u32 is None or _k32 is None:
        return 0

    exe_path_lower    = exe_path.lower()
    target_package_id = _store_package_id(exe_path)
    found = [0]   # box so the inner callback can write through

    # WNDENUMPROC signature: BOOL CALLBACK(HWND, LPARAM)
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
    )

    # (WM_GETICON itself is sent by _query_icon, with a timeout.)
    ICON_SMALL   = 0
    ICON_BIG     = 1
    ICON_SMALL2  = 2
    GCLP_HICON   = -14
    GCLP_HICONSM = -34
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def callback(hwnd, _lparam):
        try:
            if not _u32.IsWindowVisible(hwnd):
                return 1                    # keep iterating
            pid = ctypes.c_ulong(0)
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == 0:
                return 1
            ph = _k32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value,
            )
            if not ph:
                return 1
            try:
                buf = ctypes.create_unicode_buffer(1024)
                sz  = ctypes.c_ulong(len(buf))
                if not _k32.QueryFullProcessImageNameW(
                    ph, 0, buf, ctypes.byref(sz),
                ):
                    return 1
                running_path = buf.value
            finally:
                _k32.CloseHandle(ph)

            # Exact path match wins outright.
            if running_path.lower() != exe_path_lower:
                # Otherwise: if BOTH paths are Store packages with the
                # same identity (only the version differs), still treat
                # them as a match.
                if target_package_id is None:
                    return 1
                running_id = _store_package_id(running_path)
                if running_id != target_package_id:
                    return 1

            # Match: ask the window for its icon. Big first - that's
            # what's used in Alt+Tab and gives us the best source pixels
            # to downscale from.
            for which in (ICON_BIG, ICON_SMALL2, ICON_SMALL):
                hicon = _query_icon(hwnd, which)
                if hicon:
                    found[0] = hicon
                    return 0                # stop enumeration
            # No window-level icon set - try the WINDOW CLASS's icon,
            # which is the per-app default Windows uses when WM_GETICON
            # returns nothing.
            if _getclasslong is not None:
                for gcl in (GCLP_HICON, GCLP_HICONSM):
                    hicon = _getclasslong(hwnd, gcl)
                    if hicon:
                        found[0] = hicon
                        return 0
            return 1
        except Exception:
            return 1

    try:
        _u32.EnumWindows(EnumWindowsProc(callback), 0)
    except Exception:
        return 0
    return found[0]


def extract_icon_data_uri(exe_path, size=48):
    """
    Return a data: URI containing the app's icon as a `size`x`size` PNG.

    Resolution order:
      1. On-disk cache (instant - we've already extracted this exe).
      2. ExtractIconExW on the .exe (works for normal Win32/Electron apps).
      3. Find one of the .exe's running windows and ask via WM_GETICON
         (the Store-app fallback - works as long as the app is open).

    A successful extraction is persisted to disk so future dashboard
    opens never hit step 3, even when the app is no longer running.
    """
    if not exe_path or Image is None:
        return None
    if _u32 is None or _g32 is None or _s32 is None:
        return None

    # Step 1: disk cache. Works even if the .exe has since been deleted
    # or moved (e.g., a Store app updated and its old versioned path
    # no longer exists). This is the only step that can answer for
    # those stale paths, so it must come first.
    cached = _read_cached_icon_uri(exe_path)
    if cached:
        return cached

    try:
        # Step 2: pull the primary icon resource directly out of the .exe.
        # We skip this if the file no longer exists - extraction would
        # fail anyway and the os.path.exists() check is much cheaper than
        # a failed Win32 round-trip.
        large = (ctypes.c_void_p * 1)()
        small = (ctypes.c_void_p * 1)()
        hicon = 0
        we_own_extracted = False
        if os.path.exists(exe_path):
            nicons = _s32.ExtractIconExW(exe_path, 0, large, small, 1)
            # nicons may come back as 0 (Store apps - .exe carries none)
            # or 0xFFFFFFFF (-1 cast unsigned: "no icon resources"). In
            # both cases we want to fall through to the window scan.
            hicon = (large[0] or small[0]) if nicons not in (0, 0xFFFFFFFF) else 0
            we_own_extracted = bool(hicon)

        # Step 3: ask a running window owned by the same .exe.
        if not hicon:
            hicon = _find_hicon_via_running_window(exe_path)
            # Window-owned HICONs are NOT ours to destroy.

        if not hicon:
            return None

        try:
            img = _hicon_to_pil(hicon, size)
            if img is None:
                return None
            buf = io.BytesIO()
            img.save(buf, "PNG")
            png_bytes = buf.getvalue()
            # Persist to disk so future opens skip both Win32 fallbacks.
            _write_icon_cache(exe_path, png_bytes)
            return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
        finally:
            # Only destroy the HICONs WE allocated - the ones from
            # ExtractIconExW. Window-owned HICONs would crash the
            # other process if we destroyed them.
            if we_own_extracted:
                _u32.DestroyIcon(hicon)
                for hi in (large[0], small[0]):
                    if hi and hi != hicon:
                        _u32.DestroyIcon(hi)
    except Exception:
        return None


def _hicon_to_pil(hicon, size):
    """
    Render an HICON onto a freshly-created 32-bit ARGB DIB section and
    return it as a PIL RGBA Image. The DIB starts zeroed (transparent),
    DrawIconEx composites the icon onto it honoring the icon's own
    alpha + mask, and we read the pixels straight out of the section's
    backing buffer - no GetDIBits round-trip needed.
    """
    hdc_screen = _u32.GetDC(None)
    if not hdc_screen:
        return None
    try:
        hdc_mem = _g32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            return None
        try:
            bmi = _BITMAPINFOHEADER()
            bmi.biSize        = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.biWidth       = size
            bmi.biHeight      = -size           # negative -> top-down rows
            bmi.biPlanes      = 1
            bmi.biBitCount    = 32
            bmi.biCompression = 0               # BI_RGB

            bits_ptr = ctypes.c_void_p()
            hbmp = _g32.CreateDIBSection(
                hdc_mem, ctypes.byref(bmi), 0,  # DIB_RGB_COLORS
                ctypes.byref(bits_ptr), None, 0,
            )
            if not hbmp or not bits_ptr.value:
                return None
            try:
                old_bmp = _g32.SelectObject(hdc_mem, hbmp)
                try:
                    # The DIB was created zero-initialized, which is what we
                    # want as the transparent backdrop. DrawIconEx with
                    # DI_NORMAL composites color + mask + alpha onto it.
                    DI_NORMAL = 0x0003
                    ok = _u32.DrawIconEx(
                        hdc_mem, 0, 0, hicon, size, size,
                        0, None, DI_NORMAL,
                    )
                    if not ok:
                        return None
                    # Read the rendered pixels straight out of the DIB's
                    # backing buffer. size*size*4 bytes of BGRA.
                    pixels = ctypes.string_at(bits_ptr, size * size * 4)
                    return Image.frombuffer(
                        "RGBA", (size, size), pixels, "raw", "BGRA", 0, 1,
                    )
                finally:
                    _g32.SelectObject(hdc_mem, old_bmp)
            finally:
                _g32.DeleteObject(hbmp)
        finally:
            _g32.DeleteDC(hdc_mem)
    finally:
        _u32.ReleaseDC(None, hdc_screen)


def build_app_icons(entries):
    """
    Return {exe_path -> data: URI} for every unique app_exe in the log.
    Cached across calls in the same dashboard process so repeated boots
    of the same page don't re-extract every icon.
    """
    icons = {}
    seen = set()
    for e in entries:
        path = e.get("app_exe")
        if not path or path in seen:
            continue
        seen.add(path)
        if path in _icon_cache:
            uri = _icon_cache[path]
        else:
            uri = extract_icon_data_uri(path)
            _icon_cache[path] = uri      # cache None too, so we skip retries
        if uri:
            icons[path] = uri
    return icons


def list_input_devices():
    """Microphone names for the Settings dropdown: only mics that can really
    record, each once (see devices.py). Empty when there's no audio backend -
    the dropdown then just shows the default."""
    return devices.list_input_devices()


# =============================================================================
#  RELOAD SIGNAL  -  tell the running app.py to pick up a fresh config.
# =============================================================================

def signal_reload():
    """Best-effort: tell the running Scribe to re-read its settings and
    vocabulary (over its control pipe - see instance.py). If Scribe isn't
    running (the dashboard opened on its own), nothing happens."""
    instance.send({"cmd": "reload-config"})


def _window_hwnd():
    """The dashboard window's handle: pywebview's native form if it has one,
    else the window titled "Scribe"."""
    try:
        return int(webview.windows[0].native.Handle.ToInt64())
    except Exception:
        pass
    try:
        return ctypes.windll.user32.FindWindowW(None, "Scribe") or None
    except Exception:
        return None


# =============================================================================
#  JS BRIDGE  -  the methods JavaScript can call via window.pywebview.api.
# =============================================================================

class JsApi:
    """The Python side of the page. Its PAGE_FUNCTIONS (below) are exposed
    to the page as window.pywebview.api.* - each a JS-callable returning a
    Promise. Keep methods quick - long work would block the renderer."""

    def __init__(self, welcome=False):
        # True when app.py opened us for a first run (dashboard.py --welcome):
        # the page then shows the welcome instead of the dashboard.
        self.welcome = welcome
        # When this window's welcome finished setup (to heal a lost message).
        self._setup_finished_at = None
        # The app icons this window's page already has (polls send only new ones).
        self._icons_sent = set()

    def _icons_for_page(self, entries, fresh=False):
        """The app icons the page doesn't have yet (all of them on a fresh
        load) - it merges them into what it has. Icons are ~5 KB each, so a
        poll after every dictation shouldn't resend them all."""
        icons = build_app_icons(entries)
        if fresh:
            self._icons_sent = set()
        new = {exe: uri for exe, uri in icons.items() if exe not in self._icons_sent}
        self._icons_sent.update(new)
        return new

    def get_initial_data(self):
        """The single payload the page asks for on boot. Cheaper to
        bundle these reads into one round-trip than to make JavaScript
        fetch them one-by-one."""
        # The signature FIRST: a dictation landing after it is then seen by
        # the next poll instead of being skipped.
        log_sig = _log_signature()
        if HISTORY.refresh()[0] == "unreadable":
            log_sig = None                    # couldn't read: the first poll retries
        snap = HISTORY.snapshot_since(None, None)
        entries = snap["entries"]
        raw_vocab = _read_vocab_raw()
        pairs = sorted(raw_vocab["corrections"].items(),
                       key=lambda p: len(p[0]), reverse=True)
        config = read_config()
        # An older version may have saved MME's truncated mic name; show (and
        # later save) the full name it resolves to.
        full = devices.canonical_name(config.get("mic_device"))
        if full:
            config = dict(config, mic_device=full)
        return {
            "entries": entries,
            "history_gen": snap["history_gen"],
            "config":  page_config(config),
            "app_version": VERSION,
            # First run: the page shows the welcome instead of the dashboard.
            "welcome": self.welcome,
            "vocab":   {"terms": raw_vocab["terms"], "corrections": pairs,
                        "dismissed": raw_vocab["dismissed"]},
            "choices": {
                "hotkeys": HOTKEY_CHOICES,
                "models":  MODEL_CHOICES,
                "mics":    [MIC_DEFAULT_LABEL] + devices.list_input_devices(),
            },
            # Real icons for every app the user has dictated into, keyed
            # by the .exe path stored on each log entry. The page looks
            # them up when rendering the "By app" panel.
            "app_icons": self._icons_for_page(entries, fresh=True),
            # Groq free-tier usage counters (today vs. quota) for the
            # Insights "Cloud usage" panel.
            "cloud":   summarize_cloud_usage(),
            # Baseline signature for the poll loop. The page sends this
            # value back to poll_updates so the bridge can short-circuit
            # the no-change case with a single stat().
            "log_sig":  log_sig,
            # Anything storage recovered while loading the above (a damaged
            # settings file restored, say) - shown once as a gentle toast.
            # Taken LAST, after every read that could produce one.
            "notices":  _take_notices(),
        }

    def poll_updates(self, since, gen=None, count=None):
        """Live-update endpoint: the dashboard polls this every couple of
        seconds so a new dictation lands in History, the rail stats and
        the Insights page without the user needing to refresh.

        Unchanged logs cost two stat() calls (`since` is the signature this
        method last returned, or None on the first poll). Otherwise the page
        gets exactly what it's missing - it tells us which history
        generation it has (`gen`) and how many entries (`count`): "appended"
        (just the new ones) or "entries" with reset=True (the file was
        rewritten) - plus cloud usage. If the history
        couldn't be read just now, nothing changes and the page's signature
        is handed back, so the next poll tries again."""
        sig = _log_signature()
        if (isinstance(since, list) and len(since) == len(sig)
                and all(float(a) == float(b) for a, b in zip(since, sig))):
            return {"changed": False, "log_sig": sig}
        if HISTORY.refresh()[0] == "unreadable":
            return {"changed": False, "log_sig": since}
        snap = HISTORY.snapshot_since(gen, count)
        out = {
            "changed":   True,
            "log_sig":   sig,
            "app_icons": self._icons_for_page(HISTORY.entries_copy()),
            "cloud":     summarize_cloud_usage(),
        }
        out.update(snap)
        return out

    def save_settings(self, payload):
        """
        Validate and save settings from the Settings page, then ping the
        running app so they apply now. Besides config.json keys the page may
        send: groq_api_key / elevenlabs_api_key (a NEW key, already checked
        by the page), remove_groq_key / remove_elevenlabs_key, and
        start_at_login (the registry). Raises
        storage.StorageError - which the page shows - instead of ever
        reporting a failed save as saved. Returns page_config() plus
        key_storage ("vault" / "file" / None) so the page can warn when the
        key had to go into the settings file.
        """
        payload = dict(payload or {})
        # Each service's key: a new one to store, or a removal. Keys never
        # go into config.json this way - the keystore decides where they live.
        new_keys, removals = {}, set()
        for service, (_target, field) in keystore.SERVICES.items():
            new_keys[service] = (payload.pop(field, "") or "").strip()
            if payload.pop(f"remove_{service}_key", False):
                removals.add(service)
            payload.pop(f"{service}_key_hint", None)
        start_at_login = payload.pop("start_at_login", None)
        current = read_config()
        try:
            use_cloud = storage.clean_config_value(
                "use_cloud", payload.get("use_cloud", current["use_cloud"]))
            provider = storage.clean_config_value(
                "cloud_provider", payload.get("cloud_provider", current["cloud_provider"]))
        except (ValueError, TypeError):
            raise storage.StorageError("A cloud setting has an invalid value.")

        def has_key(service):
            return bool(new_keys[service]) or (
                service not in removals and keystore.resolve_key(current, service))
        if use_cloud and not has_key(provider):
            raise storage.StorageError(
                "Add an ElevenLabs API key to use ElevenLabs." if provider == "elevenlabs"
                else "Add a Groq API key to use the cloud.")
        if not use_cloud:
            payload["local_model"] = True     # local is then the only way to transcribe
        cfg = storage.save_config_changes(payload)
        # The settings are saved. The key and the sign-in switch live
        # elsewhere (Credential Manager, the registry); if one of them
        # fails, that's a warning next to "Saved" - never "Couldn't save".
        stored, warnings = None, []
        for service in keystore.SERVICES:
            try:
                if new_keys[service]:
                    stored = keystore.set_key(new_keys[service], service)
                elif service in removals:
                    keystore.delete_key(service)
            except (storage.StorageError, ValueError) as exc:
                warnings.append(f"Your API key couldn't be saved ({exc}).")
        if start_at_login is not None and autostart.available():
            try:
                autostart.set_enabled(bool(start_at_login))
            except storage.StorageError as exc:
                warnings.append(str(exc))
        signal_reload()                       # always: what WAS saved applies now
        keys_changed = any(new_keys.values()) or bool(removals)
        out = page_config(read_config() if keys_changed else cfg)
        # "file" only matters when the vault was really tried and refused.
        out["key_storage"] = stored if keystore.uses_vault() else None
        out["warnings"] = warnings
        return out

    def check_groq_key(self, key):
        """The welcome's and Settings' "Check key" (see check_groq_key)."""
        return check_groq_key(key)

    def open_groq_keys_page(self):
        """'Get a free key': open Groq's key page in the browser. A fixed
        URL - the page can't make us open anything else."""
        webbrowser.open(GROQ_KEYS_URL)

    def read_clipboard_key(self):
        """
        The welcome's Paste button (the window has no right-click menu). The
        clipboard's text if it looks like an API key - one piece of text, no
        spaces, 16-300 plain characters - else "": whatever else happens to
        be on the clipboard never reaches the page.
        """
        try:
            import pyperclip
            text = (pyperclip.paste() or "").strip()
        except Exception:
            return ""
        if 16 <= len(text) <= 300 and all(33 <= ord(c) < 127 for c in text):
            return text
        return ""

    def check_elevenlabs_key(self, key):
        """The welcome's and Settings' "Check key" for ElevenLabs: opens a
        session and closes it without audio (free) - see
        elevenlabs_stream.check_key."""
        return elevenlabs_stream.check_key(key)

    def open_elevenlabs_keys_page(self):
        """'Get a key': ElevenLabs' API-keys page. A fixed URL."""
        webbrowser.open(elevenlabs_stream.KEYS_URL)

    def check_for_updates(self):
        """Settings -> About: is a newer Scribe out? (updates.check - one
        small request to GitHub; never raises.)"""
        return updates.check()

    def open_release_page(self):
        """'What's new': the newest release's page on GitHub. A fixed URL."""
        webbrowser.open(updates.RELEASES_PAGE)

    def install_update(self):
        """
        'Update now': start the updater in a console window of its own (you
        see what it does) - it asks Scribe to quit, brings in the new version
        and starts Scribe again - then close this window, whose files are
        about to be replaced. Raises OSError (shown on the page) if the
        updater can't be started.
        """
        folder = os.path.dirname(sys.executable)
        python = os.path.join(folder, "python.exe")      # a console, not pythonw
        if not os.path.exists(python):
            python = sys.executable
        subprocess.Popen([python, os.path.join(APP_DIR, "updates.py"), "--install"],
                         cwd=APP_DIR, creationflags=subprocess.CREATE_NEW_CONSOLE)
        threading.Timer(1.0, self.close_window).start()
        return True

    def get_elevenlabs_usage(self):
        """
        The Home meter's numbers: the account's credits used this billing
        month (when the key may read them) plus the time Scribe streamed
        since the month began - and, once credits-per-hour is known, about
        how many hours are left. {"status": "none"} when there's no key.
        """
        key = keystore.resolve_key(read_config(), "elevenlabs")
        if not key:
            return {"status": "none"}
        acct = elevenlabs_stream.get_usage(key)
        out = dict(acct)
        out["dictated_seconds"] = stream_seconds_since(cycle_start(acct.get("resets_at")))
        per_hour = elevenlabs_stream.CREDITS_PER_REALTIME_HOUR
        if acct.get("status") == "ok" and per_hour:
            out["hours_left"] = max(0, acct["limit"] - acct["used"]) / per_hour
        return out

    def finish_setup(self, choices):
        """
        The welcome's "Finish setup": save the user's choices, store the key
        (cloud), set start-at-sign-in, then tell Scribe ("setup-done") - in
        that order, so Scribe finds everything in place. Raises
        storage.StorageError (shown on the page) if the settings can't be
        saved; nothing is saved if the choices are refused.
        """
        c = dict(choices or {})
        use_cloud = bool(c.get("use_cloud"))
        try:
            provider = storage.clean_config_value(
                "cloud_provider", c.get("cloud_provider") or "groq")
        except (ValueError, TypeError):
            raise storage.StorageError("Choose Groq or ElevenLabs.")
        _target, field = keystore.SERVICES[provider]
        key = (c.get(field) or "").strip()
        if use_cloud and not key:
            raise storage.StorageError(
                "Add an ElevenLabs API key to use ElevenLabs." if provider == "elevenlabs"
                else "Add a Groq API key to use the cloud.")
        mic = c.get("mic_device")
        cfg = storage.save_config_changes({
            "use_cloud": use_cloud,
            "cloud_provider": provider,
            # Local mode IS the local model; for cloud it's the backup box.
            "local_model": (not use_cloud) or bool(c.get("local_model", True)),
            "model_size": c.get("model_size") or storage.DEFAULT_CONFIG["model_size"],
            "hotkey": c.get("hotkey") or storage.DEFAULT_CONFIG["hotkey"],
            "mic_device": None if mic in (None, "", MIC_DEFAULT_LABEL) else mic,
            # "On this PC" promises private and offline: no daily request to
            # GitHub either, unless turned on in Settings -> About.
            "check_updates": use_cloud,
        })
        warnings = []
        if use_cloud and keystore.set_key(key, provider) == "file" and keystore.uses_vault():
            warnings.append(keystore.KEY_STORE_FAILED["message"])
        if autostart.available():
            try:
                autostart.set_enabled(bool(c.get("start_at_login", True)))
            except storage.StorageError as exc:
                warnings.append(str(exc))
        self._setup_finished_at = time.monotonic()
        running = instance.send({"cmd": "setup-done"})
        return {"ok": True, "scribe_running": running, "warnings": warnings,
                "config": page_config(cfg)}

    def get_setup_status(self):
        """Scribe's live status for the welcome's progress and Settings'
        model line: {"running": False} if Scribe isn't answering."""
        reply = instance.request({"cmd": "status"}, timeout=1.0)
        if reply is None:
            return {"running": False}
        # We finished setup but Scribe still says it isn't: the message was
        # lost (or Scribe was restarting) - say it again, every 2 s at most.
        if (reply.get("setup_complete") is False and self._setup_finished_at is not None
                and time.monotonic() - self._setup_finished_at > 2):
            self._setup_finished_at = time.monotonic()
            instance.send({"cmd": "setup-done"})
        return dict(reply, running=True)

    def retry_model(self):
        """Retry a failed model download (welcome / Settings)."""
        return instance.send({"cmd": "retry-model"})

    def mark_milestones_seen(self, keys):
        """Add the given milestone keys to the persisted 'seen' list so
        confetti only ever fires once per milestone. We deliberately do
        NOT call signal_reload here: app.py knows nothing about
        milestones, so there is no in-memory state to refresh."""
        seen = set(read_config().get("seen_milestones") or [])
        seen.update(k for k in (keys or []) if isinstance(k, str))
        cfg = storage.save_config_changes({"seen_milestones": sorted(seen)})
        return cfg["seen_milestones"]

    # --- Vocabulary editing -------------------------------------------
    # The Dictionary page writes the catalog through these. Each does a
    # read-modify-write of vocabulary.json (re-reading first so a manual
    # edit is never clobbered) and pings app.py to reload it live, then
    # returns the fresh {vocab} so the page re-renders without a refetch.

    def _vocab_state(self):
        """The current catalog, for the page."""
        HISTORY.refresh()
        return {"vocab": read_vocab()}

    def _save_vocab(self, raw):
        _write_vocab_raw(raw)   # raises -> the page shows "Could not save"
        signal_reload()         # app.py re-reads vocabulary.json -> live next dictation

    def add_vocab_term(self, term):
        """Add a word Scribe should know. The same word with different
        capitals REPLACES the old spelling (your latest spelling wins). The
        reply's "result" says which: "added", "updated" or "exists"."""
        term = (term or "").strip()
        raw = _read_vocab_raw()
        result = "exists"
        if term:
            same = [t for t in raw["terms"] if str(t).strip().lower() == term.lower()]
            if not same:
                raw["terms"].append(term)
                result = "added"
            elif same[0] != term:
                raw["terms"] = [term if str(t).strip().lower() == term.lower() else t
                                for t in raw["terms"]]
                result = "updated"
        if result != "exists":
            self._save_vocab(raw)
        state = self._vocab_state()
        state["result"] = result
        return state

    def add_vocab_correction(self, wrong, right):
        wrong = (wrong or "").strip()
        right = (right or "").strip()
        raw = _read_vocab_raw()
        if wrong and right:
            raw["corrections"][wrong] = right
            # Also bias Whisper toward the right spelling (prevention), the
            # way the catalog's two layers are meant to work together - and a
            # term already there in other capitals takes this spelling.
            same = [i for i, t in enumerate(raw["terms"])
                    if str(t).strip().lower() == right.lower()]
            if same:
                raw["terms"][same[0]] = right
            else:
                raw["terms"].append(right)
        self._save_vocab(raw)
        return self._vocab_state()

    def remove_vocab_term(self, term):
        key = (term or "").strip().lower()
        raw = _read_vocab_raw()
        raw["terms"] = [t for t in raw["terms"]
                        if str(t).strip().lower() != key]
        self._save_vocab(raw)
        return self._vocab_state()

    def remove_vocab_correction(self, wrong):
        key = (wrong or "").strip().lower()
        raw = _read_vocab_raw()
        raw["corrections"] = {w: r for w, r in raw["corrections"].items()
                              if str(w).strip().lower() != key}
        self._save_vocab(raw)
        return self._vocab_state()

    # --- Window controls ----------------------------------------------
    # The dashboard is frameless (no OS title bar), so the page draws its
    # own minimize/maximize/close buttons. The JS click handlers call
    # these methods, which delegate to pywebview's Window API.

    def minimize_window(self):
        if webview.windows:
            try:
                webview.windows[0].minimize()
            except Exception:
                pass

    def toggle_maximize(self):
        """Maximize, or restore if already maximized - judged by the window
        itself (IsZoomed): pywebview 6's Window.state is its shared-state
        dict, not the window's size state, so asking it never said
        "maximized" and the button could only ever maximize."""
        if not webview.windows:
            return
        w = webview.windows[0]
        try:
            hwnd = _window_hwnd()
            if hwnd and ctypes.windll.user32.IsZoomed(hwnd):
                w.restore()
            else:
                w.maximize()
        except Exception:
            pass

    def close_window(self):
        if webview.windows:
            try:
                webview.windows[0].destroy()
            except Exception:
                pass

    # --- Window drag --------------------------------------------------
    # pywebview's CSS '-webkit-app-region: drag' is not honored by Edge
    # WebView2, and built-in easy_drag only works on non-resizable
    # frameless windows. So the dashboard implements drag itself: JS
    # tracks mouse position on the titlebar, asks for the initial window
    # position once via get_window_position, and pushes new coordinates
    # back through move_window once per animation frame.

    def get_window_position(self):
        """Return [x, y] of the window's current top-left corner in screen
        pixels. We bypass pywebview's cached Window.x/.y (which can lag
        the actual position) and go straight to GetWindowRect via ctypes."""
        try:
            hwnd = _window_hwnd()
            if not hwnd:
                return None
            rect = (ctypes.c_long * 4)()   # left, top, right, bottom
            ctypes.windll.user32.GetWindowRect(hwnd, rect)
            return [int(rect[0]), int(rect[1])]
        except Exception:
            return None

    def move_window(self, x, y):
        """Reposition the window. Called many times per drag - kept short
        so JS isn't blocked. pywebview's move() handles the OS-specific
        call; we just round to ints and shrug off the rare race where the
        window doesn't exist yet."""
        if not webview.windows:
            return
        try:
            webview.windows[0].move(int(x), int(y))
        except Exception:
            pass


# The ONLY Python functions the page can call. They are handed to pywebview
# one by one (window.expose) instead of as a js_api object: pywebview finds
# an exposed function by its exact name, while a js_api object let any
# dotted name ("save_settings.__func__.__globals__.clear") walk from a
# method into the rest of Python. If the page ever runs injected script,
# this list is all it can reach.
PAGE_FUNCTIONS = (
    "get_initial_data", "poll_updates", "save_settings", "check_groq_key",
    "open_groq_keys_page", "check_elevenlabs_key", "open_elevenlabs_keys_page",
    "get_elevenlabs_usage", "check_for_updates", "open_release_page", "install_update",
    "finish_setup", "get_setup_status", "retry_model", "read_clipboard_key",
    "mark_milestones_seen", "add_vocab_term", "add_vocab_correction",
    "remove_vocab_term", "remove_vocab_correction",
    "minimize_window", "toggle_maximize", "close_window",
    "get_window_position", "move_window",
)


def page_functions(api):
    """The bound methods named in PAGE_FUNCTIONS, in order."""
    return [getattr(api, name) for name in PAGE_FUNCTIONS]


# =============================================================================
#  WINDOW ICON  -  set the Scribe icon on the dashboard window so the
#  taskbar button and Alt+Tab thumbnail show our brand, not pythonw.exe's.
# =============================================================================

# Win32 constants for SendMessage(WM_SETICON, ...).
_WM_SETICON      = 0x0080
_ICON_SMALL      = 0
_ICON_BIG        = 1
_IMAGE_ICON      = 1
_LR_LOADFROMFILE = 0x00000010
_LR_DEFAULTSIZE  = 0x00000040


def _set_window_icon(window):
    """Find the dashboard's HWND by title and stamp our scribe.ico on it
    (both small and big variants). Called from the window's 'shown' event,
    so the HWND is guaranteed to exist by the time we look it up.

    Why FindWindowW instead of pywebview internals? The internal handle
    attribute name has shifted across pywebview versions; matching by
    window title is the one stable API across them."""
    if not os.path.exists(ICO_FILE):
        return
    try:
        user32 = ctypes.windll.user32
        # The window's title is set in create_window() below. It's the
        # search key; once frameless, it's invisible but still the HWND
        # caption.
        hwnd = user32.FindWindowW(None, "Scribe")
        if not hwnd:
            return
        hicon = user32.LoadImageW(
            None, ICO_FILE, _IMAGE_ICON, 0, 0,
            _LR_LOADFROMFILE | _LR_DEFAULTSIZE,
        )
        if not hicon:
            return
        user32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, hicon)
        user32.SendMessageW(hwnd, _WM_SETICON, _ICON_BIG,   hicon)
    except Exception as exc:
        print(f"[ICON] failed to set window icon: {exc!r}")


# =============================================================================
#  WINDOW  -  build the pywebview window and hand it the page.
# =============================================================================

# =============================================================================
#  WEBVIEW2 CHECK  -  the dashboard's page runs in Microsoft Edge WebView2.
#  Without it pywebview silently falls back to the old IE engine, which can't
#  run the page at all (a dead window with no working close button). So we
#  check first - Microsoft's documented way: the runtime's version value in
#  the registry - and explain, instead of showing a broken window.
# =============================================================================

_WEBVIEW2_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def webview2_installed():
    """True if the Edge WebView2 Runtime is installed (machine or user)."""
    try:
        import winreg
    except ImportError:
        return True                      # not Windows: let pywebview decide
    locations = [
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_ID}"),
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_ID}"),
        (winreg.HKEY_CURRENT_USER,
         rf"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_ID}"),
    ]
    for hive, path in locations:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _explain_missing_webview2():
    """Native dialog: what's missing, and an offer to open the download page."""
    MB_YESNO, MB_ICONWARNING, IDYES = 0x4, 0x30, 6
    text = ("Scribe's dashboard needs the Microsoft Edge WebView2 Runtime, "
            "which isn't installed on this PC.\n\nDictation keeps working "
            "without it. Open the download page now?")
    try:
        answer = ctypes.windll.user32.MessageBoxW(
            None, text, "Scribe dashboard", MB_YESNO | MB_ICONWARNING)
    except Exception:
        answer = None
    if answer == IDYES:
        webbrowser.open(WEBVIEW2_DOWNLOAD_URL)


def main():
    # Any error that escapes goes to the shared error log, and its one-line
    # reason is the last thing printed - app.py reads that line (from
    # dashboard_log.txt) to tell the user why the dashboard didn't open.
    def _crash(exc_type, exc, tb):
        storage.log_error("dashboard", exc.with_traceback(tb))
        print(f"{exc_type.__name__}: {exc}", flush=True)
    sys.excepthook = _crash

    if not webview2_installed():
        print("The Microsoft Edge WebView2 Runtime isn't installed.", flush=True)
        _explain_missing_webview2()
        sys.exit(2)
    api = JsApi(welcome="--welcome" in sys.argv[1:])

    # Read the HTML file's contents and pass them as `html=` rather than
    # using `url=file://`. Loading from a string sidesteps the file://
    # security restrictions some WebView2 builds apply (e.g. about:blank
    # being treated as the origin), so the JS bridge boots reliably.
    cfg = read_config()
    html = page_html(cfg)

    window = webview.create_window(
        title="Scribe",                 # used by FindWindowW + the taskbar
        html=html,
        width=1280,
        height=820,
        min_size=(1000, 680),
        background_color=window_background(cfg),  # before the page paints
        # Frameless: no OS title bar / chrome. The page draws its own
        # titlebar in HTML, which keeps the visual language fully under
        # our control (no Windows dark-mode title bar bleeding in).
        frameless=True,
        easy_drag=False,                # drag is handled by a JS region
    )

    # As soon as the window is shown, stamp it with our icon. The 'shown'
    # event fires after WebView2 has actually realized the HWND, so
    # FindWindowW is guaranteed to find it.
    # Exactly the page's functions - see PAGE_FUNCTIONS.
    window.expose(*page_functions(api))

    window.events.shown += lambda: _set_window_icon(window)
    _watch_minimize(window)

    # On Windows, prefer Edge WebView2 (Chromium-based) so the modern CSS
    # in dashboard.html renders cleanly. Falls back to MSHTML if WebView2
    # isn't installed - rare on Windows 11 since it's a system component.
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    main()
