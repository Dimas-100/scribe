"""
=============================================================================
 SCRIBE - voice dictation for Windows.
=============================================================================

 HOW TO USE:
   1. Run it (setup.bat does the first time, and the Start-menu shortcut
      after that). Two ways:
        python app.py     -> also shows a console with text status messages
        pythonw app.py    -> NO console, runs purely in the background
      Add --show to open the dashboard too (the shortcuts do); without it
      Scribe starts quietly in the tray - the way it starts at sign-in. The
      very first run opens the welcome, which sets everything up.
   2. A round icon appears in your system tray (near the clock):
        TEAL   = ready / listening
        RED    = recording
        AMBER  = transcribing
   3. Put your cursor anywhere you can type (Word, browser, Slack, terminal).
   4. HOLD Ctrl + Win (or the hotkey you chose), speak, then RELEASE.
   5. Your words are transcribed and typed in at the cursor.
   6. Double-click the tray icon (or right-click -> Open Dashboard) to see
      your stats, history, and settings.
   7. Quit by right-clicking the tray icon and choosing "Quit".

 By default everything runs on your machine: no internet, no API keys, no
 cost (the very first run downloads the Whisper model). Optional cloud mode
 sends your audio to Groq using your own API key, for higher accuracy -
 turn it on in the dashboard's Settings. See README.md for setup.
=============================================================================
"""

import sys

# Scribe is Windows-only: the hotkey, typing, focus and tray all use Windows
# APIs. Say so in one plain line instead of failing later with a confusing
# error from deep inside a library.
if sys.platform != "win32":
    print("Scribe runs on Windows 10 and 11 only.")
    sys.exit(1)

# --- Single instance. Runs FIRST - before anything touches your files, and
#     before the slow imports below (numpy, Whisper, Tk: ~2 seconds). A
#     second launch (a Start-menu click while Scribe is already running) must
#     hand off at once and exit without moving the running copy's data out
#     from under it. One Scribe per Windows user and data folder - see
#     instance.py.
import instance
import time


def _hand_off_to_running_copy(send, sleep, attempts=20):
    """Ask the running Scribe to show itself. It may still be starting up
    (its control pipe opens ~2 s after launch), so keep trying for up to
    `attempts` x 0.25 s. True once it answered."""
    for i in range(attempts):
        if send({"cmd": "show"}):
            return True
        if i < attempts - 1:
            sleep(0.25)
    return False


# "--show" = the user asked to SEE Scribe: the Start-menu and taskbar
# shortcuts pass it. Without it - at sign-in, or from a terminal - Scribe
# starts quietly in the tray.
SHOW_REQUESTED = "--show" in sys.argv[1:]

if not instance.acquire():
    # Another Scribe is already running. If the user clicked Scribe, ask the
    # running copy to bring its window up - they just see their dashboard
    # appear. Either way this copy exits right here.
    if SHOW_REQUESTED:
        # This copy was just clicked, so Windows lets it bring windows to the
        # front - hand that right on (ASFW_ANY), or the running copy's
        # "bring the dashboard forward" may only flash its taskbar button.
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
        _hand_off_to_running_copy(instance.send, time.sleep)
    sys.exit(0)

import contextlib
import ctypes
import io
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
import wave
from datetime import datetime, timedelta

try:
    import numpy as np
    import pyperclip
    import pystray
    import sounddevice as sd
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    from PIL import Image, ImageDraw, ImageTk
    from pynput import keyboard
except ImportError as _missing:
    # A missing piece can't be worked around - and under pythonw (how Scribe
    # normally runs) nothing else would ever say so: Scribe would just not
    # appear. On a fresh PC the usual cause is Microsoft's Visual C++ runtime,
    # which the speech libraries need; otherwise an install that didn't
    # finish. setup.bat checks for and fixes both.
    ctypes.windll.user32.MessageBoxW(
        None,
        f"Part of Scribe is missing ({_missing}).\n\n"
        "Run setup.bat in Scribe's folder to repair it. If that doesn't help, "
        "install Microsoft's Visual C++ Redistributable (x64) from "
        "https://aka.ms/vs/17/release/vc_redist.x64.exe and run setup.bat again.",
        "Scribe couldn't start", 0x10 | 0x10000 | 0x40000)   # error icon, in front
    sys.exit(1)

# Scribe's own shared modules (also used by the dashboard): where your data
# lives and how it's saved safely, and which microphones exist.
import devices
import elevenlabs_stream
import fix_watch
import keystore
import learning
import model_manager
import polish
import storage
import updates


def _message_box(title, text):
    """A native Windows message box. Works before Tk or the tray exist, and
    under pythonw - where print() goes nowhere. Best-effort."""
    try:
        MB_ICONERROR, MB_SETFOREGROUND, MB_TOPMOST = 0x10, 0x10000, 0x40000
        ctypes.windll.user32.MessageBoxW(
            None, text, title, MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST)
    except Exception:
        pass


# =============================================================================
#  CRASH HANDLING  -  no error may vanish silently.
#
#  Under pythonw there is no console: an uncaught exception just ends the
#  program (or a thread) with no trace. These hooks make sure every one is
#  written to the error log - and that the user is told, in plain words.
# =============================================================================

# Flips True in main() once the tray is up. Before that, an uncaught error
# means Scribe couldn't start, and a message box is the only way to say so.
startup_complete = False


def _handle_uncaught(exc_type, exc, tb):
    """sys.excepthook: log the error; explain it if it stops Scribe."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    if exc is not None and exc.__traceback__ is None:
        exc = exc.with_traceback(tb)
    storage.log_error("uncaught", exc)
    reason = f"{exc_type.__name__}: {exc}"
    title = "Scribe stopped unexpectedly" if startup_complete else "Scribe couldn't start"
    _message_box(title, f"{reason}\n\nDetails were saved to:\n{storage.ERROR_LOG}")


def _handle_thread_exception(args):
    """threading.excepthook: log it, tell the user once, keep running."""
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "thread")
    storage.log_error(f"background thread {name}", args.exc_value)
    try:
        notify("background_error", "Something went wrong",
               "Scribe kept running. Details are in the error log "
               "(tray → Open data folder).")
    except Exception:
        pass


sys.excepthook = _handle_uncaught
threading.excepthook = _handle_thread_exception


# =============================================================================
#  NOTIFICATIONS  -  short Windows pop-ups from the tray icon.
#
#  The one channel for telling the user something went wrong (and what
#  Scribe did about it). Rate-limited per kind, so a repeating problem shows
#  once rather than on every dictation; safe to call from any thread.
# =============================================================================

NOTICE_SPACING_SECONDS = 4   # gap between queued startup notices
_notice_lock = threading.Lock()
_notice_last = {}            # notice key -> time.monotonic() last shown
_pending_notices = []        # (title, message) raised before the tray existed
_tray_ready = False          # True once the tray icon can show pop-ups


def notify(key, title, message, cooldown=600):
    """
    Show a notification unless one of the same `key` was shown in the last
    `cooldown` seconds. Every notice is also written to the error log, so
    there is a trail after the pop-up fades. Returns True if shown/queued.
    """
    now = time.monotonic()
    with _notice_lock:
        last = _notice_last.get(key)
        if last is not None and now - last < cooldown:
            return False
        _notice_last[key] = now
        storage.log_error("notice", message=f"{title} - {message}")
        if tray_icon is None or not _tray_ready:
            _pending_notices.append((title, message))
            return True
    _show_notice(title, message)
    return True


def _show_notice(title, message):
    try:
        tray_icon.notify(message, title)
    except Exception as exc:
        storage.log_error("notify", exc)


def _show_notices(notices):
    """Show notices produced by storage (settings/vocabulary recovery)."""
    for n in notices:
        notify(n["key"], n["title"], n["message"], cooldown=0)


def _flush_pending_notices():
    """Called once the tray icon is visible: show what startup queued, a few
    seconds apart (Windows shows one pop-up at a time per icon)."""
    global _tray_ready
    with _notice_lock:
        _tray_ready = True
        pending = list(_pending_notices)
        _pending_notices.clear()
    for i, (title, message) in enumerate(pending):
        if i:
            time.sleep(NOTICE_SPACING_SECONDS)
        _show_notice(title, message)


# =============================================================================
#  SETTINGS  -  the knobs you might want to change live here.
# =============================================================================

# --- The hotkey. Choose one of these named two-key combos. You can also
#     change it later from the dashboard's Settings tab. ---
HOTKEY_CHOICES = {
    "Ctrl + Win":   {keyboard.Key.ctrl, keyboard.Key.cmd},
    "Ctrl + Alt":   {keyboard.Key.ctrl, keyboard.Key.alt},
    "Ctrl + Shift": {keyboard.Key.ctrl, keyboard.Key.shift},
    "Alt + Win":    {keyboard.Key.alt, keyboard.Key.cmd},
}
current_hotkey_name = "Ctrl + Win"            # the active combo's name
HOTKEY = HOTKEY_CHOICES[current_hotkey_name]  # the active set of keys

# --- The undo hotkey. Hold these modifiers, then tap Z, to delete the most
#     recent dictation. Win-key combos like this are ignored by other apps,
#     so nothing else (such as an editor's redo) fires by accident. Keep it
#     from overlapping HOTKEY above, or recording would start instead. ---
UNDO_MODIFIERS = {keyboard.Key.ctrl, keyboard.Key.cmd}
UNDO_KEY_VK = 0x5A   # the "Z" key, as a layout-independent virtual-key code

# --- Whisper model size. Bigger = more accurate but slower on CPU. ---
# "tiny" | "base" | "small" | "medium"  (bigger = more accurate, slower)
# Add ".en" for an English-only model: more accurate for English, same speed
# (e.g. "small.en"). NOTE: a saved config.json overrides this default.
MODEL_SIZE = "small.en"

# The model sizes offered in the dashboard Settings tab (the welcome offers
# the English ones).
MODEL_CHOICES = storage.MODEL_CHOICES   # shared with the dashboard

# A sample of well-punctuated text handed to Whisper as a STYLE HINT before
# each transcription. It nudges the model toward proper capitalization and
# punctuation. It is NOT typed into your output - it only primes the model.
TRANSCRIBE_PROMPT = (
    "Hello. This is a clear, well-written sentence, with correct "
    "punctuation: commas, periods, and question marks. How does that sound?"
)

# Whisper's prompt window is only ~224 tokens. The style hint above plus the
# vocabulary terms must fit inside it, so build_transcribe_prompt() caps the
# vocabulary portion at this many characters. Without the cap, a vocabulary
# that grows over time (e.g. via the dashboard's auto-learning suggestions)
# would eventually crowd out the punctuation hint and hurt casing/punctuation.
# Corrections still apply to every term regardless of the prompt, so the cap
# only softens *prevention* for the overflow terms, never *correction*.
MAX_PROMPT_VOCAB_CHARS = 480

# --- Audio format. These match what Whisper expects - leave them alone. ---
SAMPLE_RATE = 16000   # 16 kHz: Whisper's native sample rate
CHANNELS = 1          # mono

# The microphone to record from, by device name. None = the Windows default
# input device. Set in the welcome or the dashboard Settings tab, and saved
# in config.json.
MIC_DEVICE = None

# Recordings shorter than this (seconds) are treated as an accidental tap.
MIN_RECORDING_SECONDS = 0.3

# Longest single recording. At this point the recording is finished and
# transcribed like a normal release (never thrown away) and the user is told
# to start a new one. It also bounds memory if a key-up is ever missed - but
# a missed key-up is caught much sooner, within ~2 s (see _handle_check).
MAX_RECORDING_SECONDS = 300
# How often (seconds) the watchdog asks the input controller to check the
# recording against the physical key state and the time limit.
WATCHDOG_TICK_SECONDS = 1

# Add a space after each dictation so back-to-back dictations don't run
# their words together. Set to False to type the text exactly as-is.
ADD_TRAILING_SPACE = True

# How the transcribed text is delivered to the focused app.
#   True  -> PASTE MODE: copy to the clipboard and send one Ctrl+V. Near-
#            instant, even for long dictations. Briefly borrows the clipboard
#            (we save and restore whatever was on it).
#   False -> TYPE MODE: simulate every keystroke. Slower for long text, but
#            never touches the clipboard.
# This changes only HOW the text is delivered - not the text itself, so
# transcription accuracy is identical either way.
PASTE_MODE = True

# Play soft tones on recording start and stop. A barely-there "tap" that
# tells you the hotkey was heard before you've even started speaking, and
# a slightly lower one on release so you know it registered. Synthesized
# on the fly by play_cue() - no audio files on disk.
SOUND_CUES = True

# Your name, used to personalize the dashboard ("Good evening, Sam"). Empty
# by default; set it on the Settings tab. Loaded from config.json like the
# rest of the settings below.
USER_NAME = ""

# Filler sounds stripped from every transcription by remove_fillers(). These
# must all be NON-words - things that are never real English words - so
# removing them can't delete anything you meant to say. (Deliberately NOT
# included: "er", "mm", "ah" - each is a real word or unit in normal use
# "the ER", "3 mm", "Ah, I see" - so stripping them would eat real text.)
# Add your own verbal tics here if Whisper keeps picking them up; leave the
# list empty to disable removal.
FILLER_WORDS = ["um", "uh", "umm", "uhh", "uhm", "erm", "hmm"]

# Spoken commands that become real line breaks. Whisper transcribes speech as
# one unbroken block - it cannot produce line breaks on its own - so saying
# these is the only way to get them while dictating. apply_voice_commands()
# does the substitution. (Punctuation is left to Whisper's auto-detection.)
VOICE_COMMANDS = {
    "new paragraph": "\n\n",
    "new line": "\n",
}

# Whether to ALSO interpret these commands when they appear INLINE inside a
# longer dictation (e.g. "...first point new line second point..."). Off by
# default: the inline matcher is necessarily loose, so it also fires on
# ordinary prose that merely contains the words - "start a new line of code"
# would lose "of code", "a new paragraph for this essay" would lose "for this
# essay". The reliable way to get a line break is to say the command ALONE
# (tap the hotkey, say just "new line", release) - that whole-utterance path
# is always on (see whole_utterance_command). Flip this to True only if you
# accept the inline false-positive risk.
INLINE_VOICE_COMMANDS = False

# Spoken commands that ACT on the last dictation rather than inserting text.
# Recognized only as a whole-utterance (you tap the hotkey, say the command
# alone, release) so we never confuse them with the user dictating ABOUT
# the action. The handler for each lives in handle_whole_utterance_action().
#
#   "scratch that" - voice version of the Ctrl+Win+Z undo hotkey: deletes
#                    the most recent dictation from the cursor and the log.
#   "fix that"     - sends the last dictation back to Groq with a cleanup
#                    prompt and replaces it with the cleaned version.
#                    Needs cloud transcription's API key (Settings).
WHOLE_UTTERANCE_ACTIONS = ("scratch that", "fix that")

# "fix that" runs the last dictation through AI polish again (polish.py -
# the same Groq model, rules and answer check as every dictation).

# Your typing speed in words-per-minute. The dashboard uses this to estimate
# how much time dictating saves you vs. typing. 40 is an average typist -
# change it to your real speed for a more accurate "time saved" figure.
TYPING_WPM = 40

# --- Cloud transcription (Groq). Optional. Off by default - Scribe stays
#     fully local until you flip the toggle in Settings. When on, audio is
#     sent to Groq's whisper-large-v3-turbo: ~$0.04 per HOUR of audio, with
#     sub-second latency and accuracy higher than the local 'small.en'
#     model. If the API call fails (network down, bad key, etc.) we
#     transparently fall back to the local model so dictation still works.
USE_CLOUD = False                          # master on/off switch
GROQ_API_KEY = ""                          # paste from console.groq.com/keys
CLOUD_MODEL = "whisper-large-v3-turbo"     # the Groq model id to use
# Which cloud service transcribes first. "elevenlabs" streams your audio to
# ElevenLabs WHILE you talk, so the text is ready moments after you let go
# (see elevenlabs_stream.py); Groq is then the backup. "groq" = Groq only.
CLOUD_PROVIDER = "groq"
ELEVENLABS_API_KEY = ""                    # kept in Credential Manager, like Groq's
# Keep a local model even in cloud mode - the offline backup, used when the
# cloud can't be reached. (Local mode always has one.) Set in the welcome
# or Settings; False = cloud-only, nothing downloaded.
LOCAL_MODEL = True

# --- AI polish. After transcription, a quick Groq model (see polish.py)
#     cleans each dictation up the way Wispr Flow does - grammar,
#     punctuation, false starts, rambling - in about 0.2 s. It needs the
#     cloud on and a Groq key; if it fails, is slow or answers oddly, your
#     words are typed as spoken.
POLISH = True
# "full" tidies like Wispr Flow; "light" only fixes punctuation, capitals
# and filler sounds - every other word you said stays (Settings -> AI polish).
POLISH_STYLE = "full"

# The dashboard's look ("system" / "light" / "dark"). Only the dashboard uses
# it; it's here so every setting has its code default in one place.
THEME = "system"

# Once a day, ask GitHub whether a newer Scribe is out, and say so once per
# new version (see the UPDATES section and updates.py). Settings -> About.
CHECK_UPDATES = True
UPDATE_NOTIFIED = ""          # the newest version you were already told about

# The Dictionary fills itself: Scribe learns the words you fix after it types
# them, and the names you say often (learning.py, fix_watch.py). Settings ->
# Dictation -> "Learn my words automatically".
LEARN_WORDS = True

# --- Cloud pipelining. When on (and cloud is also on), Scribe runs a
#     "speculative" Groq transcription every few seconds DURING the
#     dictation. On release, if that result covers nearly all the audio,
#     we use it instead of doing a fresh final transcription - so the
#     time from release to text-on-screen drops from ~500ms to almost
#     nothing. Cost: a few extra Groq calls per dictation (still
#     pennies). Falls back to a normal final transcription whenever the
#     speculative result is missing, too stale, or errored out.
PIPELINE_CLOUD = True
# How often to kick a fresh speculative call (seconds). Shorter = lower
# release latency, higher cost; longer = the opposite. 2.5s is a fair
# balance for the typical few-words-at-a-time dictation pattern.
PIPELINE_INTERVAL = 2.5
# Max gap (seconds) between the speculative's audio length and the
# total audio. Below this, we trust the speculative as the final answer
# (and transcribe just the tiny missing tail on the free local model so the
# last word is never dropped - see _use_speculative_or_transcribe). Above,
# we redo the transcription.
PIPELINE_MAX_GAP = 0.6
# Hard cap on speculative calls per dictation. Each speculative call is a
# real Groq request that re-uploads ALL audio-so-far and counts against the
# free-tier daily request AND audio-second limits, so an unbounded fan-out
# burns the quota ~3x faster than the actual audio warrants. With the cap,
# the worst case is PIPELINE_MAX_SPEC_CALLS speculative + 1 final per
# dictation, instead of "one call every PIPELINE_INTERVAL for as long as you
# talk."
PIPELINE_MAX_SPEC_CALLS = 2
# Don't kick a new speculative call unless at least this much NEW audio has
# arrived since the last one we transcribed. Re-sending near-identical audio
# (a long pause, a slow talker) just spends quota for no new information.
PIPELINE_MIN_NEW_AUDIO_SECONDS = 1.5

# --- Cloud request limits. The Groq SDK's defaults (60 s timeout, and two
#     automatic retries that also wait out rate limits) could leave a
#     dictation stuck on "transcribing" for about three minutes. These are the
#     bounds Scribe uses instead - failing fast and falling back to local.
CLOUD_TIMEOUT_SECONDS = 12           # waiting for Groq's answer
CLOUD_CONNECT_TIMEOUT_SECONDS = 4    # reaching Groq at all
CLOUD_PAUSE_AFTER_ERROR_SECONDS = 30 # skip the cloud this long after a network failure
CLOUD_DEFAULT_RATE_LIMIT_PAUSE = 60  # if a 429 doesn't say how long to wait

# --- Silence trimming for cloud uploads. Whisper was trained on YouTube
#     captions, so when it is handed silence or room noise it "hears" the
#     words that usually sit over silence in captions: "Thank you." and
#     "Thank you for watching!" - or it echoes our own style prompt back
#     ("This is a clear, well-written sentence...", "Vocabulary, ...").
#     The local model has always had vad_filter=True to prevent this; the
#     cloud path uploaded the raw recording, dead air included. Now
#     trim_silence() cuts the non-speech out first, using the same Silero
#     voice detector faster-whisper uses for the local model.
# A silent stretch at least this long (ms) is cut out. Shorter pauses - the
# natural gaps between words and sentences - stay in so Whisper keeps its
# sense of phrasing and punctuation.
CLOUD_VAD_MIN_SILENCE_MS = 1000
# Padding (ms) kept on each side of every speech stretch, so soft word
# starts ("h" in "hello") and trailing consonants are never clipped.
CLOUD_VAD_SPEECH_PAD_MS = 400


# =============================================================================
#  FILE LOCATIONS
# =============================================================================

# The folder this script lives in - the CODE. Built from __file__ so it is
# correct no matter what folder the app is launched from (important once it
# runs on startup, where the launch folder is something like System32).
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# The app icon ships with the code (it is an asset, not user data). Used by
# the dashboard window, and pointed at by the launch shortcut.
ICO_FILE = os.path.join(APP_DIR, "scribe.ico")

# Everything that is YOURS - settings, vocabulary, history, logs - lives in
# a per-user data folder (%APPDATA%\Scribe), managed by storage.py. These
# aliases keep the rest of this file reading naturally.
#   CONFIG_FILE     - settings, including your API key
#   VOCAB_FILE      - your terms and corrections
#   LOG_FILE        - every dictation, one JSON object per line
#   CLOUD_USAGE_LOG - one line per Groq call (both speculative and final
#                     calls count against Groq's per-minute/per-day limits),
#                     read by the dashboard's quota bars
#   ERROR_LOG       - anything that went wrong. pynput silently STOPS its
#                     thread when a callback raises, and pythonw has no
#                     console - so if the hotkey ever goes dead, the last
#                     entry here is your culprit.
CONFIG_FILE     = storage.CONFIG_FILE
VOCAB_FILE      = storage.VOCAB_FILE
LOG_FILE        = storage.LOG_FILE
CLOUD_USAGE_LOG = storage.CLOUD_USAGE_LOG
ERROR_LOG       = storage.ERROR_LOG

# The dashboard and the quota math only ever look at TODAY's cloud-usage
# entries, so anything older is dead weight. _trim_cloud_usage_log keeps only
# the last this-many days, bounding the file (a small safety margin over "1
# day" guards against timezone / midnight-rollover edge cases).
CLOUD_USAGE_KEEP_DAYS = 7
# ElevenLabs entries are kept longer: the dashboard's meter counts a whole
# billing month of them.
CLOUD_USAGE_KEEP_DAYS_STREAM = 40


# =============================================================================
#  SETTINGS PERSISTENCE  -  load/save the dashboard-editable settings.
# =============================================================================

# Move data files out of the code folder, where older versions kept them
# (once; see storage.migrate_legacy_files). Must run before anything reads
# the settings, so an upgrading user keeps theirs.
MIGRATED = storage.migrate_legacy_files()


def _migration_notices(problems):
    """Notices for anything migration couldn't do cleanly (see
    storage.MIGRATION_PROBLEMS). Empty when all went well."""
    notices = []
    if problems["conflicts"]:
        names = ", ".join(problems["conflicts"])
        notices.append({
            "key": "migrate_conflict", "title": "Some old files were left in place",
            "message": f"Scribe found {names} in both its old folder and its data "
                       f"folder, and kept using the data-folder copy. The old copy "
                       f"is untouched."})
    if problems["failed"]:
        names = ", ".join(problems["failed"])
        notices.append({
            "key": "migrate_failed", "title": "Couldn't move some files",
            "message": f"Scribe couldn't move {names} into its data folder. Your "
                       f"originals are untouched - details are in the error log."})
    return notices


def _is_first_run(config_exists, problems):
    """First run = no settings anywhere. A settings file that failed to
    migrate still counts as existing - the welcome must not create a fresh
    config that would hide the real one."""
    return not config_exists and "config.json" not in problems["failed"]

# Notices raised during startup (a damaged settings file restored, invalid
# values reset, no microphone...). The tray icon doesn't exist yet, so they
# are shown once it does - see _flush_pending_notices.
STARTUP_NOTICES = _migration_notices(storage.MIGRATION_PROBLEMS)


def load_config():
    """
    Apply the saved settings over the code defaults. storage.load_config
    validates every value (a bad one falls back to its default on its own)
    and recovers a damaged file - so this can't crash on a hand-edit typo.
    Called at startup, BEFORE the model loads (so a saved model size takes
    effect), and again whenever the dashboard saves. Returns notices
    describing any recovery, for the caller to show.
    """
    global MODEL_SIZE, HOTKEY, ADD_TRAILING_SPACE, current_hotkey_name
    global PASTE_MODE, MIC_DEVICE, USER_NAME, SOUND_CUES
    global USE_CLOUD, GROQ_API_KEY, CLOUD_MODEL, PIPELINE_CLOUD, LOCAL_MODEL
    global CLOUD_PROVIDER, ELEVENLABS_API_KEY, POLISH, POLISH_STYLE, THEME
    global CHECK_UPDATES, UPDATE_NOTIFIED, LEARN_WORDS
    cfg, notices = storage.load_config()
    # An API key still in config.json (older versions kept it there) moves
    # into Windows Credential Manager - once, verified - see keystore.py.
    notices = notices + keystore.migrate_from_config(cfg)
    MODEL_SIZE = cfg["model_size"]
    ADD_TRAILING_SPACE = cfg["add_trailing_space"]
    PASTE_MODE = cfg["paste_mode"]
    MIC_DEVICE = cfg["mic_device"]
    USER_NAME = cfg["user_name"]
    SOUND_CUES = cfg["sound_cues"]
    USE_CLOUD = cfg["use_cloud"]
    GROQ_API_KEY = keystore.resolve_key(cfg)  # the vault's; config.json only as a fallback
    CLOUD_MODEL = cfg["cloud_model"]
    CLOUD_PROVIDER = cfg["cloud_provider"]
    ELEVENLABS_API_KEY = keystore.resolve_key(cfg, "elevenlabs")
    PIPELINE_CLOUD = cfg["pipeline_cloud"]
    LOCAL_MODEL = cfg["local_model"]
    POLISH = cfg["polish"]
    POLISH_STYLE = cfg["polish_style"]
    THEME = cfg["theme"]
    CHECK_UPDATES = cfg["check_updates"]
    UPDATE_NOTIFIED = cfg["update_notified"]
    LEARN_WORDS = cfg["learn_words"]
    current_hotkey_name = cfg["hotkey"]          # validated: always a known name
    HOTKEY = HOTKEY_CHOICES[current_hotkey_name]
    return notices


def save_config():
    """Save the current settings (used when the welcome can't open, to set
    Scribe up with the defaults). Merges into the file, so keys the
    dashboard owns (milestones) are kept. Never writes the API key - that
    lives in the keystore. Raises storage.StorageError with a readable
    reason if it can't be written."""
    storage.save_config_changes({
        "model_size": MODEL_SIZE,
        "hotkey": current_hotkey_name,
        "add_trailing_space": ADD_TRAILING_SPACE,
        "paste_mode": PASTE_MODE,
        "mic_device": MIC_DEVICE,
        "user_name": USER_NAME,
        "sound_cues": SOUND_CUES,
        "use_cloud": USE_CLOUD,
        "cloud_model": CLOUD_MODEL,
        "cloud_provider": CLOUD_PROVIDER,
        "pipeline_cloud": PIPELINE_CLOUD,
        "local_model": LOCAL_MODEL,
        "polish": POLISH,
        "polish_style": POLISH_STYLE,
        "check_updates": CHECK_UPDATES,
    })


