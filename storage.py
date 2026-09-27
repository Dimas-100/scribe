"""
=============================================================================
 SCRIBE STORAGE - where your data lives, and how it is saved safely.
=============================================================================

 Both the tray app (app.py) and the dashboard (dashboard.py) read and write
 the same handful of files. This module is the ONE place that knows where
 those files are and how to touch them without ever corrupting or losing
 them:

   - WHERE: a per-user data folder, %APPDATA%\\Scribe - not the code folder.
     The code folder may be read-only (Program Files), gets replaced on an
     update, and is the folder people zip up to share. Your settings (with
     your API key), vocabulary and history do not belong there.
   - HOW:   every save writes a temp file first and then swaps it in with
     one atomic os.replace. A crash, a power cut or a killed process mid-
     save leaves either the old file or the new one - never half of each.

 It deliberately imports nothing heavy (no Whisper, no audio, no GUI), so
 the dashboard and the tests can use it freely.
=============================================================================
"""

import json
import os
import shutil
import threading
import time
import traceback
from datetime import datetime


# =============================================================================
#  WHERE THINGS LIVE
# =============================================================================

# The folder this file (and app.py) lives in - the code, not your data.
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_data_dir():
    """
    Pick the data folder. SCRIBE_DATA_DIR wins when set (the tests use it to
    run against a throwaway folder; it also allows a "portable" install).
    Otherwise %APPDATA%\\Scribe - the standard per-user spot on Windows. If
    APPDATA is somehow missing, fall back to the code folder (the old layout).
    """
    override = os.environ.get("SCRIBE_DATA_DIR")
    if override:
        return os.path.abspath(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "Scribe")
    return APP_DIR


DATA_DIR = _resolve_data_dir()
# True when running against an override folder. Migration (below) never runs
# then, so a test can never move your real files into its temp folder.
USING_OVERRIDE = bool(os.environ.get("SCRIBE_DATA_DIR"))

CONFIG_FILE     = os.path.join(DATA_DIR, "config.json")          # settings (+ API key)
VOCAB_FILE      = os.path.join(DATA_DIR, "vocabulary.json")      # your terms/corrections
LOG_FILE        = os.path.join(DATA_DIR, "dictation_log.jsonl")  # every dictation
CLOUD_USAGE_LOG = os.path.join(DATA_DIR, "cloud_usage.jsonl")    # today's cloud quota
ERROR_LOG       = os.path.join(DATA_DIR, "error_log.txt")        # what went wrong
DASHBOARD_LOG   = os.path.join(DATA_DIR, "dashboard_log.txt")    # the dashboard's output
APP_ICONS_DIR   = os.path.join(DATA_DIR, "app_icons")            # icon cache

# Cap the error log so a failure storm can't grow it without bound. Past this
# size, log_error keeps only the most recent half before appending.
ERROR_LOG_MAX_BYTES = 256 * 1024

# One lock for every read-modify-write in this process, so two threads (the
# dashboard runs each bridge call on its own thread) can't interleave.
_lock = threading.RLock()


class StorageError(Exception):
    """A save failed. str(exc) is a short, user-readable reason."""


def ensure_data_dir():
    """Create the data folder if it doesn't exist yet."""
    os.makedirs(DATA_DIR, exist_ok=True)


# =============================================================================
#  CRASH-SAFE WRITES
# =============================================================================

# How long to keep retrying when Windows refuses to replace a file because
# another handle has it open (the dashboard reading the log, an antivirus
# scan). Normally that clears within a few milliseconds.
REPLACE_RETRY_SECONDS = 1.0