# True if this is the very first run - there is no config.json yet. Computed
# BEFORE load_config() so it reflects the original state; it becomes
# setup_pending below (the welcome's job).
FIRST_RUN = _is_first_run(os.path.exists(CONFIG_FILE), storage.MIGRATION_PROBLEMS)

# Apply any saved settings right now, before anything else uses them.
STARTUP_NOTICES.extend(load_config())


# =============================================================================
#  CUSTOM VOCABULARY  -  your names, jargon, and product terms.
# =============================================================================

# Filled in by load_vocabulary() below from vocabulary.json.
#   VOCAB_TERMS       - a plain list of correct spellings, fed to Whisper's
#                       prompt so it is biased toward producing them.
#   VOCAB_CORRECTIONS - a list of (wrong, right) pairs for an exact
#                       find-and-replace fix-up after transcription.
VOCAB_TERMS = []
VOCAB_CORRECTIONS = []
# The whole vocabulary as loaded (terms, corrections, dismissed, and `learned`
# - which words Scribe added by itself), for learning.py.
VOCAB_DATA = {"terms": [], "corrections": {}, "dismissed": [], "learned": {}}

# Correction keys that are ALSO everyday English words. We keep these OUT of
# the forced find-and-replace (apply_vocabulary) so normal prose isn't
# miscapitalized - "cut me some slack" must not become "...Slack", "no notion
# of it" must not become "...Notion". They still live in vocabulary.json's
# `terms`, which biases Whisper toward the right spelling without rewriting
# every occurrence. Compared lowercased, so the match is case-insensitive.
VOCAB_SKIP_CORRECTIONS = {"slack", "notion", "zoom"}


def load_vocabulary():
    """
    Read vocabulary.json (validated by storage - blank or non-text entries
    are skipped) into VOCAB_TERMS and VOCAB_CORRECTIONS. A missing file just
    means no custom vocabulary. Returns notices, like load_config().
    """
    global VOCAB_TERMS, VOCAB_CORRECTIONS, VOCAB_DATA
    vocab, notices = storage.load_vocab()
    VOCAB_DATA = vocab
    VOCAB_TERMS = vocab["terms"]
    # Drop corrections for words that are also ordinary English (see
    # VOCAB_SKIP_CORRECTIONS) so they aren't force-rewritten in normal prose.
    corrections = {
        wrong: right for wrong, right in vocab["corrections"].items()
        if wrong.lower() not in VOCAB_SKIP_CORRECTIONS
    }
    # Sort longest 'wrong' phrase first: if two rules could match overlapping
    # text, the longer (more specific) one should win.
    VOCAB_CORRECTIONS = sorted(
        corrections.items(), key=lambda pair: len(pair[0]), reverse=True
    )
    return notices


STARTUP_NOTICES.extend(load_vocabulary())


def _ranked_terms():
    """
    The Dictionary's terms, least important first: the words Scribe learned
    by itself (oldest first), then YOUR words, newest last. ElevenLabs' 50
    priority words, the polish prompt and the Whisper prompt all take the
    newest first when they run out of room - so a pile of learned words can
    never push out a word you added yourself.
    """
    learned = VOCAB_DATA.get("learned") or {}
    auto = [t for t in VOCAB_TERMS if str(t).strip().lower() in learned]
    auto.sort(key=lambda t: learned[str(t).strip().lower()].get("at", ""))
    mine = [t for t in VOCAB_TERMS if str(t).strip().lower() not in learned]
    return auto + mine


# =============================================================================
#  SHARED STATE  -  variables the different threads read and write.
# =============================================================================

frames = []          # list of audio chunks captured while recording
pressed = set()      # keys currently held down (normalized)
recording = False    # True while the hotkey is held and we're capturing audio
_blocked_notice = None        # a blocked press's notice, shown on release (controller only)
recording_started_at = None   # time.monotonic() when the current recording began
                              # (read by the stuck-recording watchdog)
last_output = ""     # exact text of the most recent dictation, kept for undo
last_duration = 0.0  # duration of that dictation - used to re-log a "fix that"
last_output_hwnd = None      # the window it was typed into (undo targets it)
last_output_logged = False   # whether it has a history entry (undo drops it)
last_output_app = (None, None)   # (app_name, app_exe), for re-logging a fix
undo_lock = threading.Lock()  # guards last_output while an undo is happening

# Serializes the restore-focus -> deliver -> log critical section in
# process_audio. Two dictations finishing close together (fast back-to-back
# talking, with seconds-long CPU transcription) would otherwise run two
# deliver()/paste_text() calls at once and fight over the single system
# clipboard. The lock makes each delivery atomic.
deliver_lock = threading.Lock()

# Serializes access to the shared local Whisper `model`. CTranslate2 is not
# guaranteed re-entrant, and the speculative tail-stitch can overlap a
# back-to-back dictation's transcription, so two model.transcribe() calls
# could otherwise hit the same object concurrently.
transcribe_lock = threading.Lock()

# How loud the mic is right this instant - written by audio_callback on
# the audio thread, read by the overlay tick on the main thread to drive
# the live waveform. A single float is atomic enough for a visual
# indicator (the GIL makes the read/write whole), so no lock is needed.
current_audio_level = 0.0

# Cloud pipelining: a worker thread keeps a "rolling" transcription
# warm during recording so process_audio doesn't have to wait for a
# full round-trip on release. See _speculative_worker() and SETTINGS.
#   speculative_result   -> (audio_length_in_samples, transcribed_text)
#                            or None. Latest result the worker stored.
#   speculative_lock     -> serializes read/write of the above.
#   speculative_thread   -> the worker, or None when no recording active.
#   recording_stop_event -> set by stop_recording so the worker's
#                            between-call sleep wakes up immediately.
speculative_result = None
speculative_lock = threading.Lock()
speculative_thread = None
recording_stop_event = threading.Event()
# A monotonically increasing "generation" id, bumped on every start_recording.
# The speculative worker captures the generation it was spawned for and only
# writes its result / keeps looping while that still matches the current one.
# This fences off a stale worker that wakes from a slow Groq call AFTER the
# next dictation has begun, so it can't stamp the previous utterance's text
# into the new dictation or run as a second concurrent worker.
speculative_generation = 0

# ElevenLabs streaming: the current dictation's Session (elevenlabs_stream),
# or None. Only the input controller thread sets or clears it (start, finish,
# cancel); the mic callback only READS it to hand each chunk over - reading a
# reference is atomic, so no lock is needed.
_stream_session = None

tray_icon = None         # the system-tray icon object (created in main())
listener = None          # the global keyboard listener (created in main())
root = None              # the hidden Tkinter root window (created in main())
overlay = None           # the floating on-screen status pill
overlay_canvas = None    # the Canvas inside the overlay - we draw on this
overlay_state = "idle"   # "idle" | "recording" | "transcribing" - drives the animation
overlay_anim_phase = 0   # ever-incrementing tick counter; drives the sine waves
overlay_anim_job = None  # root.after() handle for the animation loop, so we can cancel
# Today's Groq usage state, refreshed every QUOTA_TICK_MS on the main
# thread. quota_state drives the idle pill's color; quota_worst_ratio
# is the 0.0..1.0+ fraction of the worst daily counter, used in the
# tray tooltip ("Scribe - ready · 78% of daily Groq quota used").
quota_state = "ok"           # "ok" | "warn" | "danger"
quota_worst_ratio = 0.0
app_icon_photo = None    # the app icon image (kept referenced so it persists)
target_hwnd = None       # window that had focus when dictation started
target_app_name = None   # friendly name of the app owning target_hwnd
target_app_exe = None    # full .exe path - used by the dashboard for icons
dashboard_proc = None    # the dashboard subprocess Popen handle (or None)
dashboard_welcome = False  # that dashboard was opened as the first-run welcome

# A thread-safe "mailbox". Background threads (the tray) drop commands here;
# the main Tkinter thread reads them. This is how we safely cross threads.
ui_queue = queue.Queue()

# Cached Whisper prompt (style hint + vocabulary terms). VOCAB_TERMS is fixed
# after startup, so build_transcribe_prompt() builds the string once instead
# of re-joining 35 terms on every transcription (including each speculative
# cycle). Reset to None to force a rebuild if the vocabulary ever reloads.
_TRANSCRIBE_PROMPT_CACHE = None

# Last (x, y) the overlay window was positioned at, so _reposition loops can
# skip a redundant geometry set when nothing moved.
_overlay_last_xy = None

# Date cloud_usage.jsonl was last trimmed, so the daily trim runs once a day.
_last_cloud_trim_date = None

# The "virtual keyboard" used to type transcribed text into other apps.
kbd = keyboard.Controller()


# =============================================================================
#  CONTROL CHANNEL  -  messages from the dashboard and from a second launch.
#
#  instance.serve() runs _handle_control on its own thread for every message
#  that arrives on Scribe's named pipe. It must stay quick, and anything that
#  touches Tk or the settings goes through ui_queue to the main thread.
# =============================================================================

def _status_snapshot():
    """What the dashboard's welcome and Settings show about the running app."""
    return {
        "setup_complete": not setup_pending,
        "hotkey": current_hotkey_name,
        "cloud": _cloud_configured(),
        "cloud_ready": cloud_available() or eleven_available(),
        # For the dashboard's live status: what Scribe is doing right now
        # (recording beats transcribing beats idle, like the pill) and which
        # service transcribes first.
        "activity": (("recording" if _audio_flowing.is_set() else "connecting") if recording
                     else "transcribing" if _in_flight else "idle"),
        "provider": CLOUD_PROVIDER if USE_CLOUD else "local",
        "model": models.snapshot(),
    }


def _handle_control(message):
    """One control message. Returns the reply ('status' only; None = ok)."""
    command = message.get("cmd")
    if command == "status":
        return _status_snapshot()
    if command == "reload-config":
        ui_queue.put("reload-config")     # the dashboard saved settings
    elif command == "show":
        ui_queue.put("open")              # a second launch asked to be seen
    elif command == "setup-done":
        ui_queue.put("setup-done")        # the welcome saved the user's choices
    elif command == "retry-model":
        models.retry()                    # the welcome's / Settings' Retry
    elif command == "quit":
        ui_queue.put("quit")              # the updater: files are about to change
    return None


# =============================================================================
#  FIRST RUN  -  the welcome (in the dashboard window) sets Scribe up.
#
#  On a first run nothing is loaded or downloaded until the user has chosen
#  how Scribe should listen: main() opens the welcome, and "Finish setup"
#  saves the choices and sends "setup-done" (-> complete_setup). Until then a
#  hotkey press points to the welcome. If the welcome can't open at all (no
#  WebView2), Scribe sets itself up with the defaults instead
#  (finish_setup_with_defaults).
# =============================================================================

setup_pending = FIRST_RUN


# =============================================================================
#  THE LOCAL SPEECH MODEL  -  found, downloaded and loaded in the background.
#
#  Scribe no longer makes you wait for the model: the tray, overlay and
#  hotkey come up first, and model_manager.py loads the model on its own
#  thread - downloading it first, with progress, if it isn't on disk yet.
#  Cloud users can dictate at once; local dictation works as soon as the
#  model is ready (see _dictation_blocker and transcribe_audio). It starts at
#  the very END of this file, once every function its callbacks use exists.
# =============================================================================

# How long a dictation waits for a model that is loading from disk (normally
# a few seconds) before giving up on it.
MODEL_LOAD_WAIT_SECONDS = 60

# True after the user was told to wait for the model (model_not_ready,
# setup_defaults) - so they're also told when it's ready. Never on a normal start.
_model_wait_told = False
_last_model_state = None     # for the console log: print state changes only


def _failed_title(snap):
    """A failed model's notice title: which step went wrong."""
    return ("Couldn't load the speech model" if snap.get("failed_stage") == "load"
            else "Couldn't download the speech model")


def _on_model_change(snap):
    """
    Runs on the loader thread whenever the model's state changes (and about
    twice a second while downloading): refreshes the tray tooltip, and tells
    the user about the moments that matter.
    """
    global _model_wait_told, _last_model_state
    if snap["state"] != _last_model_state:
        _last_model_state = snap["state"]
        print(f"[MODEL] {snap['name'] or '-'}: {snap['state']}"
              + (f" ({snap['error']})" if snap["error"] else ""))
    if snap["state"] == "ready" and _model_wait_told:
        _model_wait_told = False
        notify("model_ready", "Scribe is ready",
               f"Hold {current_hotkey_name} and speak.", cooldown=0)
    elif snap["state"] == "failed":
        notify("model_failed", _failed_title(snap),
               f"{snap['error']} Open the dashboard to try again.", cooldown=0)
    _refresh_status()


models = model_manager.ModelManager(on_change=_on_model_change)


def _local_model():
    """The local Whisper model in use, or None (not loaded yet, failed, or
    cloud-only). Always read it through here - the manager can swap it."""
    return models.get()


def _apply_model_settings():
    """
    Ask for the model the settings call for: the chosen size whenever local
    transcription can be needed (local mode, or cloud with the offline
    backup on), none at all for cloud-only. Runs at startup and after every
    settings reload - which is why a new model size applies live.
    """
    if LOCAL_MODEL or not USE_CLOUD:
        models.request(MODEL_SIZE)
    else:
        models.disable()


# Queue whatever startup found (a recovered settings file, invalid values...)
# to be shown as soon as the tray icon exists - see _flush_pending_notices.
_show_notices(STARTUP_NOTICES)


# =============================================================================
#  TRAY ICON  -  the visible state indicator when running in the background.
# =============================================================================

def make_icon(color):
    """
    Draw the Scribe icon, Wispr-Flow style: a soft squircle in the given
    state color with four white equalizer bars rising and falling at the
    center - the visual shorthand for "voice / audio." The squircle (a
    rounded square with a generous radius) reads more like a modern app
    icon than the old plain circle. Drawn at 256 px so it stays crisp
    when Windows downsamples it for the taskbar and tray.
    """
    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))  # transparent
    draw = ImageDraw.Draw(image)

    # Squircle background: rounded rectangle with a large corner radius.
    draw.rounded_rectangle(
        (12, 12, size - 12, size - 12), radius=56, fill=color,
    )

    # Four equalizer bars of varying heights - the same silhouette as the
    # "lll Flow" wordmark in Wispr Flow. Each bar is a white rounded
    # rectangle; varying heights with the tallest in the middle suggest
    # a live audio signal.
    bar_w = 22
    bar_gap = 18
    heights = [78, 132, 102, 58]
    total_w = len(heights) * bar_w + (len(heights) - 1) * bar_gap
    x_start = (size - total_w) / 2
    cy = size / 2
    radius = bar_w / 2
    for i, h in enumerate(heights):
        x = x_start + i * (bar_w + bar_gap)
        y_top = cy - h / 2
        y_bot = cy + h / 2
        draw.rounded_rectangle(
            (x, y_top, x + bar_w, y_bot), radius=radius, fill="white",
        )
    return image


# Deep teal idle color, matching the dashboard accent.
ICON_IDLE = make_icon((30, 95, 74))        # deep teal - ready
ICON_RECORDING = make_icon((214, 69, 69))  # red       - recording
ICON_BUSY = make_icon((209, 137, 54))      # amber     - transcribing

# Save the blue (idle) icon as a .ico file the first time, so the dashboard
# window and the launch shortcut have a proper icon. An .ico bundles several
# resolutions in one file; Windows picks whichever size it needs.
if not os.path.exists(ICO_FILE):
    try:
        ICON_IDLE.save(
            ICO_FILE, format="ICO",
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    except OSError:
        pass   # read-only code folder - the .ico ships with the repo anyway


def set_state(image, title):
    """
    Update the tray icon's picture and hover-tooltip to reflect what the
    app is doing. Safe to call before the icon exists (it just does nothing).
    """
    if tray_icon is not None:
        # Only touch pystray when something changed: a model download
        # refreshes the tooltip twice a second, and each icon swap rewrites
        # a temp .ico.
        if tray_icon.icon is not image:
            tray_icon.icon = image
        if tray_icon.title != title:
            tray_icon.title = title


def update_status(state):
    """
    Record the app's visible state - "recording", "transcribing" or "idle".
    Safe to call from ANY thread: it only queues the change, and the main
    thread applies it to the tray icon and the overlay (see _apply_status).
    (pystray swaps icon handles and writes a temp .ico on every change, so
    doing that from four threads at once could leave the wrong icon.)
    """
    ui_queue.put(("status", state))


def _idle_tooltip(snap, usable, quota, ratio, pending=False):
    """
    The tray tooltip while idle. What's still getting ready comes first
    (download progress, loading, a failed download - the last two only when
    nothing else can transcribe), else today's Groq quota nudge (a quiet
    "you'll hit the cap soon" on hover). Kept under 64 characters (Windows
    truncates tray tooltips).
    """
    if pending:
        return "Scribe - finish setup to start dictating"
    if snap["state"] == "downloading":
        return f"Scribe - downloading speech model ({_percent(snap)}%)"
    if snap["state"] == "loading" and not usable:
        return "Scribe - loading speech model..."
    if snap["state"] == "failed" and not usable:
        if snap.get("failed_stage") == "load":
            return "Scribe - couldn't load the speech model"
        return "Scribe - couldn't download the speech model"
    if quota != "ok":
        return f"Scribe - ready · {int(round(ratio * 100))}% of daily Groq quota used"
    return "Scribe - ready"


def _apply_status(state):
    """Main thread only: show `state` on the tray icon and the overlay pill."""
    if state == "recording":
        set_state(ICON_RECORDING, "Scribe - recording...")
    elif state == "transcribing":
        set_state(ICON_BUSY, "Scribe - transcribing...")
    else:  # "idle"
        usable = _cloud_configured() or _local_model() is not None
        set_state(ICON_IDLE, _idle_tooltip(models.snapshot(), usable,
                                           quota_state, quota_worst_ratio,
                                           pending=setup_pending))
    update_overlay(state)


# How many dictations are being transcribed/delivered right now. Together
# with the recording flag it decides what the status shows (_refresh_status).
_in_flight = 0
_in_flight_lock = threading.Lock()


def _job_started():
    """A dictation job began (process_audio)."""
    global _in_flight
    with _in_flight_lock:
        _in_flight += 1


def _job_finished():
    """A dictation job ended - delivered, skipped or failed."""
    global _in_flight
    with _in_flight_lock:
        _in_flight = max(0, _in_flight - 1)


def _refresh_status():
    """
    Show what Scribe is doing overall: recording beats transcribing beats
    idle. Everything that finishes calls this instead of forcing "idle", so
    a dictation that finishes while the next one is recording can't blank
    the pill mid-recording.
    """
    if recording and _audio_flowing.is_set():
        update_status("recording")
    elif _in_flight:
        update_status("transcribing")
    else:
        update_status("idle")


# =============================================================================
#  WINDOW FOCUS  -  remember and restore the target window (Windows).
# =============================================================================

# Tell ctypes these functions deal in window handles, so 64-bit handles are
# not truncated to 32 bits.
try:
    _user32 = ctypes.windll.user32
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    _user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _user32.IsWindow.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
except Exception:
    _user32 = None

# kernel32 is used below to resolve a window's owning process to an .exe
# path, so we can log which APP the user is dictating into.
# QueryFullProcessImageNameW works even against UAC-elevated processes -
# OpenProcess with PROCESS_QUERY_INFORMATION would fail there.
try:
    _kernel32 = ctypes.windll.kernel32
    _kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong,
    ]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
except Exception:
    _kernel32 = None


def capture_target_window():
    """Return a handle to the window that currently has keyboard focus."""
    if _user32 is None:
        return None
    return _user32.GetForegroundWindow()


def get_target_app_info(hwnd):
    """
    Resolve `hwnd` to (friendly_name, exe_path) for its owning process.

    friendly_name comes from the .exe's version resource (FileDescription)
    when available - that's the human label Windows itself shows in Task
    Manager and Alt+Tab, so "Code.exe" is reported as "Visual Studio Code"
    rather than the bare filename. Falls back to the basename without the
    .exe extension. Either field may be None if any step fails, in which
    case the dashboard just skips this entry in app analytics.
    """
    if not hwnd or _user32 is None or _kernel32 is None:
        return (None, None)
    try:
        # hwnd -> pid. GetWindowThreadProcessId returns the thread id and
        # writes the process id into the out-parameter.
        pid = ctypes.c_ulong(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return (None, None)

        # PROCESS_QUERY_LIMITED_INFORMATION is the minimum-rights flag
        # that still lets us read the image name, AND works against
        # elevated processes (the stricter PROCESS_QUERY_INFORMATION
        # would be refused there).
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = _kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value,
        )
        if not handle:
            return (None, None)
        try:
            buf  = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buf))
            if not _kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size),
            ):
                return (None, None)
            exe_path = buf.value
        finally:
            _kernel32.CloseHandle(handle)

        if not exe_path:
            return (None, None)

        friendly = _file_description(exe_path)
        if not friendly:
            # "C:\...\Code.exe" -> "Code"
            friendly = os.path.splitext(os.path.basename(exe_path))[0]
        return (friendly, exe_path)
    except Exception:
        return (None, None)


def _file_description(exe_path):
    """
    Read the FileDescription field from an .exe's version resource.
    Returns None if there is no version info or no description string -
    common for hand-built tools that never embedded version metadata.

    The Win32 dance: ask for the resource size, allocate, fetch it, read
    the translation block (which language/codepage the strings use), then
    query the FileDescription string under that language path.
    """
    try:
        version_dll = ctypes.windll.version
    except Exception:
        return None
    try:
        size = version_dll.GetFileVersionInfoSizeW(exe_path, None)
        if not size:
            return None
        res = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(exe_path, 0, size, res):
            return None

        # \VarFileInfo\Translation is a 4-byte block: 2 bytes language id,
        # 2 bytes codepage. We pick the first translation rather than
        # trying to match the current locale - good enough for a label.
        lp = ctypes.c_void_p()
        ln = ctypes.c_uint()
        if not version_dll.VerQueryValueW(
            res, r"\VarFileInfo\Translation",
            ctypes.byref(lp), ctypes.byref(ln),
        ):
            return None
        if ln.value < 4:
            return None
        trans    = ctypes.string_at(lp, 4)
        lang     = int.from_bytes(trans[0:2], "little")
        codepage = int.from_bytes(trans[2:4], "little")

        # The actual string lives under StringFileInfo\<lang><codepage>\.
        subblock = (
            f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
        )
        ptr = ctypes.c_void_p()
        n   = ctypes.c_uint()
        if not version_dll.VerQueryValueW(
            res, subblock, ctypes.byref(ptr), ctypes.byref(n),
        ):
            return None
        if not n.value:
            return None
        return ctypes.wstring_at(ptr, n.value).rstrip("\x00").strip() or None
    except Exception:
        return None


def restore_target_window(hwnd):
    """
    Bring `hwnd` back to the foreground so the text lands in it - even if the
    user clicked away during transcription - and report whether that really
    happened. Windows restricts which process may change the foreground
    window, so we briefly attach our thread's input to the target window's
    thread, which lifts that block; a minimized window is restored first.

    Returns True once `hwnd` is verifiably in front. Returns False when the
    window is gone (closed during transcription) or wouldn't come forward -
    the caller then must NOT type, or the text would land somewhere random.
    Where focus can't be managed at all (not Windows), returns True and
    delivery goes to whatever has focus, as it always did there.
    """
    if _user32 is None:
        return True
    if not hwnd:
        return False
    try:
        if not _user32.IsWindow(hwnd):
            return False
        SW_RESTORE = 9
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)
        if _user32.GetForegroundWindow() == hwnd:
            return True
        kernel32 = ctypes.windll.kernel32
        target_thread = _user32.GetWindowThreadProcessId(hwnd, None)
        our_thread = kernel32.GetCurrentThreadId()
        _user32.AttachThreadInput(our_thread, target_thread, True)
        _user32.SetForegroundWindow(hwnd)
        _user32.AttachThreadInput(our_thread, target_thread, False)
        # The switch isn't always instant - give it up to ~0.2 s.
        for _ in range(10):
            if _user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.02)
        return False
    except Exception:
        return False


# =============================================================================
#  AUDIO  -  piece (a), adapted for open-ended hold-to-talk recording.
# =============================================================================

# Troubleshooting (SCRIBE_SAVE_TAKES=1): keep every take's audio, as the
# mic delivered it, in the data folder's debug-takes\ - with how long the key
# was held and how often PortAudio had to drop input - so a bad transcript
# can be checked against what the microphone really heard.
SAVE_TAKES = os.environ.get("SCRIBE_SAVE_TAKES") == "1"
_take_overflows = 0          # this take's input overflows (dropped audio)
_take_started = None         # time.monotonic() when this take's mic started


# "Speak now" - the pill growing, the start sound - is only given once the
# mic's audio is really arriving. A built-in or USB mic delivers within a few
# hundredths of a second; a Bluetooth headset (AirPods) first switches to call
# mode - measured ~1 s on every start, 2 s on the first - and whatever is said
# meanwhile is LOST. So Scribe waits briefly in place, then (for such a mic)
# says "speak now" from a helper thread the moment the audio starts.
LISTEN_WAIT_INLINE = 0.15    # seconds to wait in place for the first audio
LISTEN_WAIT_MAX = 4.0        # ...and how long the helper keeps waiting after that
_audio_flowing = threading.Event()   # set by audio_callback: this take has audio
_take_serial = 0             # which take is current (only start_recording writes)


def _announce_when_listening(take):
    """Helper thread for a slow (Bluetooth) mic: "speak now" once its audio
    arrives - unless that take is already over (a quick tap)."""
    if not _audio_flowing.wait(LISTEN_WAIT_MAX):
        return
    if take != _take_serial or not recording:
        return
    with mic_lock:           # like every cue: never race a device refresh
        play_cue("start")
    update_status("recording")
    print(f"[REC]   The mic is listening now - speak.")


def _save_take(audio, held_seconds, overflows):
    """Write one take to debug-takes/<time>.wav + .json (SAVE_TAKES only)."""
    try:
        folder = os.path.join(storage.DATA_DIR, "debug-takes")
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(os.path.join(folder, stamp + ".wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
        storage.atomic_write_json(os.path.join(folder, stamp + ".json"), {
            "captured_seconds": round(len(audio) / SAMPLE_RATE, 2),
            "held_seconds": None if held_seconds is None else round(held_seconds, 2),
            "input_overflows": overflows,
            "rms": round(float(np.sqrt(np.mean(audio.astype(np.float32) ** 2))), 5),
            "peak": round(float(np.max(np.abs(audio))), 4),
        })
    except Exception as exc:
        storage.log_error("save_take", exc)


def audio_callback(indata, frame_count, time_info, status):
    """
    Called automatically by sounddevice many times per second while the
    mic stream is running. Each call hands us a small chunk of fresh audio,
    which we stash into 'frames'. We copy() because the buffer gets reused.

    We also track the chunk's loudness (RMS) so the overlay can draw a
    live waveform of the user's actual voice instead of a fake EQ. The
    exponential smoothing (0.5 alpha) turns the jittery per-chunk values
    into a flowing trace that reads as a voice, not noise.
    """
    global current_audio_level, _take_overflows
    if status and status.input_overflow:
        _take_overflows += 1          # PortAudio dropped input before we read it
    if not _audio_flowing.is_set():
        _audio_flowing.set()          # this take's audio has started (see LISTEN_WAIT_INLINE)
    chunk = indata.copy()
    frames.append(chunk)
    # Streaming to ElevenLabs: hand the same chunk to this dictation's
    # session. feed() is a queue put - it never blocks the audio thread.
    session = _stream_session
    if session is not None:
        session.feed(chunk)
    rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
    current_audio_level = current_audio_level * 0.5 + rms * 0.5


def make_stream(device):
    """Create the microphone input stream on `device` (a PortAudio device
    index, or None for the default mic). Raises if it can't be opened.

    The mic's RAW audio comes first (see devices.raw_twin): a laptop's own
    noise suppression - Dolby and the like - chops speech up so badly that
    the cloud misses most of it. A mic with no raw mode, or no WASAPI entry,
    is opened the usual way."""
    twin = devices.raw_twin(device)
    if twin is not None:
        try:
            return sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                device=twin,
                callback=audio_callback,
                extra_settings=devices.raw_settings(),
            )
        except Exception as exc:
            print(f"[MIC]   No raw audio from this mic ({exc}) - using it as is.")
    return sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        device=device,
        callback=audio_callback,
    )


# The mic stream: opened here at startup, started/stopped per dictation, and
# reopened when devices change. None when no microphone is usable - Scribe
# still runs and picks one up later (see _device_watcher).
stream = None
# Guards every open/close/start/stop of `stream`, so the device watcher can
# never swap the mic out from under a dictation that is starting.
mic_lock = threading.Lock()
# Set when the mic should be reopened at the next idle moment: a device was
# plugged/unplugged, the Windows default changed, or Settings picked a mic.
mic_dirty = False
# How the current stream was opened: "chosen" (your Settings mic), "default"
# (the Windows default), "fallback" (default, because your mic is missing),
# or "none" (no usable mic). Drives the notifications in _announce_mic.
_mic_status = "none"


def _close_stream():
    """Close the mic stream if there is one. Caller holds mic_lock."""
    global stream
    if stream is not None:
        try:
            stream.close()
        except Exception:
            pass
    stream = None


def _stop_stream():
    """Stop recording from the mic (keeps it open). Safe from any thread."""
    with mic_lock:
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass


def open_mic():
    """
    (Re)open the mic stream and return how it went (see _mic_status). Your
    Settings mic first; if it's missing or fails, the Windows default; if
    that fails too, no stream. Never raises. Caller holds mic_lock.
    """
    global stream, _mic_status
    _close_stream()
    if MIC_DEVICE:
        index = devices.resolve_device(MIC_DEVICE)
        if index is not None:
            try:
                stream = make_stream(index)
                _mic_status = "chosen"
                return _mic_status
            except Exception as exc:
                storage.log_error(f"open mic {MIC_DEVICE!r}", exc)
    try:
        stream = make_stream(None)
        _mic_status = "fallback" if MIC_DEVICE else "default"
    except Exception as exc:
        storage.log_error("open default mic", exc)
        stream = None
        _mic_status = "none"
    return _mic_status


def reopen_mic(refresh=True):
    """Re-read the device list (PortAudio snapshots it at startup) and reopen
    the mic. Takes ~0.1 s, so it only runs when something changed. Caller
    holds mic_lock."""
    global mic_dirty
    _close_stream()
    if refresh:
        try:
            devices.refresh_portaudio()
        except Exception as exc:
            storage.log_error("refresh audio devices", exc)
    mic_dirty = False
    return open_mic()


# A hotkey press with no mic re-shows "No microphone found" after this long
# (the background watcher uses the calmer 60 s) - so pressing and getting
# silence never feels like nothing happened.
NO_MIC_PRESS_COOLDOWN = 15


def _announce_mic(status, previous, default_changed, default_name=None, press=False):
    """Tell the user about a mic change - only when it matters to them.
    `default_name` is looked up by the caller while it holds mic_lock (a
    device query must never race a PortAudio refresh)."""
    if status == "none":
        notify("no_mic", "No microphone found",
               "Plug one in — Scribe will pick it up automatically.",
               cooldown=NO_MIC_PRESS_COOLDOWN if press else 60)
    elif status == "fallback":
        notify("mic_fallback", "Microphone unavailable",
               f"“{MIC_DEVICE}” isn't connected, so Scribe is using your default mic.")
    elif previous in ("none", "fallback"):
        name = MIC_DEVICE if status == "chosen" else (
            default_name or "your default microphone")
        notify("mic_switched", "Microphone connected",
               f"Now listening with {name}.", cooldown=5)
    elif default_changed and status == "default":
        name = default_name or "your default microphone"
        notify("mic_switched", "Microphone changed",
               f"Now listening with {name}.", cooldown=5)


# Open the mic now. Any problem (no mic, your mic unplugged) is only queued
# as a notice - it never stops Scribe from starting.
with mic_lock:
    _announce_mic(open_mic(), None, False)   # no name needed: only warnings


def play_cue(kind):
    """Play a brief soft tone marking a recording state transition.

    Two cues are used: a slightly HIGHER note when recording starts
    ("I'm listening"), and a LOWER one when it stops ("got it"). Both
    are short, quiet, and shaped with a raised-cosine envelope so they
    fade in and out without clicking at the edges. The descending
    interval (a perfect fourth, C5 -> G4) is the same shape most OS
    notification sounds use because it reads as completion.

    Non-blocking - sounddevice plays on its own thread, so this returns
    almost immediately even though the tone takes ~80ms to finish.
    Skipped entirely when SOUND_CUES is False.

    Safe to call from any thread, and safe to call WHILE the mic
    InputStream is running: sd.play() opens its OWN output stream and
    does not touch ours."""
    if not SOUND_CUES:
        return
    freq = 523.25 if kind == "start" else 392.00   # C5 / G4
    duration = 0.08                                # 80 ms - just a tap
    samplerate = 44100
    n = int(samplerate * duration)
    t = np.arange(n) / samplerate
    # Raised-cosine "bell" envelope: grows from silence at t=0, peaks
    # in the middle, fades back to silence at t=duration. No click at
    # either edge - clicks are what makes synthesized tones feel cheap.
    envelope = 0.5 * (1 - np.cos(2 * np.pi * t / duration))
    tone = (np.sin(2 * np.pi * freq * t) * envelope * 0.06).astype(np.float32)
    try:
        sd.play(tone, samplerate=samplerate)
    except Exception:
        # An output-device hiccup must never break dictation - the cue
        # is decoration on top of the real work the hotkey is doing.
        pass


def start_recording():
    """Begin capturing audio from the mic. Returns False (and tells the
    user) if no microphone could be started."""
    global target_hwnd, target_app_name, target_app_exe
    global speculative_result, speculative_thread, current_audio_level
    global speculative_generation, _stream_session, _take_overflows, _take_started
    global _take_serial
    # Remember which window has focus now, so the transcribed text lands
    # there even if the user switches windows while it transcribes.
    target_hwnd = capture_target_window()
    # Capture which APP that window belongs to so the dashboard can
    # break dictations down by app (with real icons). Best-effort: any
    # failure leaves the fields as None and log_dictation() drops them.
    target_app_name, target_app_exe = get_target_app_info(target_hwnd)
    frames.clear()           # throw away any previous recording
    _take_overflows, _take_started = 0, None
    _take_serial += 1
    take = _take_serial
    _audio_flowing.clear()   # "speak now" waits for this take's first audio
    flowing = False
    # Zero the live mic-level reading so the overlay's first waveform
    # frame doesn't briefly inherit the loudness of the previous dictation.
    current_audio_level = 0.0
    # Reset the speculative pipeline state for this dictation. The
    # worker thread (if cloud + pipelining are both on) keeps a fresh
    # cloud transcription warm while we record, so process_audio can
    # often skip the final round-trip entirely.
    with speculative_lock:
        speculative_result = None
        speculative_generation += 1
        my_generation = speculative_generation
    recording_stop_event.clear()
    # ElevenLabs: open this dictation's stream now, so it listens while you
    # talk (connecting takes a moment; the audio waits in its queue). Only
    # when ElevenLabs is the service and usable - or paused, but the only way
    # to transcribe at all, when it's still worth a try.
    stale = _take_stream_session()     # (only after an error elsewhere)
    if stale is not None:
        stale.cancel()
    if _eleven_configured() and (eleven_available() or not _other_backend()):
        _stream_session = _start_stream_session()
    # Make sure a working mic stream exists. Normally it's already open, so
    # this is just start() (~10 ms). If devices changed, or the last start
    # failed, reopen first (~0.1 s) - and if a start still fails (the mic was
    # unplugged since), refresh the device list once more and retry.
    with mic_lock:
        previous = _mic_status
        if stream is None or mic_dirty:
            reopen_mic()
        started = False
        if stream is not None:
            try:
                stream.start()
                started = True
            except Exception as exc:
                storage.log_error("stream.start", exc)
                if reopen_mic() != "none":
                    try:
                        stream.start()
                        started = True
                    except Exception as exc2:
                        storage.log_error("stream.start (retry)", exc2)
        status = _mic_status if started else "none"
        name = devices.default_input_name() if status == "default" and status != previous else None
        if started:
            _take_started = time.monotonic()
            # Only now - with the mic's audio really arriving - confirm it to
            # the user. Played under mic_lock so a device refresh can't free
            # the cue's audio stream mid-play. A mic that's still connecting
            # (Bluetooth) is confirmed later, by _announce_when_listening.
            flowing = _audio_flowing.wait(LISTEN_WAIT_INLINE)
            if flowing:
                play_cue("start")
    if status != previous or not started:
        _announce_mic(status, previous, False, name, press=True)
    if not started:
        session = _take_stream_session()      # no mic, nothing to stream
        if session is not None:
            session.cancel()
        return False

    if _groq_pipeline_on():
        speculative_thread = threading.Thread(
            target=_speculative_worker, args=(my_generation,), daemon=True,
        )
        speculative_thread.start()
    if flowing:
        update_status("recording")
        print(f"[REC]   Recording... speak now, release {current_hotkey_name} "
              f"when done.")
    else:
        print("[REC]   Waiting for the mic to start (a Bluetooth headset?)...")
        threading.Thread(target=_announce_when_listening, args=(take,),
                         daemon=True).start()
    return True


def stop_recording():
    """Stop the mic and return everything captured as one 1-D audio array."""
    # Soft "got it" tone first, so the user hears the release land
    # immediately - even on a short tap where transcription will be
    # skipped, the cue tells them the keystroke registered.
    with mic_lock:            # never race a device refresh (see play_cue)
        play_cue("stop")
    _stop_stream()
    # Wake the speculative worker so it can wind down immediately - this
    # also lets process_audio's `join` below return as soon as the
    # in-flight Groq call (if any) finishes, instead of waiting out the
    # worker's sleep interval.
    recording_stop_event.set()
    if not frames:
        return None
    # Glue all the little chunks into one array, then flatten to 1-D. Snapshot
    # the chunk list first (list(frames)) so a late audio_callback append on
    # the audio thread can't mutate it mid-concatenate.
    return np.concatenate(list(frames)).flatten()


# =============================================================================
#  DICTATION LOG  -  records each dictation so the dashboard can analyze it.
# =============================================================================

def log_dictation(text, duration_seconds, app_name=None, app_exe=None,
                  engine=None, latency=None, raw=None, fixed=0):
    """Append one dictation to the log file as a single JSON line. The app is
    passed in (captured when the recording started), never read from the
    globals - the next dictation may already have changed those."""
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "text": text,
        "words": len(text.split()),
        "duration": round(duration_seconds, 1),
    }
    # The app the user dictated INTO. Both fields are best-effort - we
    # only write them when the Win32 calls succeeded - so the dashboard
    # can safely skip an entry that lacks them in the per-app breakdown.
    if app_name:
        entry["app_name"] = app_name
    if app_exe:
        entry["app_exe"] = app_exe
    # Which service transcribed it, and how many seconds after release it
    # landed - shown on the dashboard (Home's live card, Insights' speed).
    if engine:
        entry["engine"] = engine
    if latency is not None:
        entry["latency"] = round(latency, 2)
    # What you actually said, when AI polish changed it - the dashboard's
    # "Show what you said" and Insights' polish share. Stays on this PC.
    if raw:
        entry["raw"] = raw
        entry["polished"] = True
    # How many words your Dictionary corrected in it (Insights counts them).
    if fixed:
        entry["fixed"] = fixed
    # storage serializes appends, so two dictations can never interleave.
    storage.append_jsonl(LOG_FILE, entry)


def log_cloud_call(audio_seconds, model, provider=None):
    """Append one successful Groq whisper call to the cloud usage log - or,
    with provider="elevenlabs", one streamed take (Groq's quota math skips
    those; the dashboard's monthly ElevenLabs meter counts them).

    Audio length (in seconds, not samples) is what the dashboard uses to
    show usage against Groq's audio-seconds-per-hour / per-day quotas.
    Stored as a separate JSON Lines file (not the dictation log) because
    one dictation can produce SEVERAL calls when cloud pipelining is on -
    we need a per-call record for the rate-limit math, not a per-
    dictation summary.

    Best-effort: a write failure must never block the dictation that
    triggered it.
    """
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "audio_seconds": round(audio_seconds, 1),
        "model": model,
    }
    if provider:
        entry["provider"] = provider
    try:
        storage.append_jsonl(CLOUD_USAGE_LOG, entry)
    except OSError:
        pass


def _trim_cloud_usage_log(keep_days=CLOUD_USAGE_KEEP_DAYS):
    """
    Drop cloud-usage entries older than keep_days so the file stays bounded -
    Groq's, whose quota math only reads TODAY; ElevenLabs' streams are kept
    CLOUD_USAGE_KEEP_DAYS_STREAM days for the dashboard's monthly meter.
    Best-effort - any read/parse/write problem leaves the file untouched,
    and unparseable lines are kept rather than lost.
    Run at startup and once per day (see _quota_tick).
    """
    try:
        cutoff = (datetime.now() - timedelta(days=keep_days)).date()
        stream_cutoff = (datetime.now()
                         - timedelta(days=CLOUD_USAGE_KEEP_DAYS_STREAM)).date()
    except (ValueError, OverflowError):
        return

    def keep_recent(lines):
        kept, changed = [], False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                changed = True            # drop blank lines while we're here
                continue
            try:
                entry = json.loads(stripped)
                ts = datetime.fromisoformat(entry.get("timestamp", ""))
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
                kept.append(line)         # keep anything we can't parse
                continue
            limit = stream_cutoff if entry.get("provider") == "elevenlabs" else cutoff
            if ts.date() >= limit:
                kept.append(line)
            else:
                changed = True
        return kept if changed else None

    # Atomic, under storage's lock: a crash mid-rewrite can't empty the file.
    try:
        storage.rewrite_lines(CLOUD_USAGE_LOG, keep_recent)
    except OSError:
        pass


def remove_last_log_entry():
    """
    Drop the final entry from the dictation log. Called when a dictation is
    undone, so the dashboard's stats and History do not count text that was
    deleted again. Atomic, so a crash mid-rewrite can't truncate the history.
    """
    def drop_last(lines):
        while lines and not lines[-1].strip():   # trailing blank lines
            lines.pop()
        if not lines:
            return None
        return lines[:-1]

    try:
        storage.rewrite_lines(LOG_FILE, drop_last)
    except OSError as exc:
        _write_error_log("remove_last_log_entry", exc)
        notify("history_failed", "Couldn't update your history",
               "Your text was handled, but Scribe couldn't update its history "
               "file. Details are in the error log.")


# =============================================================================
#  TRANSCRIBE + TYPE  -  pieces (b) and (d), run on a worker thread.
# =============================================================================

# --- Clipboard snapshot / restore (Windows). Paste-mode delivery borrows the
#     clipboard for one Ctrl+V; to give it back intact we snapshot EVERY format
#     the user had on it (text as CF_UNICODETEXT, copied files as CF_HDROP,
#     images as CF_DIB, HTML/RTF, ...) as raw bytes and put them all back
#     afterward. This is the fix for pyperclip being text-only - it returns ""
#     for an image/file payload, so the old save/restore silently destroyed it.
#     Best-effort: any failure falls back to the text-only pyperclip path. ---
try:
    if os.name == "nt":
        _clip_u = ctypes.windll.user32
        _clip_k = ctypes.windll.kernel32
        _clip_u.GetClipboardData.restype = ctypes.c_void_p
        _clip_u.GetClipboardData.argtypes = [ctypes.c_uint]
        _clip_u.SetClipboardData.restype = ctypes.c_void_p
        _clip_u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        _clip_u.EnumClipboardFormats.restype = ctypes.c_uint
        _clip_u.EnumClipboardFormats.argtypes = [ctypes.c_uint]
        _clip_u.OpenClipboard.argtypes = [ctypes.c_void_p]
        _clip_u.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        _clip_u.RegisterClipboardFormatW.restype = ctypes.c_uint
        _clip_k.GlobalLock.restype = ctypes.c_void_p
        _clip_k.GlobalLock.argtypes = [ctypes.c_void_p]
        _clip_k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        _clip_k.GlobalSize.restype = ctypes.c_size_t
        _clip_k.GlobalSize.argtypes = [ctypes.c_void_p]
        _clip_k.GlobalAlloc.restype = ctypes.c_void_p
        _clip_k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        _CLIPBOARD_OK = True
    else:
        _CLIPBOARD_OK = False
except Exception:
    _CLIPBOARD_OK = False

# Formats backed by a GDI handle rather than HGLOBAL memory - GlobalSize/Lock
# don't apply, so we skip them. Windows auto-synthesizes the common image ones
# (CF_BITMAP) from CF_DIB, which we DO capture, so images still round-trip.
_GDI_CLIPBOARD_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}


def _clipboard_snapshot(retries=50):
    """Return {format_id: bytes} for every HGLOBAL clipboard format, or None
    if the clipboard couldn't be read (non-Windows, or another app holds it)."""
    if not _CLIPBOARD_OK:
        return None
    for _ in range(retries):
        if _clip_u.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return None
    data = {}
    try:
        fmt = _clip_u.EnumClipboardFormats(0)
        while fmt:
            if fmt not in _GDI_CLIPBOARD_FORMATS:
                handle = _clip_u.GetClipboardData(fmt)
                if handle:
                    size = _clip_k.GlobalSize(handle)
                    if size:
                        ptr = _clip_k.GlobalLock(handle)
                        if ptr:
                            try:
                                data[fmt] = ctypes.string_at(ptr, size)
                            finally:
                                _clip_k.GlobalUnlock(handle)
            fmt = _clip_u.EnumClipboardFormats(fmt)
    except Exception:
        data = None
    finally:
        _clip_u.CloseClipboard()
    return data


# How long the target app gets to READ a pasted dictation before the
# user's own clipboard goes back. Slow readers (Remote Desktop, VMs, a busy
# Electron app) need more than a blink, or they'd paste the OLD clipboard.
CLIPBOARD_RESTORE_DELAY = 0.35

# Registered clipboard formats that tell Windows (and well-behaved clipboard
# managers) "don't keep this": no Win+V history, no cloud clipboard sync.
# A dictation borrows the clipboard for an instant; it's not something the
# user copied, and may well be private.
_PRIVATE_CLIPBOARD_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing",
                              "CanIncludeInClipboardHistory",
                              "CanUploadToCloudClipboard")
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _clipboard_open(retries=50):
    """Open the clipboard, retrying for up to ~0.5 s while another app holds
    it. Returns True if opened (the caller must CloseClipboard)."""
    for _ in range(retries):
        if _clip_u.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _put_clip_blob(fmt, blob):
    """Put raw bytes on the (already open) clipboard under format `fmt`."""
    handle = _clip_k.GlobalAlloc(_GMEM_MOVEABLE, len(blob) or 1)
    if not handle:
        return
    ptr = _clip_k.GlobalLock(handle)
    if not ptr:
        return
    if blob:
        ctypes.memmove(ptr, blob, len(blob))
    _clip_k.GlobalUnlock(handle)
    # On success the system owns `handle`; on failure we let it leak (rare)
    # rather than risk a double-free.
    _clip_u.SetClipboardData(fmt, handle)


def _mark_clipboard_private():
    """Tag what's on the (already open) clipboard as not-for-history."""
    for name in _PRIVATE_CLIPBOARD_FORMATS:
        fmt = _clip_u.RegisterClipboardFormatW(name)
        if fmt:
            _put_clip_blob(fmt, b"\x00\x00\x00\x00")   # DWORD 0 = "no"