def atomic_write_text(path, text):
    """
    Write `text` to `path` so that a crash can never leave a half-written
    file: write a temp file next to it, flush it to disk, then swap it in
    with os.replace (atomic on the same drive). Raises OSError if it still
    can't be written after retrying.
    """
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())    # on disk before we swap it in
        deadline = time.monotonic() + REPLACE_RETRY_SECONDS
        while True:
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
    finally:
        # Only still here if something failed before the swap.
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def atomic_write_json(path, obj):
    """atomic_write_text for a JSON document (pretty-printed, UTF-8)."""
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def read_json(path):
    """
    Read a JSON file and say how it went, as (status, data):
      ("missing", None)     - no such file
      ("ok", data)          - parsed fine
      ("corrupt", None)     - exists but isn't valid JSON / valid UTF-8
      ("unreadable", None)  - exists but couldn't be opened (locked, denied)
    utf-8-sig quietly accepts a byte-order mark, which Notepad and
    PowerShell's Out-File like to add.
    """
    if not os.path.exists(path):
        return ("missing", None)
    # Another program (an antivirus scan, a sync client) can hold the file
    # for a moment - keep trying briefly before calling it unreadable.
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    while True:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return ("ok", json.load(f))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ("corrupt", None)
        except FileNotFoundError:
            return ("missing", None)
        except OSError:
            if time.monotonic() >= deadline:
                return ("unreadable", None)
            time.sleep(0.05)


# =============================================================================
#  JSON LINES LOGS  -  one JSON object per line, append-only.
# =============================================================================

def append_jsonl(path, obj):
    """Append one entry as a single JSON line. Raises OSError on failure. If
    the file doesn't end with a newline (a hand edit), the entry starts on a
    new line anyway - never glued onto the last one."""
    line = json.dumps(obj) + "\n"
    with _lock:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    line = "\n" + line
        except OSError:
            pass                      # missing or empty file: nothing to fix
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)


def read_jsonl(path):
    """
    Every well-formed JSON object in the file, oldest first. Blank lines,
    malformed lines and lines that aren't objects are skipped, so one bad
    line can never break a reader. A missing file is just an empty list.
    """
    entries = []
    if not os.path.exists(path):
        return entries
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        pass
    return entries


def read_jsonl_from(path, offset):
    """
    The JSON objects in `path` from byte `offset` on, and the offset to
    continue from next time: (entries, new_offset). Only COMPLETE lines are
    read - a line still being written (no newline yet) is left for the next
    call. Malformed lines are skipped; a BOM at the start is ignored. A
    missing file is ([], 0); a file that exists but can't be read right now
    (another program holding it) is None - the caller must change nothing
    and try again later, never treat it as empty.
    """
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return [], 0
    except OSError:
        return None
    end = data.rfind(b"\n")
    last = data[end + 1:].strip()
    if last:
        # An unfinished last line: complete JSON means a hand edit that just
        # lacks its newline - take it too; anything else is still being
        # written (a line cut off mid-write never parses).
        try:
            if isinstance(json.loads(last.decode("utf-8", errors="replace")), dict):
                end = len(data) - 1
        except ValueError:
            pass
    if end < 0:
        return [], offset
    chunk = data[:end + 1]
    if offset == 0 and chunk.startswith(b"\xef\xbb\xbf"):
        chunk = chunk[3:]
    entries = []
    for raw in chunk.split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries, offset + end + 1


def rewrite_lines(path, transform):
    """
    Rewrite a line-based file under the lock: `transform(lines)` gets the
    current lines (with their newlines) and returns the new list, or None
    for "no change". The rewrite is atomic. Returns True if the file was
    rewritten. Raises OSError if the write itself fails.
    """
    with _lock:
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError) as exc:
            # Unreadable or not valid UTF-8 (a hand-edit in another
            # encoding): leave it untouched rather than rewrite garbage.
            log_error(f"rewrite {os.path.basename(path)}", exc)
            return False
        new_lines = transform(lines)
        if new_lines is None:
            return False
        atomic_write_text(path, "".join(new_lines))
        return True


# =============================================================================
#  ERROR LOG  -  the written trail of anything that went wrong.
# =============================================================================