def _clipboard_set_text(text, private=True):
    """
    Put `text` on the clipboard. `private` keeps it out of clipboard history
    and cloud sync (used for the brief borrow during a paste). Returns False
    if the clipboard couldn't be opened (another app is holding it).
    """
    if not _CLIPBOARD_OK:
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            return False
    if not _clipboard_open():
        return False
    try:
        _clip_u.EmptyClipboard()
        _put_clip_blob(_CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
        if private:
            _mark_clipboard_private()
        return True
    except Exception:
        return False
    finally:
        _clip_u.CloseClipboard()


def _clipboard_restore(snapshot, retries=50, private=True):
    """Replace the clipboard with a snapshot from _clipboard_snapshot(). An
    empty snapshot ({}) correctly restores an originally-empty clipboard.
    `private` marks it not-for-history (the user's copy is already there)."""
    if not _CLIPBOARD_OK or snapshot is None:
        return False
    for _ in range(retries):
        if _clip_u.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return False
    GMEM_MOVEABLE = 0x0002
    try:
        _clip_u.EmptyClipboard()
        for fmt, blob in snapshot.items():
            handle = _clip_k.GlobalAlloc(GMEM_MOVEABLE, len(blob) or 1)
            if not handle:
                continue
            ptr = _clip_k.GlobalLock(handle)
            if not ptr:
                continue
            if blob:
                ctypes.memmove(ptr, blob, len(blob))
            _clip_k.GlobalUnlock(handle)
            # On success the system takes ownership of `handle`; on failure we
            # let it leak (rare) rather than risk a double-free.
            _clip_u.SetClipboardData(fmt, handle)
        # The user's content is already in their clipboard history from when
        # they copied it - putting it back must not add a duplicate entry.
        if private:
            _mark_clipboard_private()
    except Exception:
        return False
    finally:
        _clip_u.CloseClipboard()
    return True


def paste_text(text, private=True):
    """
    Deliver `text` by clipboard paste instead of key-by-key typing: copy the
    text, then send one Ctrl+V. Near-instant even for long dictations.

    The clipboard is shared with everything else on the PC, so we save what
    was on it first and put that back afterward - the user never notices we
    borrowed it. Safe to call from the worker thread: pyperclip and pynput
    both work off the main thread (unlike Tkinter's clipboard).
    """
    # Snapshot the WHOLE clipboard (all formats) so we can hand it back intact
    # - including images/files that the text-only pyperclip would destroy.
    # When the snapshot is unavailable (non-Windows, or the clipboard was
    # locked), fall back to the text-only save/restore.
    backup = _clipboard_snapshot()
    previous_text = ""
    if backup is None:
        try:
            previous_text = pyperclip.paste()
        except Exception:
            previous_text = ""

    # Our text goes on the clipboard marked private: no Win+V history, no
    # cloud clipboard, no clipboard managers. If another app is holding the
    # clipboard, type the text instead - never lose the dictation.
    if not _clipboard_set_text(text, private=private):
        print("[PASTE] Clipboard busy - typing instead.")
        # The borrow may have failed halfway (after emptying the clipboard):
        # give the user's content back before typing.
        if backup is not None:
            _clipboard_restore(backup, private=private)
        kbd.type(text)
        return
    try:
        # Send Ctrl+V to the focused window. kbd.pressed() holds Ctrl down for
        # the block, so this is press-Ctrl, tap-V, release-Ctrl.
        with kbd.pressed(keyboard.Key.ctrl):
            kbd.press("v")
            kbd.release("v")
        # Give the target app time to actually READ the clipboard before we
        # put the user's content back - restore too fast and a slow app
        # pastes the old text.
        time.sleep(CLIPBOARD_RESTORE_DELAY)
    finally:
        # try/finally so the borrowed clipboard is always handed back, even if
        # the Ctrl+V injection raised - otherwise the dictated text (possibly
        # sensitive) would be left sitting on the clipboard.
        if backup is not None:
            _clipboard_restore(backup, private=private)
        elif previous_text:
            # Fallback path: only restore a non-empty string so we don't
            # clobber a non-text clipboard we couldn't capture.
            try:
                pyperclip.copy(previous_text)
            except Exception:
                pass


def press_line_break(times):
    """
    Insert `times` line breaks by pressing Shift+Enter - the near-universal
    "new line WITHOUT submitting" keystroke.

    A plain Enter would SEND the message in chat apps (Claude, Slack, Teams,
    Discord). Shift+Enter inserts a line break instead, and still works as a
    line break in text editors, word processors, and web text boxes - so it
    is the safe choice everywhere a person would dictate.
    """
    for _ in range(times):
        with kbd.pressed(keyboard.Key.shift):
            kbd.press(keyboard.Key.enter)
            kbd.release(keyboard.Key.enter)


def deliver(text, private=True):
    """
    Send `text` to the focused window - the single path both normal dictation
    and voice commands go through.

    The text is split on line breaks: the text pieces are delivered by paste
    or by typing (per PASTE_MODE), and each break is a Shift+Enter keystroke.
    We never paste a raw "\\n" (editors collapse pasted blank lines) and never
    press a plain Enter (it submits the message in chat apps). `private`
    marks the clipboard borrow as not-for-history (see paste_text).
    """
    parts = text.split("\n")
    with _injecting():          # our own keys: the hotkey must ignore them
        for index, part in enumerate(parts):
            if index > 0:
                press_line_break(1)
            if not part:
                continue
            if PASTE_MODE:
                paste_text(part, private=private)
            else:
                kbd.type(part)


def remove_fillers(text):
    """
    Strip filler sounds ("um", "uh", "er"...) from a transcription. Only the
    words in FILLER_WORDS are removed, and only as WHOLE words - the "um"
    inside "summary" is left alone. A comma or period stuck to a filler (as
    in Whisper's "Um, so...") is removed along with it.

    Meant to run BEFORE clean_text(): removing a filler leaves a gap or a
    double space behind, and clean_text() then collapses the whitespace and
    re-capitalizes whatever word is now first.
    """
    if not FILLER_WORDS:           # empty list -> removal disabled
        return text
    # Build one pattern: \b = word boundary, (?:a|b|c) = any one filler,
    # ,? = an optional comma clinging to it (as in Whisper's "Um, so..."),
    # re.IGNORECASE makes "Um" and "um" both match. We swallow a trailing
    # COMMA but NOT a trailing period: a period after a filler is usually the
    # end of the preceding sentence ("...I think um. Okay") and eating it
    # would merge two sentences. clean_text() then tidies the leftover space.
    #
    # A filler touching a hyphen or letter is part of a real word and stays
    # ("Uh-oh", "Mm-hmm"), and an ALL-CAPS token is an acronym, not a sound
    # ("UH" the university, "UM" the unit) - Whisper writes fillers as "um".
    pattern = (r"(?<![\w-])(?:" + "|".join(map(re.escape, FILLER_WORDS))
               + r")(?![\w-]),?")

    def drop(match):
        token = match.group(0).rstrip(",")
        return match.group(0) if len(token) > 1 and token.isupper() else ""
    return re.sub(pattern, drop, text, flags=re.IGNORECASE)


def clean_text(text):
    """
    Tidy a raw transcription with a few safe, deterministic rules - no AI, so
    the result is completely predictable. The rules only fix obvious
    formatting slips; they never rewrite, drop, or reorder words:
      * collapse any whitespace run (newlines, tabs, doubles) into one space
      * remove a stray space sitting in front of  , . ! ? ; :
      * capitalize a standalone lowercase "i"  ("i think" -> "I think")
      * capitalize the first letter of the text, and of each new sentence
    """
    # Collapse every run of whitespace into a single space, trim the ends.
    text = re.sub(r"\s+", " ", text).strip()
    # Drop punctuation orphaned at the very start - e.g. a leading filler
    # ("Um. So...") removed by remove_fillers() leaves a stray ". So...".
    text = re.sub(r"^[\s,.;:!?]+", "", text)
    if not text:
        return text

    # Drop a space that sits just before sentence punctuation
    # ("hello , there" -> "hello, there").
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)

    # Capitalize the word "i" only when it stands alone - but NOT the "i" in
    # an abbreviation like "i.e." or a dotted token. The lookarounds reject a
    # neighbouring word-char OR period, so "i'm"/"i'll" still become "I'm"/
    # "I'll" (an apostrophe is neither) while "i.e." is left untouched.
    text = re.sub(r"(?<![\w.])i(?![\w.])", "I", text)

    # Capitalize the very first letter of the whole text - unless it is a
    # single letter immediately followed by a period, i.e. a leading
    # abbreviation like "i.e." / "e.g." that should stay lowercase.
    text = re.sub(r"^[a-z](?!\.)", lambda m: m.group().upper(), text)
    # ...and the first letter right after a sentence end (. ! or ?). The
    # negative lookbehind skips an ABBREVIATION period (a single letter,
    # a dot, a single letter - "U.S.", "e.g.", "i.e."), so "U.S. economy"
    # is not mangled into "U.S. Economy".
    text = re.sub(
        r"(?<![A-Za-z]\.[A-Za-z])([.!?]\s+)([a-z])",
        lambda m: m.group(1) + m.group(2).upper(),
        text,
    )
    return text


def apply_vocabulary(text):
    """
    Fix mis-heard names, jargon, and casing using the corrections loaded from
    vocabulary.json. Each rule replaces a phrase - matched as whole words,
    case-insensitively - with its exact correct form, so "git hub" in any
    casing becomes "GitHub".

    Runs LAST in the pipeline, after clean_text(), so the exact casing from
    vocabulary.json is the final word - clean_text() cannot re-touch it.
    """
    return _apply_vocabulary_counted(text)[0]


def _apply_vocabulary_counted(text):
    """apply_vocabulary(), plus how many words it really changed (a word
    already spelled right isn't counted) - the history keeps that number for
    Insights' "the Dictionary fixed N words for you"."""
    fixed = [0]

    def replace(match, right):
        if match.group(0) != right:
            fixed[0] += 1
        return right
    for wrong, right in VOCAB_CORRECTIONS:
        pattern = r"\b" + re.escape(wrong) + r"\b"
        # The replacement is passed as a function (not a string) so any
        # special characters in 'right' - like the & in "A&M" - are inserted
        # literally, not read as a regex backreference.
        text = re.sub(pattern, lambda m, r=right: replace(m, r), text,
                      flags=re.IGNORECASE)
    return text, fixed[0]


def apply_voice_commands(text):
    """
    Turn spoken line-break commands into real line breaks. Runs LAST in the
    pipeline - clean_text() collapses whitespace, so a newline inserted any
    earlier would be wiped out.

    The tricky part: the pause you make while saying "new paragraph" reads to
    Whisper as a sentence break, so it usually transcribes punctuation around
    (and sometimes inside) the command - "...done. New paragraph. Next...".
    The match is therefore deliberately loose:
      * punctuation is allowed BETWEEN the command words ("new. paragraph")
      * any punctuation TRAILING the command is swallowed (it is spurious)
      * a leading space or comma is swallowed, but a leading . ! ? is KEPT -
        it legitimately ends the sentence before the break.
    """
    # Off by default: the inline matcher is loose enough to fire on ordinary
    # prose that merely contains "new line"/"new paragraph" (e.g. "start a new
    # line of code"), deleting the surrounding words. The whole-utterance path
    # (whole_utterance_command, run earlier in process_audio) is the reliable
    # way to get a line break, and it is always on. See INLINE_VOICE_COMMANDS.
    if not INLINE_VOICE_COMMANDS:
        return text

    between = r"[\s.,;:!?]+"   # space/punctuation Whisper may insert mid-phrase
    trailing = r"[\s.,;:!?]*"  # ...and any it sticks on the end of the command
    # Longest phrase first, so "new paragraph" is handled before "new line".
    for phrase in sorted(VOICE_COMMANDS, key=len, reverse=True):
        words = [re.escape(w) for w in phrase.split()]
        core = r"\b" + between.join(words) + r"\b"
        pattern = r"[\s,]*" + core + trailing
        text = re.sub(pattern, VOICE_COMMANDS[phrase], text,
                      flags=re.IGNORECASE)

    # Capitalize the first letter after each line break we inserted.
    text = re.sub(r"(\n)([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), text)
    return text


def whole_utterance_command(text):
    """
    If the ENTIRE dictation was nothing but a spoken command - you tapped the
    hotkey and said only "new paragraph" - return what it should insert
    ("\\n" or "\\n\\n"). Otherwise return None.

    This is the RELIABLE way to use voice commands: Whisper transcribes a
    short phrase said in isolation far more accurately than one buried inside
    a sentence. Because we know the whole utterance was meant as a command,
    matching can be loose - we keep only the letters and lowercase them, so
    "New paragraph.", "new  paragraph", "NEW PARAGRAPH" all count.
    """
    normalized = re.sub(r"[^a-z]", "", text.lower())
    for phrase, replacement in VOICE_COMMANDS.items():
        if normalized == re.sub(r"[^a-z]", "", phrase.lower()):
            return replacement
    return None


def whole_utterance_action(text):
    """
    If the ENTIRE dictation was an inline-correction command ("scratch
    that" / "fix that"), return its name. Otherwise return None.

    These are matched the same loose way as whole_utterance_command():
    we keep only the letters and compare, so "Scratch that.", "scratch,
    that", "SCRATCH THAT" all count. They are tested in process_audio()
    BEFORE the text-replacement command path so they never fall through
    into normal typing.
    """
    normalized = re.sub(r"[^a-z]", "", text.lower())
    for action in WHOLE_UTTERANCE_ACTIONS:
        if normalized == re.sub(r"[^a-z]", "", action.lower()):
            return action
    return None


def handle_whole_utterance_action(action):
    """
    Run an inline-correction action. We're already on a worker thread
    (process_audio's), so calling undo_last / Groq inline is fine - the
    hotkey listener stays responsive throughout.
    """
    if action == "scratch that":
        # Same effect as the Ctrl+Win+Z hotkey: backspace the last
        # dictation off the screen and drop its log entry.
        undo_last()
    elif action == "fix that":
        ai_fix_last_output()


# Why "fix that" couldn't polish, for its notice.
_FIX_REASONS = {
    "auth": "your Groq key was rejected",
    "forbidden": "your key isn't allowed to use its polish model",
    "gone": "its polish model isn't available",
    "rate_limited": "its free limit was reached",
    "timeout": "it took too long",
    "network": "it couldn't be reached",
    "server": "it returned an error",
}


def ai_fix_last_output():
    """
    "Fix that": polish the last delivered dictation (polish.py - the same
    Groq model and rules as every dictation), erase the original from the
    screen + log, and re-deliver the polished version in its place.

    Asked for on purpose, so it runs even for a short take or with the cloud
    switched off (a Groq key is still needed), gets a longer time budget,
    and always tells the user when it can't or when nothing changed.
    """
    global last_output, last_duration, last_output_hwnd, last_output_logged
    global last_output_app, _rejected_key

    # Snapshot what to fix under the same lock undo_last uses, so a
    # rapid second "fix that" can't see the same text twice.
    with undo_lock:
        original          = last_output
        original_duration = last_duration
        original_hwnd     = last_output_hwnd
        original_logged   = last_output_logged
        original_app      = last_output_app

    if not original or not original.strip():
        print("[FIX]   Nothing to fix.")
        return
    if not GROQ_API_KEY or GROQ_API_KEY == _rejected_key:
        notify("fix_failed", "Couldn't fix that",
               "\"Fix that\" needs a working Groq key - add one in Settings.",
               cooldown=0)
        return

    print("[FIX]   Polishing the last dictation via Groq...")
    try:
        cleaned = polish.polish(original.strip(), _get_groq_client(), _ranked_terms(),
                                name=USER_NAME, timeout=polish.FIX_TIMEOUT)
    except polish.PolishError as exc:
        # The user asked for this and is waiting: say what happened. (The
        # error log gets the kind only - never the dictated text.)
        storage.log_error("fix that", message=f"{exc.kind}: {exc.detail}")
        if exc.kind == "auth":
            _rejected_key = GROQ_API_KEY      # the same key Groq transcribes with
        if exc.kind == "suspicious":
            notify("fix_failed", "Nothing to fix", "Your text is unchanged.", cooldown=0)
        else:
            notify("fix_failed", "Couldn't fix that",
                   f"Groq couldn't polish it ({_FIX_REASONS.get(exc.kind, 'an error')}). "
                   f"Your text is unchanged.", cooldown=0)
        return
    except Exception as exc:                  # e.g. a broken groq install
        _write_error_log("ai_fix_last_output", exc)
        notify("fix_failed", "Couldn't fix that",
               "Something went wrong - details are in the error log. Your text "
               "is unchanged.", cooldown=0)
        return

    # Like every dictation: tidy it, and give the Dictionary the last word.
    # (Unchanged means unchanged by the model - or by all of it together.)
    if cleaned != original.strip():
        # Line by line: clean_text collapses whitespace, and a "fix that" on
        # an email or a note must keep the line breaks the speaker made.
        cleaned = apply_vocabulary("\n".join(clean_text(line) for line in cleaned.split("\n")))
    if cleaned == original.strip():
        notify("fix_failed", "Nothing to fix", "Your text is unchanged.", cooldown=0)
        return

    # Preserve the trailing-space convention the user set (so the next
    # dictation doesn't collide with the fixed one).
    cleaned_out = cleaned + " " if original.endswith(" ") else cleaned

    # Erase the original and type the cleaned text - all in the window it
    # was dictated into, verified first (never backspace into another app),
    # and under deliver_lock so a concurrent dictation can't interleave. We
    # do NOT call undo_last() here because it would re-read last_output at
    # delete time - and if a stray dictation lands during the Groq call, it
    # would backspace THAT one instead. The snapshotted length keeps the
    # delete targeted at what we actually fixed.
    _wait_for_modifiers_released()      # never fake releases: see undo_last
    with deliver_lock:
        if not restore_target_window(original_hwnd):
            notify("undo_failed", "Couldn't fix that",
                   "The window you dictated into isn't available, so nothing "
                   "was changed.", cooldown=0)
            return
        time.sleep(0.05)
        with _injecting():
            for _ in range(len(original)):
                kbd.press(keyboard.Key.backspace)
                kbd.release(keyboard.Key.backspace)
        deliver(cleaned_out, private=not _is_remote_client(original_app[1]))
    if original_logged:
        remove_last_log_entry()
    # Re-log under the original duration so the dashboard's WPM math stays
    # honest, and credit the same app.
    logged = False
    try:
        log_dictation(cleaned, original_duration, *original_app, raw=original.strip())
        logged = True
    except Exception as exc:
        _write_error_log("log_dictation", exc)
    with undo_lock:
        last_output, last_duration = cleaned_out, original_duration
        last_output_hwnd, last_output_logged = original_hwnd, logged
        last_output_app = original_app
    print(f"[FIX]   {cleaned}")


def build_transcribe_prompt():
    """
    Build the prompt handed to Whisper before each transcription: the
    punctuation style hint, followed by your vocabulary terms. Listing your
    names and jargon here biases Whisper toward producing them correctly in
    the first place (prevention), before apply_vocabulary() cleans up the
    rest (correction).
    """
    global _TRANSCRIBE_PROMPT_CACHE
    if _TRANSCRIBE_PROMPT_CACHE is None:
        if not VOCAB_TERMS:
            _TRANSCRIBE_PROMPT_CACHE = TRANSCRIBE_PROMPT
        else:
            # Keep terms in order until we reach the character budget, so a
            # large vocabulary can't crowd the punctuation hint out of
            # Whisper's ~224-token prompt window (see MAX_PROMPT_VOCAB_CHARS).
            kept, used = [], 0
            for term in reversed(_ranked_terms()):   # yours and the newest first
                add = len(str(term)) + 2          # +2 for the ", " separator
                if used + add > MAX_PROMPT_VOCAB_CHARS and kept:
                    break
                kept.append(str(term))
                used += add
            _TRANSCRIBE_PROMPT_CACHE = (
                TRANSCRIBE_PROMPT + " Vocabulary: " + ", ".join(kept) + "."
            )
    return _TRANSCRIBE_PROMPT_CACHE


# =============================================================================
#  TRANSCRIPTION DISPATCH  -  pick between local Whisper and cloud (Groq).
#
#  The audio always arrives here as a 1-D float32 numpy array at 16 kHz
#  (see stop_recording). Two backends:
#    - LOCAL  : faster-whisper running on CPU - the original behavior.
#    - CLOUD  : Groq's whisper-large-v3-turbo over HTTPS, ~$0.04/hour of
#               audio. Sub-second latency, higher accuracy than 'small.en'.
#
#  If cloud is enabled but anything goes wrong (no key, network down, API
#  error), we transparently fall back to local Whisper so dictation never
#  silently fails. Local is the safety net.
# =============================================================================

# The Groq client is reused across dictations - making a new one per call
# would pay the TLS handshake cost every time. We also remember the key it
# was built with, so if you paste a new key in Settings the next dictation
# rebuilds the client instead of using a stale one.
_groq_client = None
_groq_client_key = None

# Cloud health, shared by the final call and the speculative worker.
cloud_paused_until = 0.0   # time.monotonic() before which the cloud is skipped
_rejected_key = None       # the API key Groq last rejected (warn once per key)

# ElevenLabs health, like Groq's above (see _handle_eleven_error).
eleven_paused_until = 0.0     # time.monotonic() before which ElevenLabs is skipped
_eleven_rejected_key = None   # the ElevenLabs key it last rejected
# AI polish has its own pause: its Groq model has its own limits, so a
# polish problem never stops Groq from transcribing (see _handle_polish_error).
polish_paused_until = 0.0

# Which service produced the text of the take THIS thread is transcribing
# ("groq" / "local"), set by transcribe_audio and read by _transcribe_take
# for the history log. Thread-local: dictations transcribe in parallel.
_engine = threading.local()


class TranscriptionFailed(Exception):
    """No backend could transcribe the audio. str(exc) is a user-facing reason."""


def cloud_available():
    """True when Groq can be used right now - as the main service, or as
    ElevenLabs' backup: the cloud is on, Groq has a key it hasn't rejected,
    and it isn't paused after a rate limit or network failure."""
    return (bool(USE_CLOUD and GROQ_API_KEY)
            and GROQ_API_KEY != _rejected_key
            and time.monotonic() >= cloud_paused_until)


def _groq_configured():
    """Groq is switched on with a key it hasn't rejected - as the main
    service or as ElevenLabs' backup. (It may be paused - transcribe_audio
    still tries it when it's the only backend.)"""
    return bool(USE_CLOUD and GROQ_API_KEY) and GROQ_API_KEY != _rejected_key


def _eleven_configured():
    """ElevenLabs is the cloud service, with a key it hasn't rejected."""
    return (bool(USE_CLOUD and CLOUD_PROVIDER == "elevenlabs" and ELEVENLABS_API_KEY)
            and ELEVENLABS_API_KEY != _eleven_rejected_key)


def eleven_available():
    """ElevenLabs is configured and not paused after a failure."""
    return _eleven_configured() and time.monotonic() >= eleven_paused_until


def _cloud_configured():
    """A cloud service could transcribe - either one (it may be paused)."""
    return _groq_configured() or _eleven_configured()


def _other_backend():
    """Something besides ElevenLabs could transcribe a take: Groq, or the
    local model - even one still loading from disk (transcribe_audio waits
    those few seconds for it)."""
    return (_groq_configured() or _local_model() is not None
            or models.state == "loading")


def _groq_pipeline_on():
    """Groq's speculative pipeline runs only when Groq is the main service -
    next to an ElevenLabs stream it would only spend Groq calls."""
    return PIPELINE_CLOUD and CLOUD_PROVIDER == "groq" and cloud_available()


def _percent(snap):
    """A model snapshot's download progress, 0-100."""
    return int(snap["done"] * 100 / snap["total"]) if snap["total"] else 0


def _dictation_blocker():
    """
    None if a dictation started now could be transcribed; otherwise the
    notice explaining why not, as (key, title, message, cooldown). The
    controller shows it when the hotkey is RELEASED - and only if no other
    key joined in: Ctrl + Win + arrow is a desktop switch, not a question.
    """
    if setup_pending:
        return ("setup_pending", "Finish setting up Scribe",
                "Choose how Scribe should listen, then you can start dictating.", 15)
    if _cloud_configured() or _local_model() is not None:
        return None
    snap = models.snapshot()
    if snap["state"] == "loading":
        return None                       # seconds away: transcription waits
    if snap["state"] == "downloading":
        return ("model_not_ready", "Scribe is still getting ready",
                f"The speech model is {_percent(snap)}% downloaded. Dictation "
                f"works as soon as it's done.", 15)
    if snap["state"] == "failed":
        return ("model_failed", _failed_title(snap),
                f"{snap['error']} Open the dashboard to try again.", 15)
    return ("no_backend", "Scribe can't transcribe right now",
            "Cloud transcription isn't available and there's no local "
            "model. Check Settings in the dashboard.", 15)


def _show_blocked_notice(notice):
    """Show a blocked press's notice (controller thread; notify is safe)."""
    global _model_wait_told
    key, title, message, cooldown = notice
    if key == "model_not_ready":
        _model_wait_told = True           # ...so "Scribe is ready" follows
    notify(key, title, message, cooldown=cooldown)


def _retry_after_seconds(exc):
    """Seconds a 429 asked us to wait (its retry-after header), or None."""
    try:
        value = float(exc.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None
    return max(1.0, min(value, 3600.0))


def _human_duration(seconds):
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} seconds"
    return f"{round(seconds / 60)} minutes"


def _handle_cloud_error(exc):
    """
    Decide what a failed Groq call means for the next few dictations, and
    tell the user once. The caller then falls back to the local model.
      - key rejected  -> stop using that key until it changes in Settings
      - rate limited  -> pause the cloud for as long as Groq asks
      - network / 5xx -> pause the cloud briefly, so an offline laptop
                         doesn't wait out a connect timeout every dictation
    """
    global cloud_paused_until, _rejected_key
    fallback = ("Using the local model for now." if _local_model() is not None
                else "Scribe can't transcribe until it's back.")
    try:
        import groq                # only reached after a real cloud call
    except ImportError:
        # A broken install: the cloud can't work until it's reinstalled.
        # Pause it for the session and let local take over.
        storage.log_error("cloud transcription (groq not installed)", exc)
        cloud_paused_until = time.monotonic() + 3600
        notify("cloud_broken", "Cloud transcription unavailable",
               "Scribe's cloud component isn't installed correctly. Run "
               f"setup.bat again to repair it. {fallback}")
        return
    if isinstance(exc, (groq.AuthenticationError, groq.PermissionDeniedError)):
        _rejected_key = GROQ_API_KEY
        notify("key_rejected", "Groq API key rejected",
               f"Check the key in Settings. {fallback}", cooldown=0)
    elif isinstance(exc, groq.RateLimitError):
        wait = _retry_after_seconds(exc) or CLOUD_DEFAULT_RATE_LIMIT_PAUSE
        cloud_paused_until = time.monotonic() + wait
        then = ("Using the local model until then." if _local_model() is not None
                else fallback)
        notify("rate_limited", "Cloud limit reached",
               f"Groq asked Scribe to wait {_human_duration(wait)}. {then}")
    elif isinstance(exc, groq.APIConnectionError) or (
            isinstance(exc, groq.APIStatusError) and exc.status_code >= 500):
        cloud_paused_until = time.monotonic() + CLOUD_PAUSE_AFTER_ERROR_SECONDS
        notify("cloud_down", "Cloud transcription unavailable",
               "Couldn't reach Groq, so Scribe used the local model."
               if _local_model() is not None else f"Couldn't reach Groq. {fallback}")
    elif isinstance(exc, groq.APIStatusError):
        # Anything else Groq refused (e.g. a retired or mistyped model - the
        # way Groq retired the "fix that" model). Silent local fallback would
        # hide it forever, so say so.
        storage.log_error("cloud transcription", exc)
        hint = (" - check the model in Settings"
                if exc.status_code == 404 or "model" in str(exc).lower() else "")
        detail = str(getattr(exc, "message", "") or exc)[:120]
        notify("cloud_rejected", "Groq couldn't transcribe that",
               f"Groq returned an error ({exc.status_code}: {detail}){hint}. "
               f"{fallback}")
    else:
        storage.log_error("cloud transcription", exc)


def _get_groq_client():
    """
    Return a cached Groq client, rebuilding it if the API key has changed.
    Imports `groq` lazily so a missing dependency only matters when cloud
    mode is actually used - the local-only install path still works.
    """
    global _groq_client, _groq_client_key
    key = GROQ_API_KEY      # read once: Settings may swap it while we build
    if _groq_client is None or _groq_client_key != key:
        import httpx            # lazy, like groq: only when cloud is used
        from groq import Groq   # lazy import: only loaded when cloud is used
        _groq_client = Groq(
            api_key=key,
            timeout=httpx.Timeout(CLOUD_TIMEOUT_SECONDS,
                                  connect=CLOUD_CONNECT_TIMEOUT_SECONDS),
            max_retries=0,          # we decide about retries, not the SDK
        )
        _groq_client_key = key
    return _groq_client


def _audio_to_wav_bytes(audio):
    """
    Encode the recorded audio (1-D float32 in [-1, 1] at SAMPLE_RATE) as
    an in-memory WAV file. Groq's transcription endpoint takes a file-like
    upload; WAV is the simplest format that needs no extra dependency
    (Python's stdlib `wave` module handles it). We clip first so any
    out-of-range sample from a hot mic doesn't wrap around to noise.
    """
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)           # 2 bytes = int16 samples
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def trim_silence(audio):
    """
    Return `audio` with its non-speech stretches cut out, or None if it holds
    no speech at all. Used before every Groq upload - see the silence-
    trimming note in SETTINGS for why Whisper must never be handed dead air.

    The Silero detector marks where speech is (as sample offsets, padded by
    CLOUD_VAD_SPEECH_PAD_MS), and we glue just those stretches together.
    The dead air at the start (hotkey down, then a beat before you speak) and
    at the end (you finish, then release) is exactly where the phantom
    "Thank you for watching!" used to come from.
    """
    try:
        spans = get_speech_timestamps(audio, VadOptions(
            min_silence_duration_ms=CLOUD_VAD_MIN_SILENCE_MS,
            speech_pad_ms=CLOUD_VAD_SPEECH_PAD_MS,
        ))
    except Exception as exc:
        # Trimming is a quality nicety, never a gate: if the detector itself
        # breaks, send the untrimmed audio rather than lose the dictation.
        print(f"[VAD]   Failed ({exc!r}); sending untrimmed audio.")
        return audio
    if not spans:
        return None
    return np.concatenate([audio[s["start"]:s["end"]] for s in spans])


def transcribe_local(audio):
    """
    Transcribe with the on-device faster-whisper model. The original Scribe
    path - always works offline, no key needed.
    """
    local = _local_model()
    if local is None:
        # Not loaded (yet), or cloud-only. Signal up so the caller can cope.
        raise RuntimeError("local Whisper model is not loaded")
    # vad_filter=True drops silent gaps so Whisper doesn't "hallucinate"
    # stray words during silence. initial_prompt nudges punctuation/casing
    # and biases the model toward our vocabulary terms.
    # condition_on_previous_text=False stops Whisper from feeding one 30s
    # window's tokens into the next as context - the classic cause of
    # repetition loops / hallucinated runs on long dictations. For push-to-
    # talk this only helps: a sub-30s utterance is a single window (no-op),
    # and a longer one no longer risks a runaway repeat.
    # The lock serializes access to the shared model object; the generator is
    # consumed (joined) inside the lock because iterating it is what actually
    # runs the model.
    with transcribe_lock:
        segments, _info = local.transcribe(
            audio, language="en", vad_filter=True,
            initial_prompt=build_transcribe_prompt(),
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


def transcribe_cloud(audio):
    """
    Transcribe via Groq's hosted Whisper. Raises on any failure (missing
    key, network error, API rejection) so the caller can fall back to
    local. Groq's `prompt` parameter is the same idea as Whisper's
    `initial_prompt`, so vocabulary priming still applies.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("Groq API key is not set (Settings -> API key).")
    # Cut the dead air out BEFORE upload: Whisper invents words in silence
    # (see trim_silence). A recording with no speech at all never reaches
    # Groq - no phantom "Thank you.", and no quota spent on nothing.
    speech = trim_silence(audio)
    if speech is None:
        print("[CLOUD] No speech detected - skipped the Groq call.")
        return ""
    wav_bytes = _audio_to_wav_bytes(speech)
    client = _get_groq_client()
    # The 3-tuple form (filename, content, mime) is what Groq's SDK expects
    # for an in-memory upload. The filename is cosmetic but required.
    result = client.audio.transcriptions.create(
        file=("audio.wav", wav_bytes, "audio/wav"),
        model=CLOUD_MODEL,
        language="en",
        prompt=build_transcribe_prompt(),
    )
    # Record this call against the free-tier rate limits so the dashboard
    # can show how much of today's quota is left. Logged AFTER the API
    # returns so we only count calls that actually reached Groq, and in
    # TRIMMED seconds - that is what Groq bills against the quota.
    log_cloud_call(len(speech) / SAMPLE_RATE, CLOUD_MODEL)
    return (result.text or "").strip()


def transcribe_audio(audio):
    """
    Run transcription through whichever backend is available: the cloud
    first when enabled and healthy, falling back to local on any failure -
    so a flaky network never blocks a dictation. Raises TranscriptionFailed
    (with a readable reason) when neither can run - never returns a silent
    "" for a failure.
    """
    # With no local model, the cloud is the only backend: keep trying it even
    # during a pause (a rejected key is still skipped - it can't succeed).
    only_backend = (_local_model() is None and bool(USE_CLOUD and GROQ_API_KEY)
                    and GROQ_API_KEY != _rejected_key)
    if cloud_available() or only_backend:
        try:
            text = transcribe_cloud(audio)
            _engine.name = "groq"
            return text
        except Exception as exc:
            print(f"[CLOUD] Failed ({exc!r}); using local model.")
            _handle_cloud_error(exc)
    if _local_model() is None and models.state == "loading":
        # Loading from disk takes a few seconds - worth waiting for, rather
        # than failing a dictation that was recorded while it loaded. (Only
        # loading: a download can take minutes.)
        models.wait_loaded(MODEL_LOAD_WAIT_SECONDS)
    if _local_model() is None:
        snap = models.snapshot()
        if snap["state"] == "downloading":
            raise TranscriptionFailed(
                f"The speech model is still downloading ({_percent(snap)}%).")
        raise TranscriptionFailed(
            "Cloud transcription isn't available right now and there's no "
            "local model to fall back on.")
    text = transcribe_local(audio)
    _engine.name = "local"
    return text


# =============================================================================
#  ELEVENLABS STREAMING  -  the take is transcribed WHILE you talk.
#
#  When ElevenLabs is the cloud service, start_recording opens a Session
#  (elevenlabs_stream.py) and audio_callback feeds it every chunk; on
#  release, _transcribe_take asks it for the final text - usually ready a few
#  hundred milliseconds later. Scribe still records the take itself, so if
#  ElevenLabs can't answer, the same audio goes to Groq or the local model
#  and nothing is lost.
# =============================================================================

def _start_stream_session():
    """Open this dictation's ElevenLabs stream (it connects on its own
    thread). None if even that failed - the take then goes the usual way."""
    try:
        terms = elevenlabs_stream.keyterms(_ranked_terms(), USER_NAME)
        return elevenlabs_stream.Session(ELEVENLABS_API_KEY, terms).start()
    except Exception as exc:
        storage.log_error("start ElevenLabs stream", exc)
        return None


def _take_stream_session():
    """Detach the current dictation's stream and return it (controller thread)."""
    global _stream_session
    session, _stream_session = _stream_session, None
    return session


# What the fallback did, for the end of ElevenLabs' notice - said only once
# it really happened (Groq might be unreachable too).
_USED_INSTEAD = {"groq": "Scribe used Groq instead.",
                 "local": "Scribe used the local model instead."}


# Why a take failed, for the "Couldn't transcribe that" notice when
# ElevenLabs was the only way to transcribe it.
_ELEVEN_REASONS = {
    "auth": "ElevenLabs rejected your API key.",
    "quota": "Your ElevenLabs hours for this month are used up.",
    "busy": "ElevenLabs is busy right now.",
    "terms": "ElevenLabs needs you to accept its terms at elevenlabs.io.",
    "network": "Couldn't reach ElevenLabs.",
    "timeout": "Couldn't reach ElevenLabs.",
    "broken": "Scribe's streaming component isn't installed correctly.",
}


def _eleven_reason(exc):
    return _ELEVEN_REASONS.get(exc.kind, "ElevenLabs couldn't transcribe that.")


def _handle_eleven_error(exc, key=None):
    """
    Decide what a failed ElevenLabs take means for the next few dictations:
      - key rejected         -> stop using that key until it changes
      - hours used up, terms -> skip ElevenLabs for an hour, then try again
      - busy                 -> skip it for a minute
      - network / too slow   -> skip it for 30 s, so an offline laptop
                                doesn't wait out a timeout every dictation
      - anything else        -> just this take; the next one tries again
    Returns the notice for the user as (key, title, message, cooldown) - the
    caller shows it once it knows what the fallback did - or None when there
    is nothing new to say. `key` is the API key the failed take used.
    """
    global eleven_paused_until, _eleven_rejected_key
    storage.log_error("ElevenLabs", message=f"{exc.kind}: {exc.detail}")
    now = time.monotonic()
    kind = exc.kind
    if kind == "auth":
        # Reject the key THIS take used - the user may have pasted a new one
        # since - and say so once per key (overlapping takes fail together).
        key = key or ELEVENLABS_API_KEY
        told = key == _eleven_rejected_key
        _eleven_rejected_key = key
        if told or key != ELEVENLABS_API_KEY:
            return None
        return ("eleven_key_rejected", "ElevenLabs API key rejected",
                "Check the key in Settings.", 0)
    if kind == "quota":
        eleven_paused_until = now + 3600
        return ("eleven_quota", "ElevenLabs time used up",
                "Your plan's hours for this month are used up.", 6 * 3600)
    if kind == "busy":
        eleven_paused_until = now + 60
        return ("eleven_busy", "ElevenLabs is busy", "", 600)
    if kind == "terms":
        eleven_paused_until = now + 3600
        return ("eleven_terms", "Accept ElevenLabs' terms",
                "Sign in at elevenlabs.io and accept the terms.", 3600)
    if kind in ("network", "timeout"):
        eleven_paused_until = now + CLOUD_PAUSE_AFTER_ERROR_SECONDS
        return ("eleven_down", "Couldn't reach ElevenLabs", "", 600)
    if kind == "broken":
        eleven_paused_until = now + 3600
        return ("eleven_broken", "Cloud transcription unavailable",
                "Scribe's streaming component isn't installed correctly. Run "
                "setup.bat again to repair it.", 3600)
    return ("eleven_error", "ElevenLabs couldn't transcribe that",
            f"ElevenLabs returned an error ({exc.detail[:120]}).", 600)


def _polish_on():
    """AI polish can run now: switched on, the cloud on with a usable Groq
    key (cloud_available() - a local-only user keeps their text on this PC),
    and polish not paused after a failure."""
    return POLISH and cloud_available() and time.monotonic() >= polish_paused_until


def _polish_take(text):
    """
    The take's text after AI polish, and whether polish changed it. Skipped
    (text unchanged) when polish can't run or the take is too short; on any
    failure the words stay as spoken and _handle_polish_error explains.
    """
    if not (_polish_on() and polish.should_polish(text)):
        return text, False
    t0 = time.perf_counter()
    try:
        out = polish.polish(text, _get_groq_client(), _ranked_terms(), name=USER_NAME,
                            style=POLISH_STYLE)
    except polish.PolishError as exc:
        print(f"[POLISH] Skipped ({exc.kind}).")
        _handle_polish_error(exc)
        return text, False
    except Exception as exc:              # e.g. a broken groq install
        storage.log_error("AI polish", exc)
        _handle_polish_error(polish.PolishError("broken", type(exc).__name__))
        return text, False
    print(f"[POLISH] {(time.perf_counter() - t0) * 1000:.0f} ms.")
    return out, out != text


def _warm_polish():
    """
    Get Groq ready in the background - mainly the first `import groq`
    (~0.3 s; with ElevenLabs transcribing, nothing else would pay it before
    the first polish), plus a connection that helps a polish in the next few
    seconds (an idle one is closed after ~5 s). Returns the thread, or None
    when polish can't run.
    """
    if not _polish_on():
        return None

    def warm():
        try:
            _get_groq_client().models.list()      # free: no quota, no audio
        except Exception:
            pass                                  # the real polish reports problems
    thread = threading.Thread(target=warm, name="polish-warmup", daemon=True)
    thread.start()
    return thread


def _handle_polish_error(exc):
    """
    What a failed polish means - the words were typed as spoken either way:
      - key rejected -> stop using that Groq key (it's the same key Groq
                        transcribes with) until it changes in Settings
      - forbidden, gone, broken -> the key may not use the polish model,
                        the model was retired, or Scribe's groq package is
                        broken: pause polish until the key or the polish
                        switch changes (Groq keeps transcribing)
      - rate limited -> pause polish for as long as Groq asks
      - network / 5xx -> pause polish 30 s
      - too slow      -> nothing; the next take tries again
      - suspicious    -> the answer didn't look like a cleanup: just logged
                         (you got your own words, which is right)
    Only polish pauses - Groq transcription has its own limits.
    """
    global polish_paused_until, _rejected_key
    # The kind and detail only - never the dictated text.
    storage.log_error("AI polish", message=f"{exc.kind}: {exc.detail}")
    kind = exc.kind
    if kind == "auth":
        _rejected_key = GROQ_API_KEY
        notify("key_rejected", "Groq API key rejected",
               "Check the key in Settings. Your dictations are typed without AI "
               "polish until then.", cooldown=0)
    elif kind == "forbidden":
        polish_paused_until = float("inf")    # until Settings change (reload_config)
        notify("polish_forbidden", "AI polish unavailable",
               f"Groq didn't allow its polish model ({polish.MODEL}) for your key, so "
               f"your words are typed as spoken. Check the model's permissions at "
               f"console.groq.com, or turn off AI polish in Settings.", cooldown=0)
    elif kind == "gone":
        polish_paused_until = float("inf")
        notify("polish_gone", "AI polish unavailable",
               f"Groq no longer offers the polish model ({polish.MODEL}), or refused "
               f"the request, so your words are typed as spoken. Update Scribe, or "
               f"turn off AI polish in Settings.", cooldown=0)
    elif kind == "broken":
        polish_paused_until = float("inf")
        notify("polish_broken", "AI polish unavailable",
               "Scribe's cloud component isn't installed correctly, so your words "
               "are typed as spoken. Run setup.bat again to repair it, or turn off "
               "AI polish in Settings.", cooldown=0)
    elif kind == "rate_limited":
        wait = exc.retry_after or CLOUD_DEFAULT_RATE_LIMIT_PAUSE
        polish_paused_until = time.monotonic() + wait
        notify("polish_limited", "AI polish paused",
               f"Groq's free limit for polishing was reached. Scribe types your "
               f"words as spoken for {_human_duration(wait)}.")
    elif kind in ("network", "server"):
        polish_paused_until = time.monotonic() + CLOUD_PAUSE_AFTER_ERROR_SECONDS
        notify("polish_down", "AI polish unavailable",
               "Couldn't reach Groq, so your words were typed as spoken."
               if kind == "network" else
               f"Groq returned an error ({exc.detail[:120]}), so your words were "
               f"typed as spoken.")
    elif kind == "timeout":
        notify("polish_slow", "AI polish skipped",
               "Groq took too long, so your words were typed as spoken.")


def _transcribe_take(audio, session):
    """
    The text for one take, and which service produced it ("elevenlabs",
    "groq" or "local"). A stream is finished first; if ElevenLabs can't
    answer, the full recording goes down the usual path - Groq, then the
    local model. ElevenLabs' failure notice waits until the fallback has
    answered, so it says what REALLY happened ("Scribe used Groq instead");
    when nothing could transcribe, TranscriptionFailed carries both reasons
    and the caller's one notice says it all.
    """
    failure = None
    if session is not None:
        try:
            text = session.finish(elevenlabs_stream.FINAL_TIMEOUT)
            print(f"[STREAM] Text {session.final_ms or 0:.0f} ms after release "
                  f"(connected in {session.connect_ms or 0:.0f} ms).")
            return text, "elevenlabs"
        except elevenlabs_stream.StreamError as exc:
            print(f"[STREAM] Failed ({exc}); falling back.")
            failure = (exc, _handle_eleven_error(
                exc, key=getattr(session, "api_key", None)))
            if not _other_backend():
                raise TranscriptionFailed(_eleven_reason(exc)) from None
        finally:
            # What was streamed is billed, text or not - it counts toward
            # the dashboard's monthly meter.
            if session.streamed_seconds:
                log_cloud_call(session.streamed_seconds, elevenlabs_stream.MODEL_ID,
                               provider="elevenlabs")
    _engine.name = None
    try:
        text = _use_speculative_or_transcribe(audio)
    except TranscriptionFailed as exc:
        if failure is None:
            raise
        raise TranscriptionFailed(f"{_eleven_reason(failure[0])} {exc}") from None
    engine = getattr(_engine, "name", None)
    if failure is not None and failure[1] is not None:
        key, title, message, cooldown = failure[1]
        used = _USED_INSTEAD.get(engine, "")
        notify(key, title, f"{message} {used}".strip(), cooldown=cooldown)
    return text, engine


# =============================================================================
#  CLOUD PIPELINING  -  speculative transcription while the user is talking.
#
#  The worker thread below spawns in start_recording() (when cloud + pipeline
#  are both on) and keeps a "rolling" Groq transcription warm. Each cycle it:
#    1. Snapshots the audio captured so far.
#    2. Sends it to Groq (a normal transcribe_cloud call).
#    3. Stores (audio_length_samples, text) in speculative_result.
#    4. Waits PIPELINE_INTERVAL seconds (or until stop_recording() fires).
#
#  When the user releases the hotkey, _use_speculative_or_transcribe() in
#  process_audio waits briefly for any in-flight call to land, then either
#  RETURNS THE SPECULATIVE (covering >= total - PIPELINE_MAX_GAP seconds)
#  or falls back to a fresh final transcribe_audio() if it's too stale.
#  Either way the rest of the dictation flow (post-processing, voice
#  commands, undo) is unchanged.
# =============================================================================

def _speculative_worker(generation):
    """
    Run from start_recording to stop_recording. Each iteration sends the
    audio-so-far to Groq in this thread (blocking on the HTTP call) and
    writes the result to speculative_result. Exceptions are swallowed
    silently - the final transcription in process_audio is the safety
    net for any of these calls that fail.

    `generation` is the recording-session id captured when this worker was
    spawned. Every loop and every result-write is fenced on it still matching
    the current speculative_generation, so a worker that wakes from a slow
    Groq call after the NEXT dictation has begun exits instead of leaking into
    it (no zombie second worker, no cross-dictation text contamination).

    Quota discipline (the whole point of the rewrite): at most
    PIPELINE_MAX_SPEC_CALLS Groq calls per dictation; a call is skipped unless
    PIPELINE_MIN_NEW_AUDIO_SECONDS of fresh audio has arrived since the last
    one; and speculation stops entirely once today's free-tier usage is in the
    warn/danger band. Each call still re-uploads cumulative audio, so these
    bounds are what keep a dictation from fanning out into many billed calls.
    """
    global speculative_result

    # Don't kick the first call immediately - very short dictations
    # would just waste a Groq round-trip. recording_stop_event.wait()
    # returns True if the user releases during this initial pause.
    if recording_stop_event.wait(2.0):
        return

    calls_made = 0
    last_spec_len = 0
    while recording and generation == speculative_generation:
        # The setting can flip mid-recording (the dashboard's
        # signal-reload). Bail out as soon as that happens.
        if not _groq_pipeline_on():
            return
        # Stop speculating once we're close to a daily Groq limit - the
        # latency nicety isn't worth pushing the user over the free tier.
        if quota_state != "ok":
            return
        # Bounded fan-out: never make more than the cap per dictation.
        if calls_made >= PIPELINE_MAX_SPEC_CALLS:
            return
        if not frames:
            if recording_stop_event.wait(PIPELINE_INTERVAL):
                return
            continue
        try:
            # list(frames) snapshots the chunk list so a concurrent append by
            # the audio thread can't mutate it mid-concatenate.
            snapshot = np.concatenate(list(frames)).flatten()
        except ValueError:
            if recording_stop_event.wait(PIPELINE_INTERVAL):
                return
            continue
        # Skip very short snapshots - the per-call overhead would
        # dominate any savings.
        if len(snapshot) < SAMPLE_RATE * 1.0:
            if recording_stop_event.wait(PIPELINE_INTERVAL):
                return
            continue
        # Skip a cycle that has barely more audio than the last one we already
        # transcribed - re-uploading near-identical audio just burns quota.
        if len(snapshot) - last_spec_len < SAMPLE_RATE * PIPELINE_MIN_NEW_AUDIO_SECONDS:
            if recording_stop_event.wait(PIPELINE_INTERVAL):
                return
            continue
        try:
            t0 = time.time()
            text = transcribe_cloud(snapshot)
            t1 = time.time()
            calls_made += 1
            last_spec_len = len(snapshot)
            # Only publish if THIS dictation is still the current one - and
            # only if it heard something. Empty text means the audio so far
            # was all silence (trim_silence found no speech); publishing it
            # would let a release moments later reuse "" and drop words you
            # spoke after the snapshot. Unpublished, the release does a fresh
            # transcription of the whole recording instead.
            with speculative_lock:
                if text and generation == speculative_generation:
                    speculative_result = (len(snapshot), text)
            print(f"[SPEC]  {len(snapshot)/SAMPLE_RATE:.1f}s of audio -> "
                  f"{len(text)} chars in {(t1-t0)*1000:.0f}ms")
        except Exception as exc:
            # Silent: the final transcription in process_audio is the
            # safety net. Printing too loudly here would spam the
            # console for transient network blips.
            print(f"[SPEC]  Skipped ({exc}).")
            _handle_cloud_error(exc)
        if recording_stop_event.wait(PIPELINE_INTERVAL):
            return


def _use_speculative_or_transcribe(audio):
    """
    Return the final transcription for `audio`. When cloud pipelining is
    on AND the speculative worker stored a result that covers nearly all
    of the audio, hand back the speculative text directly (saves the
    full Groq round-trip on release). Otherwise fall through to a fresh
    transcribe_audio() like the un-pipelined path.
    """
    if not _groq_pipeline_on():
        return transcribe_audio(audio)

    # The user just released. The worker might still be inside a Groq
    # call - give it a brief window to finish so we can use that result
    # instead of starting a new transcription. The timeout is the cap;
    # if the call returns sooner, join() returns sooner.
    if speculative_thread is not None:
        speculative_thread.join(timeout=PIPELINE_MAX_GAP)

    with speculative_lock:
        spec = speculative_result

    if spec is not None:
        spec_len, spec_text = spec
        # Gap between the speculative's audio length and the total audio. The
        # generation fence guarantees spec belongs to THIS dictation, so
        # spec_len <= len(audio) and the gap is >= 0.
        gap_seconds = (len(audio) - spec_len) / SAMPLE_RATE
        if gap_seconds <= PIPELINE_MAX_GAP:
            # The speculative only transcribed audio[:spec_len]; the trailing
            # audio[spec_len:] (up to PIPELINE_MAX_GAP seconds) was never sent
            # to Groq. That tail is often the user's final word, so instead of
            # silently dropping it, transcribe JUST the tail on the free local
            # model and stitch it on - zero extra Groq cost, no lost word.
            tail = audio[spec_len:]
            stitched = spec_text
            if _local_model() is not None and len(tail) >= SAMPLE_RATE * 0.3:
                try:
                    tail_text = transcribe_local(tail)
                except Exception as exc:
                    print(f"[CLOUD] Tail transcription failed ({exc}).")
                    tail_text = ""
                if tail_text:
                    stitched = (spec_text + " " + tail_text).strip()
            print(f"[CLOUD] Used speculative ({spec_len/SAMPLE_RATE:.1f}s of "
                  f"{len(audio)/SAMPLE_RATE:.1f}s) + local tail "
                  f"({len(tail)/SAMPLE_RATE:.1f}s).")
            _engine.name = "groq"
            return stitched
        print(f"[CLOUD] Speculative too stale (gap {gap_seconds:.1f}s); "
              f"doing a fresh transcription.")

    return transcribe_audio(audio)