def _trim_error_log():
    """If the log has grown past the cap, keep only its most recent half."""
    try:
        if (os.path.exists(ERROR_LOG)
                and os.path.getsize(ERROR_LOG) > ERROR_LOG_MAX_BYTES):
            with open(ERROR_LOG, "rb") as f:
                f.seek(-(ERROR_LOG_MAX_BYTES // 2), 2)
                tail = f.read()
            nl = tail.find(b"\n")          # drop a partial first line
            if nl != -1:
                tail = tail[nl + 1:]
            with open(ERROR_LOG, "wb") as f:
                f.write(b"... [older entries trimmed] ...\n")
                f.write(tail)
    except OSError:
        pass


def log_error(where, exc=None, message=None):
    """
    Append an entry to the error log and return the text written. Pass an
    exception (its traceback is included) or a plain message. Never raises -
    logging a problem must not itself become a problem.
    """
    try:
        stamp = datetime.now().isoformat(timespec="seconds")
        if exc is not None:
            tb = "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__))
            text = f"[{stamp}] {where}: {exc!r}\n{tb}\n"
        else:
            text = f"[{stamp}] {where}: {message}\n"
        with _lock:
            ensure_data_dir()
            _trim_error_log()
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(text)
        return text
    except Exception:
        return ""


# =============================================================================
#  SETTINGS  -  defaults, validation, and damage recovery for config.json.
# =============================================================================

# The single source of truth for choices and defaults. app.py and the
# dashboard both read these (a test keeps app.py's SETTINGS constants in step).
HOTKEY_NAMES  = ["Ctrl + Win", "Ctrl + Alt", "Ctrl + Shift", "Alt + Win"]
MODEL_CHOICES = ["base", "base.en", "small", "small.en", "medium.en"]
# The cloud services Scribe can transcribe with (see cloud_provider below).
CLOUD_PROVIDERS = ("groq", "elevenlabs")
THEMES = ("system", "light", "dark")
# How hard AI polish tidies (polish.py): "full" like Wispr Flow, or "light"
# (punctuation, capitals and filler sounds only - every other word stays).
POLISH_STYLES = ("full", "light")

DEFAULT_CONFIG = {
    "user_name":          "",
    "hotkey":             "Ctrl + Win",
    "model_size":         "small.en",
    "mic_device":         None,
    "add_trailing_space": True,
    "paste_mode":         True,
    "sound_cues":         True,
    "use_cloud":          False,
    "groq_api_key":       "",
    "cloud_model":        "whisper-large-v3-turbo",
    "pipeline_cloud":     True,
    # Which cloud service transcribes first: "groq" (free tier) or
    # "elevenlabs" (streams while you talk; paid). Groq is then the backup.
    "cloud_provider":     "groq",
    # Only the keystore's fallback - the key normally lives in Credential Manager.
    "elevenlabs_api_key": "",
    # AI polish: a quick Groq model cleans each dictation up (polish.py).
    "polish":             True,
    "polish_style":       "full",
    # The dashboard's look: "system" follows Windows' light/dark setting.
    "theme":              "system",
    # Keep a local Whisper model. Always true for local mode; for cloud users
    # it is the offline backup (the welcome's checkbox, Settings' toggle).
    "local_model":        True,
    # Milestones already celebrated, so the dashboard's confetti fires once.
    "seen_milestones":    [],
    # Once a day, ask GitHub whether a newer Scribe is out (updates.py).
    "check_updates":      True,
    # The newest version Scribe has already told you about (one notice each).
    "update_notified":    "",
}

_BOOL_KEYS = ("add_trailing_space", "paste_mode", "sound_cues",
              "use_cloud", "pipeline_cloud", "local_model", "polish", "check_updates")
_TEXT_KEYS = ("user_name", "groq_api_key", "cloud_model", "elevenlabs_api_key",
              "update_notified")


def _as_bool(value):
    """A real bool, or a hand-typed stand-in for one ("false", "1", "on")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"not a true/false value: {value!r}")


def clean_config_value(key, value):
    """
    Return `value` cleaned for setting `key`, or raise ValueError/TypeError
    if it can't be a valid value for that setting. Unknown keys pass through.
    """
    if key in _BOOL_KEYS:
        return _as_bool(value)
    if key in _TEXT_KEYS:
        if not isinstance(value, str):
            raise TypeError(f"{key} must be text")
        value = value.strip()
        if key == "cloud_model" and not value:
            raise ValueError("cloud_model can't be empty")
        return value
    if key == "theme":
        if isinstance(value, str) and value in THEMES:
            return value
        raise ValueError(f"unknown theme {value!r}")
    if key == "polish_style":
        if isinstance(value, str) and value in POLISH_STYLES:
            return value
        raise ValueError(f"unknown polish style {value!r}")
    if key == "cloud_provider":
        if isinstance(value, str) and value in CLOUD_PROVIDERS:
            return value
        raise ValueError(f"unknown cloud service {value!r}")
    if key == "hotkey":
        if isinstance(value, str) and value in HOTKEY_NAMES:
            return value
        raise ValueError(f"unknown hotkey {value!r}")
    if key == "model_size":
        if isinstance(value, str) and value in MODEL_CHOICES:
            return value
        raise ValueError(f"unknown model {value!r}")
    if key == "mic_device":
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str):
            return value.strip()
        raise TypeError("mic_device must be text")
    if key == "seen_milestones":
        if isinstance(value, list):
            return [v for v in value if isinstance(v, str)]
        raise TypeError("seen_milestones must be a list")
    return value


def validate_config(raw):
    """
    Defaults overlaid with every valid value from `raw`. Returns (cfg,
    bad_keys): a key whose value is invalid falls back to its default on its
    own - one typo never resets the rest. Unknown keys are kept as-is, so a
    newer version's settings survive a trip through an older one.
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg["seen_milestones"] = []          # fresh list, never the shared default
    bad = []
    if not isinstance(raw, dict):
        return cfg, bad
    for key, value in raw.items():
        try:
            cfg[key] = clean_config_value(key, value)
        except (ValueError, TypeError):
            bad.append(key)
    return cfg, bad


# =============================================================================
#  VOCABULARY  -  validation for vocabulary.json.
# =============================================================================

def _empty_vocab():
    return {"terms": [], "corrections": {}, "dismissed": [], "learned": {}}


# Where a learned word came from (learning.py): "fix" - you corrected it after
# Scribe typed it; "said" - you say it often.
LEARNED_SOURCES = ("fix", "said")


def _clean_learned(record):
    """One `learned` entry, cleaned - or None if it isn't a valid one."""
    if not isinstance(record, dict):
        return None
    source, at = record.get("from"), record.get("at")
    if source not in LEARNED_SOURCES or not isinstance(at, str):
        return None
    out = {"from": source, "at": at}
    wrong = record.get("wrong")
    if isinstance(wrong, str) and wrong.strip():
        out["wrong"] = wrong.strip()
    return out


def validate_vocab(raw):
    """
    Keep only entries that are real text: non-empty string terms, non-empty
    string -> string corrections, string dismissed words (lowercased), and
    `learned` records - which words Scribe learned by itself, and how
    (learning.py) - keyed by the lowercased word. Returns (vocab,
    skipped_count). A null or blank correction would otherwise make every
    dictation containing that word vanish.
    """
    vocab = _empty_vocab()
    skipped = 0
    if not isinstance(raw, dict):
        return vocab, skipped
    terms = raw.get("terms", [])
    if isinstance(terms, list):
        for t in terms:
            if isinstance(t, str) and t.strip():
                if t.strip() not in vocab["terms"]:
                    vocab["terms"].append(t.strip())
            else:
                skipped += 1
    else:
        skipped += 1
    corrections = raw.get("corrections", {})
    if isinstance(corrections, dict):
        for wrong, right in corrections.items():
            if (isinstance(wrong, str) and wrong.strip()
                    and isinstance(right, str) and right.strip()):
                vocab["corrections"][wrong.strip()] = right.strip()
            else:
                skipped += 1
    else:
        skipped += 1
    dismissed = raw.get("dismissed", [])
    if isinstance(dismissed, list):
        for d in dismissed:
            if isinstance(d, str) and d.strip():
                if d.strip().lower() not in vocab["dismissed"]:
                    vocab["dismissed"].append(d.strip().lower())
            else:
                skipped += 1
    learned = raw.get("learned", {})
    if isinstance(learned, dict):
        for word, record in learned.items():
            clean = _clean_learned(record)
            if isinstance(word, str) and word.strip() and clean:
                vocab["learned"][word.strip().lower()] = clean
            else:
                skipped += 1
    else:
        skipped += 1
    return vocab, skipped


# =============================================================================
#  DAMAGE RECOVERY  -  a broken file is set aside, never overwritten.
#
#  After every successful load or save we keep a copy as "<file>.bak" - the
#  last version known to be good. When a file won't parse (a hand-edit typo,
#  a disk error) we rename it to "<file>.corrupt-<timestamp>.json" so nothing
#  is lost, restore the .bak if there is one, and hand back a notice telling
#  the user exactly what happened.
# =============================================================================

_NOTICE_TEXT = {
    "settings": {
        "recovered": ("Settings restored",
                      "Your settings file couldn't be read, so Scribe restored "
                      "the last working copy. The damaged file was kept as {file}."),
        "reset":     ("Settings reset",
                      "Your settings file couldn't be read, so Scribe started "
                      "with defaults. The damaged file was kept as {file}."),
        "unreadable": ("Settings couldn't be read",
                       "Another program is holding your settings file, so "
                       "Scribe is using defaults for now and won't overwrite "
                       "it. Restart Scribe to try again."),
        "blocked":   "Another program is holding your settings file, so "
                     "Scribe won't overwrite it. Try again in a moment.",
        "damaged":   "Your settings file is damaged and couldn't be backed "
                     "up, so Scribe won't overwrite it.",
    },
    "vocab": {
        "recovered": ("Vocabulary restored",
                      "Your vocabulary file couldn't be read, so Scribe restored "
                      "the last working copy. The damaged file was kept as {file}."),
        "reset":     ("Vocabulary reset",
                      "Your vocabulary file couldn't be read, so Scribe started "
                      "with an empty vocabulary. The damaged file was kept as {file}."),
        "unreadable": ("Vocabulary couldn't be read",
                       "Another program is holding your vocabulary file, so "
                       "Scribe is running without it for now and won't "
                       "overwrite it."),
        "blocked":   "Another program is holding your vocabulary file, so "
                     "Scribe won't overwrite it. Try again in a moment.",
        "damaged":   "Your vocabulary file is damaged and couldn't be backed "
                     "up, so Scribe won't overwrite it.",
    },
}


def _notice(key, title, message):
    return {"key": key, "title": title, "message": message}


def _set_aside(path):
    """
    Move a damaged file out of the way as <name>.corrupt-<stamp><ext> and
    return the new path. If it can't be moved, try copying it instead. Returns
    None only if the damaged content could not be preserved at all.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base, ext = os.path.splitext(path)
    dest = f"{base}.corrupt-{stamp}{ext}"
    n = 2
    while os.path.exists(dest):          # never replace an earlier kept copy
        dest = f"{base}.corrupt-{stamp}-{n}{ext}"
        n += 1
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        pass
    try:
        shutil.copy2(path, dest)
        return dest
    except OSError:
        return None


def _refresh_backup(path, data):
    """Keep <path>.bak equal to the last good content (only rewrite on change)."""
    backup = path + ".bak"
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        with open(backup, "r", encoding="utf-8") as f:
            if f.read() == text:
                return
    except OSError:
        pass
    try:
        atomic_write_text(backup, text)
    except OSError as exc:
        log_error("backup", exc)


def _load_recovering(path, kind):
    """
    Load a JSON object file, recovering from damage. Returns (raw, notices,
    blocker): raw is the parsed dict (or None), and blocker is None when it
    is safe to write the file afterwards - or a user-readable reason why it
    isn't (the file couldn't be read at all, or a damaged file couldn't be
    preserved). Callers must never write while there is a blocker: an
    unreadable file is NOT a missing one.
    """
    text = _NOTICE_TEXT[kind]
    notices = []
    status, raw = read_json(path)
    if status == "ok" and not isinstance(raw, dict):
        status = "corrupt"                 # valid JSON, wrong shape
    if status == "unreadable":
        log_error(f"load {os.path.basename(path)}",
                  message="exists but couldn't be read (held by another program?)")
        title, message = text["unreadable"]
        notices.append(_notice(f"{kind}_unreadable", title, message))
        return None, notices, text["blocked"]
    if status != "corrupt":
        if status == "ok":
            _refresh_backup(path, raw)
        return (raw if status == "ok" else None), notices, None

    kept = _set_aside(path)
    if kept is None:
        log_error(f"load {os.path.basename(path)}",
                  message="damaged and could not be backed up; leaving it untouched")
        return None, notices, text["damaged"]
    log_error(f"load {os.path.basename(path)}",
              message=f"damaged; kept as {os.path.basename(kept)}")
    b_status, b_raw = read_json(path + ".bak")
    if b_status == "ok" and isinstance(b_raw, dict):
        try:
            atomic_write_json(path, b_raw)
        except OSError as exc:
            log_error("restore backup", exc)
        title, message = text["recovered"]
        notices.append(_notice(f"{kind}_recovered", title,
                               message.format(file=os.path.basename(kept))))
        return b_raw, notices, None
    title, message = text["reset"]
    notices.append(_notice(f"{kind}_reset", title,
                           message.format(file=os.path.basename(kept))))
    return None, notices, None


def load_config():
    """
    The validated settings: defaults overlaid with config.json. Returns
    (cfg, notices) - notices describe any recovery or reset keys, ready to
    show the user.
    """
    with _lock:
        raw, notices, blocker = _load_recovering(CONFIG_FILE, "settings")
        cfg, bad = validate_config(raw)
        if bad:
            names = ", ".join(f"“{k}”" for k in bad)
            log_error("config.json", message=f"invalid value(s) reset: {', '.join(bad)}")
            notices.append(_notice(
                "settings_invalid", "Some settings were reset",
                f"{names} had invalid values and went back to their defaults."))
            # Save the reset, so the file matches what Scribe is using and the
            # notice appears once - not on every start.
            if blocker is None:
                try:
                    atomic_write_json(CONFIG_FILE, cfg)
                    _refresh_backup(CONFIG_FILE, cfg)
                except OSError as exc:
                    log_error("save cleaned settings", exc)
        return cfg, notices


def save_config_changes(changes):
    """
    Merge `changes` into the saved settings and write them safely. Every
    changed value is validated first; an invalid one raises StorageError
    naming the setting. Keys not in `changes` are left exactly as they were.
    Returns the merged settings.
    """
    with _lock:
        cleaned = {}
        for key, value in (changes or {}).items():
            try:
                cleaned[key] = clean_config_value(key, value)
            except (ValueError, TypeError):
                raise StorageError(f"“{key}” has an invalid value.")
        raw, _notices, blocker = _load_recovering(CONFIG_FILE, "settings")
        if blocker:
            raise StorageError(blocker)
        cfg, _bad = validate_config(raw)
        cfg.update(cleaned)
        try:
            ensure_data_dir()
            atomic_write_json(CONFIG_FILE, cfg)
        except OSError as exc:
            raise StorageError(f"Couldn't write your settings ({exc.strerror or exc}).")
        _refresh_backup(CONFIG_FILE, cfg)
        return cfg


def load_vocab():
    """The validated vocabulary. Returns (vocab, notices). A missing file is
    simply an empty vocabulary - and is not created."""
    with _lock:
        raw, notices, blocker = _load_recovering(VOCAB_FILE, "vocab")
        vocab, skipped = validate_vocab(raw)
        if skipped:
            log_error("vocabulary.json", message=f"{skipped} invalid entries ignored")
            notices.append(_notice(
                "vocab_invalid", "Some vocabulary entries were skipped",
                f"{skipped} entries in vocabulary.json weren't valid text and "
                f"were ignored."))
            if blocker is None:            # save the cleaned file: report once
                try:
                    out = _vocab_file_form(vocab)
                    atomic_write_json(VOCAB_FILE, out)
                    _refresh_backup(VOCAB_FILE, out)
                except OSError as exc:
                    log_error("save cleaned vocabulary", exc)
        return vocab, notices


def _vocab_file_form(vocab):
    """The on-disk shape: terms + corrections, and dismissed / learned only
    if there are any (so a hand-made file stays as simple as it was)."""
    out = {"terms": vocab["terms"], "corrections": vocab["corrections"]}
    if vocab["dismissed"]:
        out["dismissed"] = vocab["dismissed"]
    if vocab.get("learned"):
        out["learned"] = vocab["learned"]
    return out


def save_vocab(vocab):
    """Validate and safely write the vocabulary. Raises StorageError."""
    with _lock:
        clean, _skipped = validate_vocab(vocab)
        _raw, _notices, blocker = _load_recovering(VOCAB_FILE, "vocab")
        if blocker:
            raise StorageError(blocker)
        out = _vocab_file_form(clean)
        try:
            ensure_data_dir()
            atomic_write_json(VOCAB_FILE, out)
        except OSError as exc:
            raise StorageError(f"Couldn't write your vocabulary ({exc.strerror or exc}).")
        _refresh_backup(VOCAB_FILE, out)
        return clean


# =============================================================================
#  MIGRATION  -  move data out of the code folder (the old layout).
# =============================================================================

# error_log.txt goes first: if anything below needs to log, the old log is
# already in place instead of being shadowed by a brand-new one.
LEGACY_FILES = ("error_log.txt", "config.json", "vocabulary.json",
                "dictation_log.jsonl", "cloud_usage.jsonl")
LEGACY_DIRS = ("app_icons",)

# What the last migrate_legacy_files() couldn't do cleanly, for app.py to tell
# the user: files found in BOTH places (the data-folder copy is used) and
# files that couldn't be moved at all (the original stays where it was).
MIGRATION_PROBLEMS = {"conflicts": [], "failed": []}


def migrate_legacy_files(app_dir=APP_DIR):
    """
    Move data files from the code folder into DATA_DIR, once. Each file is
    copied, checked (same size), and only then removed from the old spot. A
    file already present in DATA_DIR is never overwritten - the old copy is
    left alone and the fact logged. Never runs against an override folder.
    Returns the names moved.
    """
    moved = []
    MIGRATION_PROBLEMS["conflicts"] = []
    MIGRATION_PROBLEMS["failed"] = []
    if USING_OVERRIDE:
        return moved
    if os.path.normcase(os.path.abspath(app_dir)) == os.path.normcase(DATA_DIR):
        return moved
    for name in LEGACY_FILES:
        src = os.path.join(app_dir, name)
        dst = os.path.join(DATA_DIR, name)
        if not os.path.isfile(src):
            continue
        if os.path.exists(dst):
            log_error("migrate", message=f"{name} is in both {app_dir} and "
                      f"{DATA_DIR}; using the data-folder copy, old one left alone.")
            MIGRATION_PROBLEMS["conflicts"].append(name)
            continue
        tmp = dst + ".migrating"
        try:
            ensure_data_dir()
            shutil.copy2(src, tmp)
            if os.path.getsize(tmp) != os.path.getsize(src):
                raise OSError(f"size mismatch copying {name}")
            os.replace(tmp, dst)
        except OSError as exc:
            log_error(f"migrate {name}", exc)
            MIGRATION_PROBLEMS["failed"].append(name)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            continue
        try:
            os.remove(src)
        except OSError as exc:
            log_error(f"migrate {name}", exc)   # copy is in place; old stays
        moved.append(name)
    for name in LEGACY_DIRS:
        src = os.path.join(app_dir, name)
        dst = os.path.join(DATA_DIR, name)
        if not os.path.isdir(src) or os.path.exists(dst):
            continue
        try:
            shutil.copytree(src, dst)
            shutil.rmtree(src, ignore_errors=True)
            moved.append(name + "/")
        except OSError as exc:
            log_error(f"migrate {name}", exc)
            MIGRATION_PROBLEMS["failed"].append(name + "/")
    return moved