# =============================================================================
#  DELIVERY  -  in the order spoken, into a window that is really in front.
# =============================================================================

# Each finished recording gets a sequence number (see _finish_recording).
# A dictation may finish transcribing before an earlier, longer one - but
# the text must still appear in the order it was spoken, so each job waits
# for its turn. Skipped jobs (too short, no speech, failures) still "take"
# their turn, so they never hold up the next one.
_turn_cond = threading.Condition()
_next_turn = 0            # the sequence number whose turn it is
_finished_seqs = set()    # jobs that finished out of order, waiting to count
# If an earlier job somehow never finishes, stop waiting after this long.
DELIVERY_TURN_TIMEOUT = 120
# Pasting while the user still holds Win would send Win+V (and Ctrl+V with
# Alt held means something else again) - so wait for the modifiers to come
# up. They're held for as long as the NEXT dictation is being recorded.
MODIFIER_WAIT_MAX = MAX_RECORDING_SECONDS + 10


def _wait_turn(seq):
    """Block until every dictation spoken before `seq` has finished."""
    global _next_turn
    if seq is None:
        return
    with _turn_cond:
        if not _turn_cond.wait_for(lambda: _next_turn >= seq,
                                   timeout=DELIVERY_TURN_TIMEOUT):
            storage.log_error("delivery order", message=f"dictation #{seq} "
                              f"waited too long for an earlier one; going ahead")
            _next_turn = seq


def _finish_turn(seq):
    """Mark `seq` finished (delivered or skipped) and let the next one go."""
    global _next_turn
    if seq is None:
        return
    with _turn_cond:
        _finished_seqs.add(seq)
        while _next_turn in _finished_seqs:
            _finished_seqs.discard(_next_turn)
            _next_turn += 1
        _finished_seqs.difference_update({s for s in _finished_seqs if s < _next_turn})
        _turn_cond.notify_all()


def _wait_for_modifiers_released(timeout=None):
    """Wait until Ctrl/Alt/Shift/Win are all physically up. Returns False if
    they're still held after `timeout` seconds (default MODIFIER_WAIT_MAX)."""
    if timeout is None:
        timeout = MODIFIER_WAIT_MAX
    deadline = time.monotonic() + timeout
    while _any_modifier_down():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


# Remote Desktop, VM and remote-control windows copy the clipboard to the
# other machine by WATCHING it - a borrow marked "don't monitor" might never
# reach the remote side, which would then paste ITS old clipboard. Dictating
# into one of these gets a normal borrow.
_REMOTE_CLIENT_EXES = {
    "mstsc.exe", "msrdc.exe", "vmconnect.exe", "vmware.exe",
    "vmware-remotemks.exe", "vmplayer.exe", "virtualboxvm.exe",
    "parsecd.exe", "anydesk.exe", "teamviewer.exe",
}


def _is_remote_client(app_exe):
    return bool(app_exe) and os.path.basename(app_exe).lower() in _REMOTE_CLIENT_EXES


def _copy_for_user(text):
    """Leave `text` on the clipboard for the user to paste themselves - a
    normal copy (it may appear in clipboard history, like any copy)."""
    if not _clipboard_set_text(text, private=False):
        storage.log_error("copy for user", message="clipboard busy; text not copied")
        return False
    return True


def _deliver_job(output, hwnd, app_name, app_exe, seq, duration, log_text,
                 engine=None, released_at=None, raw=None, fixed=0):
    """
    Deliver one dictation's `output` - in its turn, once the modifiers are
    up, into its own window - or, if that window is gone, leave it on the
    clipboard and say so. `log_text` (None for a voice command's line break)
    is what goes into the history, with `engine` (the service that
    transcribed it), the seconds since `released_at`, how many words the
    Dictionary `fixed`, and - when AI polish changed it - the `raw`
    transcript. Arms undo only for text that really landed in `hwnd`, and
    only such text is learned from (the fix watcher reads that box).
    """
    global last_output, last_duration, last_output_hwnd, last_output_logged
    global last_output_app
    _wait_turn(seq)
    keys_free = _wait_for_modifiers_released()
    with deliver_lock:
        focused = keys_free and restore_target_window(hwnd)
        # The user may have grabbed a modifier in the moment focus was being
        # restored (starting the next dictation) - wait that out too, so the
        # paste never goes out as Win+V.
        if focused and _any_modifier_down():
            keys_free = _wait_for_modifiers_released()
            focused = keys_free
        if focused:
            time.sleep(0.05)  # give the focus change a moment to take effect
            print(f"[{'PASTE' if PASTE_MODE else 'TYPED'}] {output.strip()}")
            deliver(output, private=not _is_remote_client(app_exe))
        elif log_text is None:
            # A voice command's lone line break: nothing worth a clipboard
            # swap or a notice.
            print("[CMD]   Window unavailable - line break skipped.")
        else:
            why = ("Keys were still held down" if not keys_free
                   else f"{app_name or 'The window you dictated into'} wasn't available")
            if _copy_for_user(output):
                message = (f"{why}, so your text is on the clipboard — press "
                           f"Ctrl+V to paste it.")
            else:
                message = (f"{why}, and Scribe couldn't copy your text either — "
                           f"it's saved in your history (open the dashboard to "
                           f"copy it).")
            notify("paste_failed", "Couldn't paste your dictation", message,
                   cooldown=0)
        latency = time.monotonic() - released_at if released_at is not None else None
        logged = False
        if log_text:
            try:
                log_dictation(log_text, duration, app_name, app_exe,
                              engine=engine, latency=latency, raw=raw, fixed=fixed)
                logged = True
            except Exception as exc:
                _write_error_log("log_dictation", exc)
                notify("history_failed", "Couldn't update your history",
                       "Your text was handled, but Scribe couldn't update its "
                       "history file. Details are in the error log.")
        with undo_lock:
            if focused:
                last_output, last_duration = output, duration
                last_output_hwnd, last_output_logged = hwnd, logged
                last_output_app = (app_name, app_exe)
            else:
                last_output = ""      # nothing on screen for undo to remove
    # Learning happens OUTSIDE the delivery lock (it reads and writes the
    # Dictionary): the words of this dictation count toward names you say
    # often, and the box it was typed into is watched for your fixes.
    if focused and logged and LEARN_WORDS:
        _learn_from_dictation(log_text)
        if fix_watcher is not None:
            fix_watcher.watch(hwnd, output)


def process_audio(audio, hwnd, app_name=None, app_exe=None, seq=None,
                  session=None, released_at=None):
    """
    Transcribe the recorded audio and type the result. This runs on its OWN
    thread so the slow transcription does not freeze the hotkey listener.

    `hwnd`, `app_name`/`app_exe` and `seq` were captured when the recording
    finished (see _finish_recording) - NOT read from the globals here: after
    a multi-second transcription those may belong to a LATER dictation. `seq`
    is this dictation's place in line (None = no ordering, e.g. in tests).
    `session` is its ElevenLabs stream (None when it wasn't streamed), and
    `released_at` the time.monotonic() of the release, for the history's
    "how fast did it land".
    """
    # This job counts as in-flight until the finally below, so the status
    # shows "transcribing" - unless the next dictation is already recording.
    _job_started()
    _refresh_status()
    # Everything from here on is wrapped so that NO failure inside it (a
    # clipboard hiccup, a log-write error, a transcription exception that
    # escaped the fallback) can leave the overlay stuck on "transcribing" or
    # skip the listener-ready reset - the finally always settles the status
    # (the symptom this guards against was otherwise silent under pythonw:
    # the pill frozen mid-transcribe forever).
    try:
        # Ignore an accidental quick tap of the hotkey.
        if audio is None or len(audio) < MIN_RECORDING_SECONDS * SAMPLE_RATE:
            if session is not None:
                session.cancel()          # a tap: no text wanted from the stream
            print("[SKIP]  Recording too short - ignored.")
            return

        # How long the recording was, in seconds (samples / samples-per-second).
        duration = len(audio) / SAMPLE_RATE

        backend = ("ELEVENLABS" if session is not None
                   else "CLOUD" if cloud_available() else "LOCAL")
        print(f"[...]   Transcribing ({backend})...")
        # _use_speculative_or_transcribe() prefers the in-flight cloud
        # speculative result (kept warm by the pipelining worker) when it
        # covers nearly all of the audio - skipping the final Groq round-
        # trip on release. Falls back to transcribe_audio() in every other
        # case (pipeline off, no speculative yet, stale result, etc.) so
        # behavior is identical when pipelining is disabled.
        try:
            text, engine = _transcribe_take(audio, session)
        except TranscriptionFailed as exc:
            notify("transcribe_failed", "Couldn't transcribe that",
                   f"{exc} Nothing was typed.", cooldown=0)
            return

        # Whole-utterance commands are matched on the text WITHOUT fillers,
        # so "Um, scratch that." still counts as "scratch that".
        matchable = remove_fillers(text)

        # Inline-correction action ("scratch that" / "fix that"): act on the
        # PREVIOUS dictation rather than insert text - once that one has
        # been delivered (its turn comes first). Checked before the
        # text-replacement command path because the latter could otherwise
        # find no match and let "scratch that" leak through as typed text.
        action = whole_utterance_action(matchable)
        if action is not None:
            print(f"[CMD]   Voice action: {action}")
            _wait_turn(seq)
            handle_whole_utterance_action(action)
            return

        # If the whole dictation was nothing but a spoken command - you tapped
        # the hotkey and said only "new paragraph" - act on it directly and
        # skip the pipeline. This is the reliable path for voice commands.
        # (Not logged to history - so undoing it can't drop an older entry.)
        command_output = whole_utterance_command(matchable)
        if command_output is not None:
            print("[CMD]   Voice command (whole utterance).")
            _deliver_job(command_output, hwnd, app_name, app_exe, seq,
                         duration, log_text=None)
            return

        # AI polish first (a Groq model, ~0.2 s - see _polish_take): it tidies
        # what you said the way you meant to type it. The steps below still
        # run after it, so your Dictionary corrections always have the last
        # word. Anything wrong and `text` is simply left as it was.
        raw = text
        text, polished = _polish_take(text)

        # Then post-process through the pipeline. Each stage is a pure
        # text function, and the ORDER matters (see each function's docstring):
        #   remove_fillers       - drop "um", "uh", and other filler sounds
        #   clean_text           - tidy whitespace and capitalization
        #   apply_vocabulary     - fix mis-heard names and casing
        #   apply_voice_commands - turn an inline "new line" into a line break
        text = remove_fillers(text)
        text = clean_text(text)
        text, fixed = _apply_vocabulary_counted(text)
        text = apply_voice_commands(text)

        if not text:
            print("[SKIP]  No speech detected.")
            return

        # Optionally append a space so the next dictation doesn't collide.
        output = text + " " if ADD_TRAILING_SPACE else text
        # In its turn, into its window (or the clipboard, with a notice),
        # logged to history, and remembered for undo - see _deliver_job.
        _deliver_job(output, hwnd, app_name, app_exe, seq, duration,
                     log_text=text, engine=engine, released_at=released_at,
                     raw=raw if polished else None, fixed=fixed)
    except Exception as exc:
        # Last-resort guard: a failure in the dictation body must not leave
        # the app wedged. Log it (side-effect-free - does NOT touch the mic
        # stream, so an overlapping recording is undisturbed) and let finally
        # reset the UI.
        _write_error_log("process_audio", exc)
        notify("transcribe_failed", "Couldn't transcribe that",
               "Something went wrong - details are in the error log. "
               "Nothing was typed.", cooldown=30)
    finally:
        if session is not None:
            session.cancel()     # whatever happened, never leave a stream open
        _finish_turn(seq)        # delivered or skipped: let the next one go
        _job_finished()
        _refresh_status()
        print(f"[READY] Listening - hold {current_hotkey_name} to dictate.")


# How long a Ctrl+Win+Z undo waits for the user to finish the chord before
# releasing the modifiers virtually (Backspace with Ctrl held would delete
# whole words). Spoken commands never fake releases - see undo_last.
UNDO_MODIFIER_WAIT = 3.0


def undo_last(seq=None, from_chord=False):
    """
    Delete the most recent dictation: bring back the window it was typed
    into, send one Backspace per delivered character, then drop its history
    entry. Runs on its own thread so the hotkey listener stays responsive.

    `seq` is this undo's place in line (given by the input controller for
    Ctrl+Win+Z): it waits until every dictation spoken BEFORE it has landed,
    so it removes the one you just said - not the one before it. "Scratch
    that" is already in line (process_audio), so it passes None.

    `from_chord` is True for Ctrl+Win+Z: those keys are held right now, so we
    give them a few seconds and then release them virtually (Backspace with
    Ctrl held deletes whole words). A spoken "scratch that" instead waits for
    the user's keys like any delivery - faking releases then could cut short
    the next dictation the user is already holding the hotkey for.

    If the target window is gone, nothing is deleted - backspacing into
    whatever window has focus now could destroy unrelated text - and the
    user is told.
    """
    global last_output
    _wait_turn(seq)
    try:
        # Atomically take and clear the saved text, so a repeated or second
        # undo press finds nothing left and cannot delete a second time.
        with undo_lock:
            text, hwnd, logged = last_output, last_output_hwnd, last_output_logged
            last_output = ""
        if not text:
            print("[UNDO]  Nothing to undo.")
            return

        if from_chord:
            if not _wait_for_modifiers_released(UNDO_MODIFIER_WAIT):
                with _injecting():
                    kbd.release(keyboard.Key.ctrl)
                    kbd.release(keyboard.Key.cmd)
                time.sleep(0.05)
        else:
            _wait_for_modifiers_released()

        with deliver_lock:
            if not restore_target_window(hwnd):
                notify("undo_failed", "Couldn't undo",
                       "The window you dictated into isn't available, so "
                       "nothing was deleted.", cooldown=0)
                return
            time.sleep(0.05)
            # One Backspace per delivered character (trailing space included).
            with _injecting():
                for _ in range(len(text)):
                    kbd.press(keyboard.Key.backspace)
                    kbd.release(keyboard.Key.backspace)

        # A voice command's line break was never logged - dropping "the last
        # entry" then would delete an older, real dictation from history.
        if logged:
            remove_last_log_entry()
        print(f"[UNDO]  Deleted last dictation ({len(text)} characters).")
    finally:
        _finish_turn(seq)


# =============================================================================
#  HOTKEY LISTENER  -  piece (c).
# =============================================================================

def _write_error_log(where, exc):
    """
    Append an exception + traceback to the error log. Best-effort (logging
    must never itself raise) and SIDE-EFFECT-FREE: unlike log_listener_error,
    it does not touch the recording flag or the mic stream, so it is safe to
    call from the dictation worker or a Tk callback without disturbing an
    in-progress recording.
    """
    text = storage.log_error(where, exc)
    print(text, end="")          # visible when run as `python app.py`


def log_listener_error(where, exc):
    """
    Record a hotkey-listener exception and reset the recording state, so the
    listener thread stays alive and the next hotkey press starts cleanly.

    Why this exists: pynput's Listener stops its thread the moment a callback
    raises. Under pythonw there is no console, so the traceback vanishes and
    the only symptom is "the hotkey suddenly does nothing." Wrapping every
    callback in a try/except that ends here keeps the listener alive AND
    leaves a written trail of whatever went wrong.
    """
    global recording
    _write_error_log(where, exc)

    # The exception may have left us mid-recording: `recording` still True,
    # the mic stream still running. Reset both so the very next hotkey press
    # is treated as a fresh start. stop() on an already-stopped stream is
    # harmless; the try/except just hides whichever way sounddevice signals it.
    recording = False
    _stop_stream()
    session = _take_stream_session()   # ...and never leave a stream open
    if session is not None:
        session.cancel()
    _refresh_status()          # never leave the pill stuck on "recording"


def normalize(key):
    """Collapse left/right modifier-key variants into one generic key."""
    if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
        return keyboard.Key.ctrl
    if key in (keyboard.Key.cmd_l, keyboard.Key.cmd_r):
        return keyboard.Key.cmd
    if key in (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr):
        return keyboard.Key.alt
    if key in (keyboard.Key.shift_l, keyboard.Key.shift_r):
        return keyboard.Key.shift
    return key


# =============================================================================
#  INPUT CONTROLLER  -  all hotkey logic runs on ONE thread.
#
#  pynput calls on_press/on_release from inside Windows' low-level keyboard
#  hook. If that hook ever takes too long, Windows silently REMOVES it and
#  the hotkey is dead until Scribe restarts - and starting a recording
#  (opening the mic, sound cue, reading the focused window) is slow work.
#  So the hook callbacks only drop the key into `key_events` and return at
#  once; the input controller thread does everything else, in order. It is
#  the only code that changes `pressed`, `recording` and the hold latch, so
#  there are no races between a press and a release.
#
#  Scribe's own synthetic keystrokes (the Ctrl+V of a paste, undo's
#  Backspaces, Shift+Enter) arrive flagged `injected` while Scribe is sending
#  them (see _injecting) and are ignored - so a paste can never end or
#  restart the user's next dictation.
# =============================================================================

key_events = queue.Queue()

# Windows virtual-key codes for the modifiers, to ask which are PHYSICALLY
# held right now (GetAsyncKeyState). Win has a left and a right key.
_VK_BY_KEY = {
    keyboard.Key.ctrl:  (0x11,),
    keyboard.Key.alt:   (0x12,),
    keyboard.Key.shift: (0x10,),
    keyboard.Key.cmd:   (0x5B, 0x5C),
}
MODIFIER_KEYS = tuple(_VK_BY_KEY)

# Set when the current hold of the hotkey is "used up": a press that
# couldn't start (no mic), a cancelled recording, or one that hit the time
# limit. Windows auto-repeats held keys ~30 times a second; without this
# latch each repeat would start again. Cleared when a hotkey key is released.
_hold_consumed = False

# Sequence number for the next finished recording, so dictations are
# delivered in the order they were spoken (see _wait_turn).
_next_job_seq = 0

# Consecutive watchdog checks that found a hotkey key physically UP while we
# still think it's held - a missed key-up (see _handle_check).
_missed_release_checks = 0


def _key_is_down(key):
    """Is this modifier physically held right now? None when unknown (not a
    modifier, or not on Windows)."""
    vks = _VK_BY_KEY.get(key)
    if vks is None or _user32 is None:
        return None
    try:
        return any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks)
    except Exception:
        return None


def _any_modifier_down():
    """True while Ctrl, Alt, Shift or Win is physically held."""
    return any(_key_is_down(k) for k in MODIFIER_KEYS)


# While Scribe itself is sending keys (and for a short grace period after -
# Windows delivers them to the hook a moment later), injected key events are
# Scribe's own and are ignored. Injected keys from anything else - the
# on-screen keyboard, AutoHotkey remaps, remote-control tools - still count,
# so the hotkey keeps working through them.
INJECT_GRACE_SECONDS = 0.3
_injecting_until = 0.0
_injecting_depth = 0
_injecting_lock = threading.Lock()


@contextlib.contextmanager
def _injecting():
    """Wrap every block where Scribe sends keystrokes (see on_press)."""
    global _injecting_until, _injecting_depth
    with _injecting_lock:
        _injecting_depth += 1
        _injecting_until = float("inf")
    try:
        yield
    finally:
        with _injecting_lock:
            _injecting_depth -= 1
            if _injecting_depth == 0:
                _injecting_until = time.monotonic() + INJECT_GRACE_SECONDS


def _is_own_keystroke(injected):
    return injected and time.monotonic() < _injecting_until


def on_press(key, injected=False):
    """pynput hook callback - must return instantly (see the banner above)."""
    if not _is_own_keystroke(injected):
        key_events.put(("press", key))


def on_release(key, injected=False):
    """pynput hook callback - must return instantly (see the banner above)."""
    if not _is_own_keystroke(injected):
        key_events.put(("release", key))


def _process_key_event(event):
    """Handle one queued event. An exception is logged and the recording
    state reset - the controller loop must never die."""
    kind, key = event
    try:
        if kind == "press":
            _handle_press(key)
        elif kind == "release":
            _handle_release(key)
        elif kind == "check":
            _handle_check()
    except Exception as exc:
        log_listener_error(f"input controller ({kind})", exc)


def _input_controller():
    """The input controller thread: handle key events one at a time, forever."""
    while True:
        _process_key_event(key_events.get())


def _handle_press(key):
    """A real key went down."""
    global recording, recording_started_at, _hold_consumed, _missed_release_checks
    global _blocked_notice
    k = normalize(key)
    # Drop modifiers we THINK are held but physically aren't - their key-up
    # happened where our hook couldn't see it (Win+L, Ctrl+Alt+Del, a UAC
    # prompt). Without this, a later Ctrl hold could start a recording.
    for held in list(pressed):
        if held != k and _key_is_down(held) is False:
            pressed.discard(held)
            if held in HOTKEY:
                # That hold ended where we couldn't see it (e.g. Ctrl+Alt+Del
                # with a Ctrl+Alt hotkey) - so it isn't "used up" any more,
                # and its blocked-press notice is out of date.
                _hold_consumed = False
                _blocked_notice = None
    pressed.add(k)

    # Another key joined a blocked hotkey hold: it's a shortcut (Ctrl + Win
    # + arrow), not a dictation attempt - its notice would only annoy.
    if _blocked_notice is not None and k not in HOTKEY:
        _blocked_notice = None

    # Start recording the instant ALL hotkey keys are held.
    if not recording and not _hold_consumed and HOTKEY.issubset(pressed):
        blocker = _dictation_blocker()
        if blocker is not None:
            _hold_consumed = True             # once per press, not per repeat
            _blocked_notice = blocker         # explained on release
            return
        recording = True
        recording_started_at = time.monotonic()
        _missed_release_checks = 0
        if not start_recording():
            recording = False                 # no mic: nothing to stop
            recording_started_at = None
            _hold_consumed = True             # once per press, not per repeat
        return

    is_undo_key = getattr(key, "vk", None) == UNDO_KEY_VK
    # Undo (not recording): the undo modifiers are held AND Z was tapped.
    # The last_output check is here too, so a held-down Z (which auto-
    # repeats) stops spawning threads once there is nothing left to undo.
    if (not recording and last_output
            and UNDO_MODIFIERS.issubset(pressed) and is_undo_key):
        _start_chord_undo()
        return

    # Undo (during recording): when the record hotkey and the undo modifiers
    # are the SAME combo (e.g. the default Ctrl+Win), holding them starts
    # recording, so tapping Z while held means "scratch": cancel this
    # just-started recording and undo the previous dictation.
    if recording and is_undo_key and last_output and HOTKEY == UNDO_MODIFIERS:
        _cancel_recording()
        _start_chord_undo()
        return

    # Any OTHER key while recording means the user is using a shortcut that
    # starts with the hotkey (Ctrl+Win+Right switches desktops) - not
    # dictating. Cancel quietly; nothing gets transcribed or pasted.
    if recording and k not in HOTKEY:
        print("[CANCEL] Another key was pressed with the hotkey - cancelled.")
        _cancel_recording()


def _start_chord_undo():
    """Ctrl+Win+Z: undo on a worker thread, in line behind any dictation
    that's still being transcribed (so it removes THAT one, once it lands)."""
    global _next_job_seq
    seq = _next_job_seq
    _next_job_seq += 1
    threading.Thread(target=undo_last, args=(seq,),
                     kwargs={"from_chord": True}, daemon=True).start()


def _handle_release(key):
    """A real key came up."""
    global _hold_consumed, _blocked_notice
    k = normalize(key)
    if k in HOTKEY:
        # Letting go of the hotkey finishes the dictation...
        if recording:
            _finish_recording()
        # ...or explains why none could start (setup, a download...) - as
        # things stand NOW: the model may have got ready while it was held.
        if _blocked_notice is not None:
            current = _dictation_blocker()
            if current is not None:
                _show_blocked_notice(current)
            else:
                # Ready now - but nothing was recorded this time: say so,
                # rather than leave the user wondering where their words went.
                notify("model_ready", "Scribe is ready",
                       f"Nothing was recorded that time - hold {current_hotkey_name} "
                       f"and speak again.", cooldown=0)
            _blocked_notice = None
        # ...and ends the "used up" hold, so the next press starts fresh.
        _hold_consumed = False
    pressed.discard(k)


def _cancel_recording():
    """Stop recording and throw the audio away (a shortcut, not dictation)."""
    global recording, recording_started_at, _hold_consumed
    recording = False
    recording_started_at = None
    _hold_consumed = True                  # the combo is still held
    recording_stop_event.set()             # wind down any speculative worker
    with mic_lock:                         # never race a device refresh
        play_cue("stop")
    _stop_stream()
    session = _take_stream_session()       # nothing to transcribe: just close it
    if session is not None:
        session.cancel()
    frames.clear()
    _refresh_status()


def _finish_recording():
    """Stop recording and hand the audio to a worker to transcribe and
    deliver - with everything it needs captured NOW (the target window and
    app, and its place in line), not read later from globals that the next
    dictation will overwrite."""
    global recording, recording_started_at, _next_job_seq
    released_at = time.monotonic()         # the clock for "how fast did it land"
    recording = False
    recording_started_at = None
    audio = stop_recording()
    if SAVE_TAKES and audio is not None:
        held = released_at - _take_started if _take_started is not None else None
        threading.Thread(target=_save_take, args=(audio, held, _take_overflows),
                         daemon=True).start()
    # The mic has stopped, so the stream has every chunk: hand it over.
    session = _take_stream_session()
    seq = _next_job_seq
    _next_job_seq += 1
    try:
        threading.Thread(
            target=process_audio,
            args=(audio, target_hwnd, target_app_name, target_app_exe, seq),
            kwargs={"session": session, "released_at": released_at},
            daemon=True,
        ).start()
    except BaseException:
        if session is not None:
            session.cancel()          # no worker will ever finish it
        raise


def _handle_check():
    """
    The watchdog's periodic check, run on the controller thread:
      - a hotkey key physically UP on two checks in a row while we think it's
        held = a missed key-up (focus stolen mid-paste, a swallowed event):
        finish the recording normally, so the take is transcribed, not lost;
      - the time limit reached: finish and transcribe what was said, and
        tell the user to start a new dictation.
    """
    global _missed_release_checks, _hold_consumed, _blocked_notice
    if not recording:
        _missed_release_checks = 0
        # A used-up hold whose keys are all physically up is over, even if
        # their key-ups happened where the hook couldn't see them - and a
        # notice it was saving for the release is out of date.
        if _hold_consumed and all(_key_is_down(k) is False for k in HOTKEY):
            _hold_consumed = False
            _blocked_notice = None
        return
    if any(_key_is_down(k) is False for k in HOTKEY):
        _missed_release_checks += 1
        if _missed_release_checks >= 2:
            storage.log_error("hotkey",
                              message="a key-up was missed; finishing the recording")
            _missed_release_checks = 0
            _finish_recording()
            for k in HOTKEY:
                pressed.discard(k)          # they're physically up
            return
    else:
        _missed_release_checks = 0
    started = recording_started_at
    if started is not None and time.monotonic() - started >= MAX_RECORDING_SECONDS:
        _finish_recording()
        _hold_consumed = True               # the hotkey is still held
        notify("recording_limit", "Recording limit reached",
               f"Scribe transcribed the first {MAX_RECORDING_SECONDS // 60} "
               f"minutes. Release the hotkey and start a new dictation to "
               f"keep going.", cooldown=0)


def _recording_watchdog():
    """Background thread: every WATCHDOG_TICK_SECONDS, ask the input
    controller to check the recording (it owns the state - see _handle_check)."""
    while True:
        time.sleep(WATCHDOG_TICK_SECONDS)
        key_events.put(("check", None))


# How often the device watcher checks for plugged/unplugged mics and a new
# Windows default. Each check is two cheap system calls.
DEVICE_POLL_SECONDS = 2.0


def _device_watch_tick(last):
    """
    One device-watcher check. `last` is the previous (default mic id, device
    count); returns the new one. On a change the mic is marked dirty, and -
    only while idle, never mid-dictation - reopened on the new device.
    """
    global mic_dirty
    now = (devices.default_capture_id(), devices.input_device_count())
    default_changed = now[0] != last[0]
    if now != last:
        mic_dirty = True
    if mic_dirty and not recording:
        with mic_lock:
            if recording or not mic_dirty:
                return now
            previous = _mic_status
            status = reopen_mic()
            name = devices.default_input_name() if status == "default" else None
        _announce_mic(status, previous, default_changed, name)
    return now


def _device_watcher():
    """Background thread: follow the Windows default mic and plug/unplug
    events, so Scribe never needs a restart after a device change."""
    last = (devices.default_capture_id(), devices.input_device_count())
    while True:
        time.sleep(DEVICE_POLL_SECONDS)
        try:
            last = _device_watch_tick(last)
        except Exception as exc:
            storage.log_error("device watcher", exc)


# =============================================================================
#  STATUS OVERLAY  -  the always-visible pill at bottom-center of the screen.
#  It is a Tkinter window, so it is only ever touched on the main thread.
#
#  Design (Wispr Flow-style):
#    - Idle           -> a small dark COLLAPSED pill, always on screen, as a
#                        quiet "Scribe is here" indicator. No animation.
#    - Recording      -> the pill EXPANDS upward into the full-size shape
#                        and shows a live scrolling waveform inside (white),
#                        driven by the actual mic level.
#    - Transcribing   -> same expanded shape, a traveling wave through the
#                        same bars (grey) to signal continuous processing.
#  Transitions between the two sizes are eased so the pill feels alive,
#  not jumpy.
#
#  Tk has no per-pixel transparency, so we fake rounded corners with the
#  Windows `transparentcolor` trick: one specific color in the window becomes
#  fully see-through. We use PURE BLACK as the chromakey and render the pill
#  via PIL with supersampling + LANCZOS downsampling, so the rounded edges
#  are anti-aliased. Only EXACTLY-black pixels disappear; the AA fringe at
#  the pill's edge fades from pill-color toward (but not all the way to)
#  black, so it stays visible as a subtle dark glow instead of producing a
#  garish purple halo - the artefact you would otherwise get when AA edges
#  blend against a magenta key color.
# =============================================================================

# --- Overlay look and feel. Tweak here, not inline. ---
# --- Display scaling. Without this, Windows treats Scribe as a 96-DPI app
#     and bitmap-stretches the overlay at 125-200% scaling (blurry). Declaring
#     "system DPI aware" makes Windows hand us real pixels - so every size
#     below is multiplied by the scale factor to keep its physical size.
#     Must run before any window exists (main() creates them later).
def _enable_dpi_awareness():
    """Declare DPI awareness; return the display scale (1.0 = 100%)."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()     # older Windows
        except Exception:
            pass
    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


UI_SCALE = _enable_dpi_awareness()


def _px(value, scale=None):
    """A size in 100%-scaling pixels -> real pixels at this display's scale."""
    return max(1, int(round(value * (UI_SCALE if scale is None else scale))))


OVERLAY_W       = _px(56)          # ACTIVE pill width  (recording/transcribing)
OVERLAY_H       = _px(18)          # ACTIVE pill height (recording/transcribing)
OVERLAY_W_IDLE  = _px(30)          # collapsed pill width  (idle)
OVERLAY_H_IDLE  = _px(4)           # collapsed pill height (idle) - thin bar

# Supersample factor for the PIL render. The WHOLE overlay - the pill AND the
# waveform bars - is drawn this many times larger, then LANCZOS-downsampled,
# so every edge is anti-aliased. (The bars used to be jagged Tk-canvas
# rectangles with no AA - rendering them in PIL is what makes the waveform
# look crisp instead of pixelated.)
OVERLAY_SS = 5
# Waveform bar geometry, in logical px (pre-supersample).
OVERLAY_BAR_W   = _px(2)           # bar width
OVERLAY_BAR_GAP = _px(2)           # gap between bars
OVERLAY_BAR_PAD = _px(6)           # vertical padding: tallest bar = pill_height - this
OVERLAY_BAR_MIN_H = _px(2)         # shortest bar, so silence still shows a thin line
OVERLAY_BAR_MIN_PILL_H = _px(10)   # only draw bars once the pill has expanded past this
# The pill floats over every app, in any theme, so it follows none: a
# neutral near-black capsule with a hairline edge - like a Windows flyout.
OVERLAY_FILL    = "#1c1c1f"   # the visible pill color (never pure black: that's the key)
OVERLAY_EDGE    = "#3a3a40"   # its 1 px hairline, so it reads on dark backgrounds too
OVERLAY_CHROMAKEY = "#000000" # pure black - exactly-black pixels are made
                              # transparent; the pill's AA fringe fades
                              # toward black but never reaches it, so it
                              # shows as a soft dark glow around the pill.
OVERLAY_ALPHA   = 0.94        # very slight translucency over the pill itself
OVERLAY_BOTTOM_PAD = _px(24)       # distance from screen bottom (pill BOTTOM edge)
OVERLAY_TICK_MS = 50          # animation tick interval; ~20 fps is plenty smooth

# Eased size transition: each tick, current dimensions move this fraction
# of the way toward the target. 0.30 lands in ~6 frames (~300ms), which
# feels responsive without snapping.
OVERLAY_LERP = 0.30

# Waveform colors for the overlay's active states: plain WHITE bars follow
# your voice while recording; soft GREY bars pulse while it transcribes - the
# brightness alone tells the two apart, with no colour to clash with whatever
# is on screen. (The tray icon still goes red while recording, so the
# universal "rec" cue is never lost.)
WAVE_RECORDING    = "#f4f4f5"   # recording    - white (the live voice)
WAVE_TRANSCRIBING = "#8e8e96"   # transcribing - soft grey "working" pulse

# Quota-nudge tints for the IDLE pill - applied when today's Groq free-
# tier usage crosses 70% / 90% of either daily counter. Muted on purpose
# (a gentle presence, not a panic) but clearly warmer / redder than the
# neutral fill, or the nudge would be invisible on the thin idle bar.
# Active states (recording, transcribing) keep the neutral fill, so the
# dictation flow stays unambiguous.
OVERLAY_FILL_WARN   = "#44361a"   # 70-90% of a daily quota used (muted amber)
OVERLAY_FILL_DANGER = "#55252e"   # 90%+ of a daily quota used (muted rose)

# Groq whisper free-tier daily limits. Duplicated from dashboard.py
# (the two processes can't share state) and hard-coded because they
# change only when Groq announces a tier update. We only use the
# DAILY counters for the tray-tooltip / overlay warning - the minute
# and hour counters reset too quickly to be a useful "you're about
# to run out" nudge.
GROQ_DAILY_REQUESTS_LIMIT = 2000
GROQ_DAILY_AUDIO_SECONDS_LIMIT = 28800   # 8 hours of audio per day

# How often (ms) to refresh the quota state. 5s lag is invisible for a
# daily quota and avoids re-reading the usage log too aggressively.
QUOTA_TICK_MS = 5000

# Memo for _compute_quota_state so the 5s tick doesn't re-parse the whole
# usage log when nothing changed. _quota_cache_sig is (file size, mtime,
# day-ordinal); _quota_cache_val is the last computed (state, ratio).
_quota_cache_sig = None
_quota_cache_val = ("ok", 0.0)

# Current rendered pill dimensions. Updated by the tick loop as it eases
# toward the target size for the current state. Module-level so the draw
# helpers can read them without an argument plumbing.
_pill_w = float(OVERLAY_W_IDLE)
_pill_h = float(OVERLAY_H_IDLE)

# The live-waveform buffer. Each tick we push the latest smoothed mic
# level onto the right edge and drop the oldest off the left, so the
# bars appear to scroll right -> left while the user speaks. Used only
# by the recording-state draw helper; transcribing computes its bar
# heights from a traveling sine wave instead.
OVERLAY_BARS = 12
overlay_bar_levels = [0.0] * OVERLAY_BARS

# Tk garbage-collects PhotoImage objects the moment their last Python
# reference disappears - even if they are still attached to a canvas
# item. We keep the latest pill image here so the canvas's create_image
# entry has something to point at across ticks.
_pill_image_tk = None
# (rounded w, rounded h, fill_color) the current pill image was rendered for.
# Lets _draw_pill skip a re-render when nothing about the pill changed.
_pill_cache_key = None


def _state_target():
    """Target (w, h) for the pill in the current overlay state."""
    if overlay_state == "idle":
        return OVERLAY_W_IDLE, OVERLAY_H_IDLE
    return OVERLAY_W, OVERLAY_H


def build_overlay():
    """Create the always-on-top status pill at its collapsed idle size,
    and leave it visible. From here on the tick loop handles all visual
    state changes; the window itself is never withdrawn."""
    global overlay, overlay_canvas
    overlay = tk.Toplevel(root)
    overlay.overrideredirect(True)        # no title bar, no borders
    overlay.attributes("-topmost", True)  # always float above other windows
    overlay.attributes("-alpha", OVERLAY_ALPHA)
    # Chromakey: any pixel painted in OVERLAY_CHROMAKEY becomes fully
    # transparent on Windows. That hides the canvas area outside the
    # pill so the idle pill reads as a small floating shape, not a square.
    try:
        overlay.wm_attributes("-transparentcolor", OVERLAY_CHROMAKEY)
    except tk.TclError:
        pass
    overlay.configure(bg=OVERLAY_CHROMAKEY)
    # The canvas is sized to fit the LARGER active pill; the smaller idle
    # pill is drawn centered horizontally and bottom-aligned within it,
    # so transitions feel like the pill growing UP rather than re-centering.
    overlay_canvas = tk.Canvas(
        overlay,
        width=OVERLAY_W, height=OVERLAY_H,
        bg=OVERLAY_CHROMAKEY,
        highlightthickness=0, bd=0,
    )
    overlay_canvas.pack()
    position_overlay()
    _draw_pill(_pill_w, _pill_h)          # draw the initial idle pill
    overlay.deiconify()                   # visible from the start
    # Keep the pill anchored to the work area for the rest of the
    # process's life - the taskbar can move or auto-hide after startup,
    # and position_overlay() is otherwise never re-run.
    root.after(OVERLAY_REPOSITION_MS, _reposition_overlay_loop)


def _draw_pill(w, h, bars=None, bar_color=None):
    """Put the overlay on its canvas: the pill (rendered by _render_pill())
    in the right fill for the state and quota.

    Memoized while idle (no bars), so a settled pill isn't re-rendered every
    tick; always re-rendered while the waveform animates (bars change each
    frame, so there is nothing to cache)."""
    global _pill_image_tk, _pill_cache_key
    if h < 1 or w < 1:
        return

    if overlay_state == "idle" and quota_state == "danger":
        fill_color = OVERLAY_FILL_DANGER
    elif overlay_state == "idle" and quota_state == "warn":
        fill_color = OVERLAY_FILL_WARN
    else:
        fill_color = OVERLAY_FILL

    if bars is None:
        key = (int(round(w)), int(round(h)), fill_color)
        if key == _pill_cache_key and overlay_canvas.find_withtag("pill"):
            return
        _pill_cache_key = key
    else:
        _pill_cache_key = None        # animating - force the next idle frame to redraw

    overlay_canvas.delete("pill")
    _pill_image_tk = ImageTk.PhotoImage(_render_pill(w, h, fill_color, bars, bar_color))
    overlay_canvas.create_image(
        0, 0, image=_pill_image_tk, anchor="nw", tags="pill",
    )


def _render_pill(w, h, fill_color, bars=None, bar_color=None):
    """Render the whole overlay - the rounded pill (w x h, bottom-aligned so it
    appears to grow UP from a fixed baseline), plus the waveform bars when
    `bars` (a list of 0..1 levels) is supplied - as ONE supersampled PIL
    image, downsampled with LANCZOS for smooth anti-aliased edges. Returns
    the OVERLAY_W x OVERLAY_H image. Pure (no Tk), so tests can look at it.

    Drawing the bars here in PIL (rather than as Tk-canvas rectangles, which
    have NO anti-aliasing) is what keeps the waveform crisp instead of jagged.
    No glow, no colour: a calm grey capsule that sits well over any app."""
    SS = OVERLAY_SS
    iw, ih = OVERLAY_W * SS, OVERLAY_H * SS
    # Black background = chromakey, so anything outside the rounded shape
    # disappears entirely. LANCZOS averaging across the edges produces
    # near-black (but not exactly black) pixels, the soft visible AA fringe.
    img = Image.new("RGB", (iw, ih), OVERLAY_CHROMAKEY)
    d = ImageDraw.Draw(img)

    sw = max(1, int(round(w * SS)))
    sh = max(1, int(round(h * SS)))
    radius = sh / 2
    cx = iw / 2                                  # horizontal center
    bottom = ih                                  # pill bottom = image bottom
    top = bottom - sh
    pill_box = (cx - sw / 2, top, cx + sw / 2 - 1, bottom - 1)
    # The hairline is one real pixel wide (SS supersampled pixels), drawn as
    # the shape's own outline so it follows the rounded ends exactly. Only
    # on the expanded pill: on the 4 px idle bar it would leave almost no
    # room for the fill - and the quota tint lives in the fill.
    edge = OVERLAY_EDGE if h >= OVERLAY_BAR_MIN_PILL_H else None
    d.rounded_rectangle(pill_box, radius=radius, fill=fill_color,
                        outline=edge, width=SS if edge else 0)

    # Waveform bars - only once the pill has expanded enough to contain them.
    if bars and h >= OVERLAY_BAR_MIN_PILL_H:
        n = len(bars)
        bw = OVERLAY_BAR_W * SS
        gap = OVERLAY_BAR_GAP * SS
        block_w = n * bw + (n - 1) * gap
        bx = cx - block_w / 2
        bcy = bottom - sh / 2                     # vertical center of the pill
        max_h = max(OVERLAY_BAR_MIN_H * SS, (h - OVERLAY_BAR_PAD) * SS)
        min_h = OVERLAY_BAR_MIN_H * SS
        brad = bw / 2
        boxes = []
        for i, level in enumerate(bars):
            lvl = 0.0 if level < 0 else 1.0 if level > 1 else level
            bh = min_h + (max_h - min_h) * lvl
            x = bx + i * (bw + gap)
            boxes.append((x, bcy - bh / 2, x + bw, bcy + bh / 2))

        for box in boxes:
            d.rounded_rectangle(box, radius=brad, fill=bar_color)

    return img.resize((OVERLAY_W, OVERLAY_H), Image.LANCZOS)


def _work_area_bottom():
    """Return the y-coordinate of the bottom of the primary monitor's WORK
    AREA - the usable region that excludes the Windows taskbar. winfo_
    screenheight() reports the full screen including the taskbar, so the
    overlay would tuck under the taskbar if we anchored to it. Falls back
    to the plain screen height off-Windows or if the call fails."""
    try:
        SPI_GETWORKAREA = 0x0030
        # RECT layout: long left, top, right, bottom.
        rect = (ctypes.c_long * 4)()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, rect, 0,
        )
        if ok:
            return int(rect[3])     # rect.bottom
    except Exception:
        pass
    return overlay.winfo_screenheight()


def position_overlay():
    """Center the overlay horizontally and place its BOTTOM at a fixed
    distance ABOVE the Windows taskbar. We anchor by the bottom so the
    pill appears to grow upward when it expands."""
    global _overlay_last_xy
    screen_w = overlay.winfo_screenwidth()
    work_bottom = _work_area_bottom()
    x = (screen_w - OVERLAY_W) // 2
    # Window y so the canvas (and pill) BOTTOM sits OVERLAY_BOTTOM_PAD
    # pixels above the taskbar's top edge. The canvas height is OVERLAY_H.
    y = work_bottom - OVERLAY_H - OVERLAY_BOTTOM_PAD
    # The reposition loop calls this every second; skip the geometry set (and
    # the update_idletasks flush) when the target hasn't actually moved.
    if (x, y) == _overlay_last_xy:
        return
    _overlay_last_xy = (x, y)
    overlay.update_idletasks()
    overlay.geometry(f"+{x}+{y}")


# How often (ms) to re-run position_overlay(). 1 Hz is enough - the
# taskbar's geometry only changes on user actions (auto-hide animations,
# moving the taskbar, plugging a monitor) and a ~1s lag is invisible.
OVERLAY_REPOSITION_MS = 1000


def _compute_quota_state():
    """Return ('ok'|'warn'|'danger', worst_ratio) based on today's Groq
    free-tier usage. The WORST of the two daily counters wins, so e.g.
    hitting 92% of audio-seconds pushes 'danger' even when request
    count is fine. Returns ('ok', 0.0) when cloud is off, no key is
    set, or no calls have been made today - the warning never fires
    for a local-only user."""
    global _quota_cache_sig, _quota_cache_val
    if not (USE_CLOUD and GROQ_API_KEY):
        return ("ok", 0.0)
    if not os.path.exists(CLOUD_USAGE_LOG):
        return ("ok", 0.0)
    today = datetime.now().date()
    # Cheap change-signature: file size + mtime + the calendar day. If none
    # changed since the last parse, the answer is identical - return the
    # cached value instead of re-reading and re-JSON-parsing the entire
    # (unbounded, append-only) usage log. This runs every 5s for the life of
    # the process, so skipping the parse when nothing changed is what lets the
    # app actually sit idle.
    try:
        _st = os.stat(CLOUD_USAGE_LOG)
        sig = (_st.st_size, _st.st_mtime_ns, today.toordinal())
    except OSError:
        return ("ok", 0.0)
    if sig == _quota_cache_sig:
        return _quota_cache_val
    day_requests = 0
    day_audio = 0.0
    try:
        with open(CLOUD_USAGE_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("provider") == "elevenlabs":
                    continue              # streamed time isn't Groq quota
                ts_str = entry.get("timestamp", "")
                try:
                    if datetime.fromisoformat(ts_str).date() != today:
                        continue
                except ValueError:
                    continue
                day_requests += 1
                day_audio += float(entry.get("audio_seconds", 0.0) or 0.0)
    except OSError:
        return ("ok", 0.0)

    worst = max(
        day_requests / GROQ_DAILY_REQUESTS_LIMIT,
        day_audio    / GROQ_DAILY_AUDIO_SECONDS_LIMIT,
    )
    if worst >= 0.9:
        result = ("danger", worst)
    elif worst >= 0.7:
        result = ("warn", worst)
    else:
        result = ("ok", worst)
    # Cache against the signature so the next tick can skip the parse.
    _quota_cache_sig = sig
    _quota_cache_val = result
    return result


def _quota_tick():
    """Periodic refresh of the quota state on the main thread. When the
    state changes, re-tint the idle overlay and re-apply the tray
    tooltip so the user sees a 'getting close' nudge before Groq
    actually 429s. Always re-arms itself so a transient file-read
    error can't kill the loop."""
    global quota_state, quota_worst_ratio, _last_cloud_trim_date
    # Once per day (and on the first tick at startup), trim the cloud-usage log
    # so it stays bounded over a long-running session. The date check is a
    # no-op on every tick but the first after midnight.
    today = datetime.now().date()
    if _last_cloud_trim_date != today:
        _last_cloud_trim_date = today
        try:
            _trim_cloud_usage_log()
        except Exception:
            pass
    try:
        new_state, ratio = _compute_quota_state()
    except Exception:
        new_state, ratio = quota_state, quota_worst_ratio
    changed = (new_state != quota_state)
    quota_state = new_state
    quota_worst_ratio = ratio
    if changed:
        # The tick loop is parked while idle, so a quota change won't
        # naturally redraw - poke the pill ourselves. For active
        # states the animation is already running and will pick up
        # the (unchanged) idle color the next time it settles.
        if overlay is not None and overlay_state == "idle":
            try:
                _draw_pill(_pill_w, _pill_h)
            except Exception:
                pass
        # Refresh the tray tooltip so the percentage appears /
        # disappears in the hover text. (_refresh_status, not the last
        # overlay state: that could be stale and re-show "transcribing".)
        _refresh_status()
    root.after(QUOTA_TICK_MS, _quota_tick)


def _reposition_overlay_loop():
    """Re-anchor the overlay to the current work area on a slow tick.
    position_overlay() is otherwise called only at startup, so if the
    taskbar auto-hides, moves, or the primary monitor changes after
    that, the pill ends up at the wrong y - sometimes hidden behind the
    taskbar. Polling is cheap (one Win32 call + one geometry set per
    second) and handles every cause uniformly without subscribing to
    Windows messages."""
    if overlay is None:
        return
    try:
        position_overlay()
    except Exception:
        # Never let a transient Tk/Win32 hiccup kill the loop - the
        # overlay would then stay frozen at whatever the last good
        # position was, which is the bug we are trying to avoid.
        pass
    root.after(OVERLAY_REPOSITION_MS, _reposition_overlay_loop)


def _active_bar_levels():
    """Return (levels, color) for the current active state - a list of 0..1
    bar heights plus the waveform color - or (None, None) when idle. The
    actual drawing happens in _draw_pill(), which renders these bars into the
    supersampled PIL image for smooth, anti-aliased edges.

    Recording advances the rolling live-waveform buffer by one fresh mic
    sample, so the bars scroll right -> left as you speak. Transcribing
    computes a traveling sine wave - a steady 'still working' pulse."""
    if overlay_state == "recording":
        # The 12x multiplier maps typical speech RMS (~0.05..0.15) into a
        # comfortable mid-range; the 0.7 power curve compresses the dynamic
        # range so quiet speech still shows and loud bursts don't clip wildly.
        target = min(1.0, (current_audio_level * 12.0) ** 0.7)
        overlay_bar_levels.pop(0)
        overlay_bar_levels.append(target)
        return list(overlay_bar_levels), WAVE_RECORDING
    if overlay_state == "transcribing":
        # Per-bar phase lag creates the traveling wave - each bar trails the
        # one to its left. 0.25 sets the speed (~0.8 Hz at 20 fps); 0.45 the
        # wavelength.
        levels = [0.5 + 0.5 * math.sin(overlay_anim_phase * 0.25 - i * 0.45)
                  for i in range(OVERLAY_BARS)]
        return levels, WAVE_TRANSCRIBING
    return None, None


def _log_overlay_error(exc):
    """Append an overlay-tick exception to the error log instead of letting
    it vanish. Tk routes an exception raised inside an after() callback to
    its default handler, which writes to stderr - and under pythonw there
    is NO stderr, so the traceback is lost and the only visible symptom is
    'the status pill froze and never moved again.' Writing it here leaves a
    trail we can actually read. Best-effort: logging must never itself
    raise, or it would defeat the self-healing it exists to support."""
    _write_error_log("_overlay_tick", exc)


def _overlay_tick():
    """Animation heartbeat. Eases the pill toward the target size for the
    current state, redraws the pill, and (if active) draws the inner
    animation on top. The loop keeps running while we are still moving
    toward an idle target; once we settle there, it stops to save CPU.

    The whole body is wrapped in try/except for a reason that bit us once:
    if any single frame raised - a transient Tk / PIL / Win32 hiccup while
    drawing - the old code stopped here BEFORE re-arming the loop, AND left
    overlay_anim_job pointing at the already-fired after id. That id is not
    None, so update_overlay()'s "if overlay_anim_job is None: restart" check
    then failed forever: the pill stuck at whatever size it had (usually the
    collapsed idle bar) until Scribe was restarted, with the traceback lost
    to pythonw's missing stderr. Now a bad frame is logged and we fall
    through to re-arm, so the next tick simply tries again and the pill
    heals itself."""
    global overlay_anim_phase, overlay_anim_job, _pill_w, _pill_h

    try:
        target_w, target_h = _state_target()

        # Snap if we are within a sub-pixel of the target (avoids endless
        # micro-adjustments that never quite reach the integer).
        if abs(target_w - _pill_w) < 0.5 and abs(target_h - _pill_h) < 0.5:
            _pill_w = float(target_w)
            _pill_h = float(target_h)
        else:
            _pill_w += (target_w - _pill_w) * OVERLAY_LERP
            _pill_h += (target_h - _pill_h) * OVERLAY_LERP

        # Render the pill AND the waveform bars together in one anti-aliased
        # PIL image (the bars used to be jagged Tk-canvas rectangles).
        bars, bar_color = _active_bar_levels()
        _draw_pill(_pill_w, _pill_h, bars=bars, bar_color=bar_color)

        overlay_anim_phase += 1

        # Stop ticking only when we are idle AND have fully settled at the
        # idle size. While transitioning to idle, the tick keeps going so we
        # can animate the collapse smoothly.
        settled = (_pill_w == target_w and _pill_h == target_h)
        if overlay_state == "idle" and settled:
            overlay_anim_job = None
            return
    except Exception as exc:
        # One bad frame must never kill the loop (see docstring). Log it,
        # then fall through to re-arm so the animation recovers next tick.
        _log_overlay_error(exc)

    # Re-arm. Reached on every active frame AND after a caught exception,
    # so the ONLY way the loop stops is the clean idle-park above (which
    # sets overlay_anim_job = None). That keeps update_overlay()'s
    # "is None" restart check an honest signal of "loop is parked."
    try:
        overlay_anim_job = root.after(OVERLAY_TICK_MS, _overlay_tick)
    except Exception:
        # root.after only fails while Tk is tearing down at shutdown -
        # there is nothing left to animate, so let the loop end quietly.
        overlay_anim_job = None


def update_overlay(state):
    """Switch overlay state. The pill is always visible; this just changes
    what the tick loop is animating toward (and starts the loop if it
    is currently parked)."""
    global overlay_state, overlay_anim_phase
    if overlay is None:
        return
    overlay_state = state
    # Reset the inner animation phase when ENTERING an active state, so
    # the bars start near zero instead of mid-swing.
    if state in ("recording", "transcribing"):
        overlay_anim_phase = 0
    # Clear the live-waveform buffer at the start of each recording so
    # the new dictation doesn't briefly show leftover bars from the
    # previous one before audio_callback feeds in fresh levels.
    if state == "recording":
        overlay_bar_levels[:] = [0.0] * OVERLAY_BARS
    # Kick off the tick loop if it stopped. It will run until the pill
    # settles at the new state's target size (or, for idle, until the
    # collapse animation finishes).
    if overlay_anim_job is None:
        _overlay_tick()


# =============================================================================
#  DASHBOARD  -  spawned in its own pywebview window by dashboard.py.
#
#  The dashboard's UI is HTML/CSS/JS (dashboard.html), wired to a tiny
#  Python bridge (dashboard.py) that exposes settings and log data to the
#  page and writes settings back to config.json. We keep the dashboard
#  out of this process for two reasons:
#    1. Tkinter (used here for the floating overlay) and pywebview both
#       want to own the main thread; running each in its own process keeps
#       both event loops happy.
#    2. Reloading the page or restarting the dashboard never threatens the
#       background dictation flow - this process keeps recording, hot-keys
#       and the tray alive regardless of what the dashboard is doing.
# =============================================================================

DASHBOARD_SCRIPT = os.path.join(APP_DIR, "dashboard.py")


def _dashboard_python():
    """Pick the Python interpreter to run dashboard.py with. When Scribe is
    launched from a venv, sys.executable points at venv's python.exe (or
    pythonw.exe under pythonw) - exactly what we want. We prefer pythonw
    when present so the dashboard never spawns a console of its own."""
    exe = sys.executable
    pythonw = exe.replace("python.exe", "pythonw.exe")
    if pythonw != exe and os.path.exists(pythonw):
        return pythonw
    return exe


def _focus_dashboard_window():
    """Try to bring an already-open dashboard window to the foreground.
    Looks the window up by its title ('Scribe') via FindWindowW, then
    issues ShowWindow(SW_RESTORE) to undo any minimize and
    SetForegroundWindow to raise it. No-op on non-Windows or if the
    window can't be found - the dashboard is mid-boot, say."""
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "Scribe")
        if not hwnd:
            return
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def open_dashboard():
    """Open the dashboard - or, if it's already open, raise it. Runs on
    the main thread via poll_ui_queue(); we just spawn the subprocess
    (or focus the existing one) and let it own its own event loop. Until
    the first-run setup is done, it opens as the welcome."""
    global dashboard_proc, dashboard_welcome

    # poll() returns None while the child is alive; non-None means it has
    # exited and we should spawn a fresh one.
    if dashboard_proc is not None and dashboard_proc.poll() is None:
        # Already running - bring its window forward instead of doing
        # nothing. That way clicking the pinned taskbar icon when the
        # dashboard is already open behaves like every other app.
        _focus_dashboard_window()
        return

    # CREATE_NO_WINDOW (Windows) hides the dashboard's console even when
    # we are running python.exe. Harmless on other platforms - the flag
    # is just an integer that subprocess ignores there.
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # Until setup is done, the dashboard opens as the welcome.
    args = [_dashboard_python(), DASHBOARD_SCRIPT]
    if setup_pending:
        args.append("--welcome")

    try:
        # The dashboard's output goes to a log (fresh each launch), so if it
        # dies on startup we can tell the user why instead of doing nothing.
        # If the log can't be created, open the dashboard anyway.
        try:
            storage.ensure_data_dir()
            log = open(storage.DASHBOARD_LOG, "w", encoding="utf-8")
        except OSError:
            log = None
        try:
            dashboard_proc = subprocess.Popen(
                args,
                cwd=APP_DIR,
                creationflags=flags,
                stdout=log if log else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
            )
        finally:
            if log:
                log.close()           # the dashboard has its own handle
        dashboard_welcome = setup_pending
        root.after(DASHBOARD_CHECK_MS, _check_dashboard_started)
    except Exception as exc:
        _write_error_log("open_dashboard", exc)
        notify("dashboard_failed", "Dashboard didn't open", str(exc), cooldown=0)
        dashboard_proc = None


# How long after launching the dashboard we check it didn't die on startup.
DASHBOARD_CHECK_MS = 4000


def _last_line(path):
    """The last non-empty line of a small text file, or ''."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


def _check_dashboard_started():
    """
    Runs on the main thread a few seconds after launch (and, for the
    first-run welcome, every few seconds until it exits). If the dashboard
    exited with an error, say so - with its own last words. (Exit
    code 2 means it showed its own "WebView2 is missing" dialog.) If it was
    the first-run welcome, Scribe must not be left unset: set it up with the
    defaults instead.
    """
    proc = dashboard_proc
    if proc is None:
        return
    code = proc.poll()
    if code is None:
        # Still running. While it's the first-run welcome, keep watching: a
        # "WebView2 is missing" dialog stays up for as long as the user reads
        # it, and only then does the dashboard exit (code 2).
        if dashboard_welcome and setup_pending:
            root.after(DASHBOARD_CHECK_MS, _check_dashboard_started)
        return
    if code == 0:
        return                             # closed normally (setup may still be pending)
    if code != 2:
        reason = _last_line(storage.DASHBOARD_LOG) or f"It exited with code {code}."
        notify("dashboard_failed", "Dashboard didn't open", reason, cooldown=0)
    if dashboard_welcome and setup_pending:
        finish_setup_with_defaults()       # no welcome - never leave Scribe unset


def reload_config():
    """Re-read config.json AND vocabulary.json into the module-level settings -
    called when the dashboard saves and pings the control channel. Most live-
    applied settings (hotkey, mic preference, cloud flag) and ALL vocabulary
    edits (terms/corrections added from the dashboard's Dictionary page) take
    effect on the very next dictation. A new microphone is picked up at the
    next idle moment; a new model loads in the background and takes over
    when it's ready."""
    global _TRANSCRIBE_PROMPT_CACHE, mic_dirty, eleven_paused_until, polish_paused_until
    old_mic = MIC_DEVICE
    old_eleven = (CLOUD_PROVIDER, ELEVENLABS_API_KEY)
    old_polish = (GROQ_API_KEY, POLISH)
    # Both re-read their file (mutating module globals in place) and return
    # notices if a file had to be recovered - shown to the user below.
    notices = load_config() + load_vocabulary()
    _TRANSCRIBE_PROMPT_CACHE = None # force a rebuild with the new terms
    if not setup_pending:
        _apply_model_settings()     # a new model size / cloud-only choice applies live
    if MIC_DEVICE != old_mic:
        mic_dirty = True            # the device watcher reopens it when idle
    if (CLOUD_PROVIDER, ELEVENLABS_API_KEY) != old_eleven:
        # A new ElevenLabs key or service is what the user did about the
        # last failure - try it right away, not after the pause runs out.
        eleven_paused_until = 0.0
    if (GROQ_API_KEY, POLISH) != old_polish:
        polish_paused_until = 0.0   # ...and the same for AI polish
    if fix_watcher is not None:
        # The switch applies at once - unless the watcher couldn't start at
        # all (no UI Automation): then there's nothing to switch on.
        fix_watcher.enabled = (LEARN_WORDS and fix_watcher.thread is not None
                               and fix_watcher.thread.is_alive())
    _show_notices(notices)
    _warm_polish()                  # a new Groq key / polish switch: get it ready
    print("[CTL] config + vocabulary reloaded after dashboard save.")


def complete_setup():
    """
    Main thread, on "setup-done": the welcome saved the user's choices.
    Apply them and start the model. Ignored unless a settings file now
    exists (a stray message must not end the first run with nothing chosen).
    """
    global setup_pending
    if not setup_pending:
        reload_config()
        return
    if not os.path.exists(CONFIG_FILE):
        return
    setup_pending = False
    reload_config()          # settings + vocabulary + the chosen model
    _refresh_status()        # the tooltip stops saying "finish setup"


def finish_setup_with_defaults():
    """
    Main thread: a first run whose welcome couldn't open (no WebView2, or it
    crashed on start). Set Scribe up with the defaults - on this PC,
    small.en, the default mic, Ctrl + Win; start-at-sign-in stays off, since
    nobody agreed to it - and say what's happening.
    """
    global setup_pending, _model_wait_told, CHECK_UPDATES
    # "On this PC" (these defaults) makes no daily request to GitHub unless
    # turned on in Settings -> About - the same as choosing it in the welcome.
    CHECK_UPDATES = False
    try:
        save_config()
    except storage.StorageError as exc:
        # Run with the defaults anyway; the welcome is offered again next time.
        _write_error_log("setup defaults", exc)
    setup_pending = False
    _model_wait_told = True
    notify("setup_defaults", "Setting up Scribe",
           "Downloading the speech model (about 480 MB). You'll get a notice "
           "when it's ready.", cooldown=0)
    _apply_model_settings()
    _refresh_status()


def _should_open_dashboard_at_start():
    """First run (the welcome) or a launch that asked to be seen (--show).
    A sign-in start stays quietly in the tray."""
    return setup_pending or SHOW_REQUESTED


def shutdown():
    """Cleanly stop every part of the app. Runs on the main thread."""
    print("\nShutting down. Goodbye.")
    if listener is not None:
        listener.stop()
    with mic_lock:
        _close_stream()
    # Close the dashboard window if the user has one open. terminate() is
    # the cross-platform "ask nicely" signal; the pywebview side handles
    # WM_CLOSE for us and exits cleanly.
    if dashboard_proc is not None and dashboard_proc.poll() is None:
        try:
            dashboard_proc.terminate()
        except Exception:
            pass
    # (The control pipe's thread is a daemon - it ends with the process.)
    if tray_icon is not None:
        tray_icon.stop()
    root.destroy()   # ends root.mainloop(), so the program exits


def poll_ui_queue():
    """
    Run on the main thread ~10x per second. Reads commands that background
    threads (the tray icon, the control pipe) dropped into ui_queue,
    and acts on them here - where it is SAFE to touch Tkinter widgets and
    module globals at the same time.
    """
    try:
        while True:
            command = ui_queue.get_nowait()  # raises Empty when drained
            # Each command runs in its OWN try/except: one handler raising
            # (e.g. reload_config on a wrong-shape config) must not escape and
            # kill the pump. If it did, the overlay would freeze and the tray's
            # Open/Quit would stop working - silently under pythonw.
            try:
                if command == "open":
                    open_dashboard()
                elif command == "quit":
                    shutdown()
                    return  # do not reschedule - we are shutting down
                elif command == "reload-config":
                    # The dashboard saved settings and pinged us. Pull the
                    # fresh values into our module globals on the main thread.
                    reload_config()
                elif command == "setup-done":
                    # The welcome saved the user's choices (Finish setup).
                    complete_setup()
                elif isinstance(command, tuple) and command[0] == "status":
                    # A status change queued by any thread (update_status):
                    # tray icon, tooltip and overlay, applied here.
                    _apply_status(command[1])
            except Exception as exc:
                _write_error_log("poll_ui_queue", exc)
    except queue.Empty:
        pass
    # Schedule ourselves to run again in 100 milliseconds.
    root.after(100, poll_ui_queue)


# =============================================================================
#  LEARNING  -  the Dictionary fills itself (learning.py, fix_watch.py).
#
#  Two ways, both switched by LEARN_WORDS:
#    - the fix watcher (its own thread, started in main()) reads back the text
#      box each dictation was typed into; a word you correct there becomes a
#      correction - _learn_word(right, wrong, "fix"), with one notice;
#    - _learn_from_dictation() counts the words of each dictation; a name
#      said often enough (learning.auto_terms) is added quietly.
#  Both change vocabulary.json through storage and reload it here, so the
#  word is used from the very next dictation. Removed words never return.
# =============================================================================

fix_watcher = None                 # a fix_watch.FixWatcher, from main()
vocab_lock = threading.Lock()      # one learn at a time: read, change, save
_learning_index = None             # learning.SuggestionIndex over the history
_learning_index_lock = threading.Lock()
_fix_watch_logged = {}             # where -> time.monotonic() of its last log line


def _learn_word(right, wrong=None, source="fix"):
    """
    Add a learned word to the Dictionary: `right` as a term and, with
    `wrong`, the correction wrong -> right (learning.learn - never a word you
    removed). Saved, then reloaded here, so the next dictation uses it. A
    fix is announced once; a name said often is added quietly. Returns True
    if the Dictionary changed. Safe from any thread.
    """
    global _TRANSCRIBE_PROMPT_CACHE
    with vocab_lock:
        try:
            # Read the FILE, not memory: the dashboard may have just changed it.
            vocab, _notices = storage.load_vocab()
            if not learning.learn(vocab, right, wrong, source):
                return False
            storage.save_vocab(vocab)
        except storage.StorageError as exc:
            storage.log_error("learn a word", exc)
            return False
        load_vocabulary()
        _TRANSCRIBE_PROMPT_CACHE = None     # the Whisper prompt lists the terms
    print(f"[LEARN] {right}" + (f" (was heard as \"{wrong}\")" if wrong else " (said often)"))
    if source == "fix":
        notify(f"learned:{right.lower()}", f"Learned “{right}”",
               "Scribe will spell it that way from now on. You can remove it on "
               "the Dictionary page.", cooldown=0)
    return True


def _build_learning_index(skip_text=None):
    """The word counts over the whole history. `skip_text`: the dictation
    being learned from right now, if it is already the newest line - it is
    added by the caller, and must not count twice."""
    entries = storage.read_jsonl(LOG_FILE)
    if skip_text is not None and entries and entries[-1].get("text") == skip_text:
        entries = entries[:-1]
    index = learning.SuggestionIndex()
    index.add([e for e in entries if isinstance(e.get("text"), str)])
    return index


def _learn_from_dictation(text):
    """Count this dictation's words; add any name that now qualifies
    (learning.auto_terms - said at least 5 times, a name, not a mishearing).
    Never raises: learning must not cost you a dictation."""
    global _learning_index
    if not LEARN_WORDS or not isinstance(text, str) or not text.strip():
        return
    try:
        with _learning_index_lock:
            if _learning_index is None:
                _learning_index = _build_learning_index(skip_text=text)
            _learning_index.add([{"text": text}])
            new_terms = learning.auto_terms(_learning_index, VOCAB_DATA)
        for term in new_terms:
            _learn_word(term, source="said")
    except Exception as exc:
        storage.log_error("learn from a dictation", exc)


def _make_uia_reader():
    """The fix watcher's Reader - made on its own thread (see fix_watch.py)."""
    import uia_text
    return uia_text.Reader()


def _fix_watch_error(where, exc):
    """The fix watcher's failures go to the error log - at most one line per
    kind every 10 minutes (an app that can't be read would repeat it)."""
    now = time.monotonic()
    if where != "start" and now - _fix_watch_logged.get(where, -1e9) < 600:
        return
    _fix_watch_logged[where] = now
    storage.log_error(f"learn from your fixes ({where})", exc)


# =============================================================================
#  UPDATES  -  once a day, is there a newer Scribe? (updates.py)
#
#  A small request to GitHub's public API - nothing about you or your
#  dictations is sent. When a newer version is out you get ONE notice for
#  it (the version is remembered in config.json), and Settings -> About has
#  the "Update now" button. Switch the check off there too.
# =============================================================================

UPDATE_CHECK_DELAY = 90            # seconds after start: never slow a start down
UPDATE_CHECK_EVERY = 24 * 3600     # then once a day


def _check_for_update():
    """One check: a notice the first time a newer version is seen. Returns
    updates.check()'s result, or None when checking is switched off - or
    while the welcome is still open: nothing may be saved before "Finish
    setup" (remembering the notice would write config.json, and a first run
    closed early would then be taken for a finished one)."""
    global UPDATE_NOTIFIED
    if not CHECK_UPDATES or setup_pending:
        return None
    result = updates.check()
    latest = result.get("latest")
    if result.get("status") == "available" and latest and latest != UPDATE_NOTIFIED:
        notify("update", f"Scribe {latest} is available",
               "Open Scribe's Settings → About and click Update now. "
               "It takes about a minute.", cooldown=0)
        UPDATE_NOTIFIED = latest
        try:
            storage.save_config_changes({"update_notified": latest})
        except storage.StorageError as exc:     # at worst, one more notice later
            storage.log_error("remember update notice", exc)
    return result


def _update_watcher():
    """The daily check, on its own thread. A failed check just waits for
    the next day - never a notice about the check itself."""
    time.sleep(UPDATE_CHECK_DELAY)
    while True:
        try:
            _check_for_update()
        except Exception as exc:
            storage.log_error("update check", exc)
        time.sleep(UPDATE_CHECK_EVERY)


# =============================================================================
#  TRAY MENU CALLBACKS  -  these run on the TRAY thread, so they only drop
#  a command in the queue and let the main thread do the real work.
# =============================================================================

def on_open_dashboard(icon, item):
    ui_queue.put("open")


def on_quit(icon, item):
    ui_queue.put("quit")


def on_open_data_folder(icon, item):
    """Tray menu: open the data folder, %APPDATA%/Scribe (settings, history,
    error log)."""
    try:
        storage.ensure_data_dir()
        os.startfile(storage.DATA_DIR)
    except Exception as exc:
        storage.log_error("open data folder", exc)


# =============================================================================
#  MAIN
# =============================================================================

def main():
    global tray_icon, listener, root, startup_complete

    # Give this process its own Windows taskbar identity. Without this,
    # Windows assumes our windows belong to pythonw.exe and shows ITS
    # (Python) icon on the taskbar. Must run before any window is created.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Scribe.VoiceDictation"
        )
    except Exception:
        pass  # not on Windows / call unavailable - harmless to skip

    # 1. Start the input controller (it owns all hotkey logic), then the
    #    global keyboard listener, whose hook callbacks only queue events.
    threading.Thread(target=_input_controller, daemon=True).start()
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    # 2. Build the tray icon and run it on its OWN background thread
    #    (the main thread is reserved for Tkinter, below).
    menu = pystray.Menu(
        # default=True -> double-clicking the tray icon runs this item.
        pystray.MenuItem("Open Dashboard", on_open_dashboard, default=True),
        pystray.MenuItem("Open data folder", on_open_data_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    tray_icon = pystray.Icon(
        "scribe", icon=ICON_IDLE, title="Scribe - ready", menu=menu
    )
    def _on_tray_ready(icon):
        # pystray calls this (on its own thread) once the icon exists; it
        # must make the icon visible itself. Then show startup's notices.
        icon.visible = True
        _flush_pending_notices()

    threading.Thread(target=tray_icon.run, kwargs={"setup": _on_tray_ready},
                     daemon=True).start()

    # 3. Answer the control pipe - the dashboard's "reload-config" when you
    #    save settings, a second launch's "show", the welcome's "status".
    instance.serve(_handle_control)

    # 3b. Start the stuck-recording watchdog so a missed hotkey key-up can
    #     never leave the mic stuck open and the hotkey permanently dead.
    threading.Thread(target=_recording_watchdog, daemon=True).start()

    # 3c. Start the device watcher: follows the Windows default mic and
    #     plug/unplug events so a device change never needs a restart.
    threading.Thread(target=_device_watcher, daemon=True).start()

    # 3d. Once a day, see whether a newer Scribe is out.
    threading.Thread(target=_update_watcher, name="update-check", daemon=True).start()

    # 3e. Learn from your fixes: a thread that reads back the box each
    #     dictation was typed into (UI Automation - see fix_watch.py).
    global fix_watcher
    fix_watcher = fix_watch.FixWatcher(
        _make_uia_reader, on_fix=lambda wrong, right: _learn_word(right, wrong, "fix"),
        on_error=_fix_watch_error)
    fix_watcher.enabled = LEARN_WORDS
    fix_watcher.start()

    print()
    print(f"[READY] Listening - hold {current_hotkey_name} to dictate.")
    print("        (Double-click the tray icon to open the dashboard.)")
    if HOTKEY == UNDO_MODIFIERS:
        # Record hotkey and undo modifiers are the same combo, so undo runs
        # via the tap-Z-while-holding gesture (see on_press).
        print(f"        (Undo: hold {current_hotkey_name} and tap Z, "
              f"or say \"scratch that\".)")
    else:
        print("        (Undo: Ctrl+Win+Z, or say \"scratch that\".)")

    # Open the dashboard only when it's wanted: the welcome on a first run,
    # or when the user clicked Scribe (--show). A sign-in start stays in the
    # tray. Queued so it runs on the main thread once mainloop is spinning.
    if _should_open_dashboard_at_start():
        ui_queue.put("open")

    # 4. Build the hidden root window and give it the main thread. It
    #    stays invisible; the only Tk surface we ever show is the small
    #    floating status overlay. The dashboard itself runs in its own
    #    pywebview process (see open_dashboard()), so there is no need
    #    for CustomTkinter here - plain tkinter is enough.
    root = tk.Tk()

    # Errors inside Tk callbacks (the overlay's animation, ui_queue handlers)
    # go to the log instead of a console nobody can see.
    def _tk_error(exc_type, exc, tb):
        storage.log_error("tk callback", exc.with_traceback(tb))
        notify("background_error", "Something went wrong",
               "Scribe kept running. Details are in the error log "
               "(tray → Open data folder).")
    root.report_callback_exception = _tk_error
    root.withdraw()
    # Make the Scribe icon the default for every window this app opens -
    # title bars AND taskbar buttons. default=True covers all of them.
    # app_icon_photo is kept referenced so Python does not discard the image.
    global app_icon_photo
    app_icon_photo = ImageTk.PhotoImage(ICON_IDLE)
    root.iconphoto(True, app_icon_photo)
    build_overlay()                 # create the (hidden) status overlay
    root.after(100, poll_ui_queue)  # start the queue-checking loop
    # Seed the quota state and start its refresh loop. Running it once
    # here (before the first tick fires QUOTA_TICK_MS later) means the
    # pill / tooltip reflect carried-over usage from earlier in the day
    # immediately, instead of showing "ok" for the first 5 seconds.
    root.after(0, _quota_tick)
    _warm_polish()                  # the first polish shouldn't pay for connecting
    # From here on an uncaught error means "stopped", not "couldn't start".
    startup_complete = True
    root.mainloop()                 # blocks here until shutdown()


# =============================================================================
#  START THE MODEL  -  last, so every function the loader's callbacks use
#  (notify, _refresh_status...) already exists.
# =============================================================================

if setup_pending:
    models.set_waiting()        # nothing to load until the welcome is done
else:
    _apply_model_settings()


if __name__ == "__main__":
    main()
