"""
=============================================================================
 SCRIBE INSTANCE - one Scribe per user, and how its parts reach it.
=============================================================================

 Two jobs:

   1. SINGLE INSTANCE. A named mutex, "Local\\Scribe-<id>": the first copy
      of Scribe creates it; a second copy (a Start-menu click while Scribe is
      already running) finds it taken and exits before touching any file.

   2. CONTROL CHANNEL. A named pipe, "\\\\.\\pipe\\Scribe-<id>": the
      dashboard and a second launch send the running Scribe small JSON
      messages - {"cmd": "reload-config"}, {"cmd": "show"}, {"cmd":
      "status"}... Every message gets a JSON reply (status's is the
      interesting one), so a sender knows it was heard.

 <id> is a short hash of the data folder (%APPDATA%\\Scribe normally). So:
   - each Windows user has their own Scribe (their own APPDATA) - two people
     signed in to one PC can't poke each other's copy;
   - a test run, which uses a temp data folder, gets different names and
     can never reach the Scribe you're actually using.

 Messages are plain JSON bytes (send_bytes/recv_bytes) - never pickle, so a
 message can't carry code. Named pipes are Windows' standard local IPC; the
 default security lets only this user (and administrators) write to it.
=============================================================================
"""

import ctypes
import hashlib
import json
import os
import threading
import time
from ctypes import wintypes
from multiprocessing.connection import Client, Listener

import storage


def _instance_id(data_dir):
    """12 hex characters naming this data folder (case-insensitive, like
    Windows paths)."""
    norm = os.path.normcase(os.path.abspath(data_dir))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


INSTANCE_ID = _instance_id(storage.DATA_DIR)
MUTEX_NAME = "Local\\Scribe-" + INSTANCE_ID
PIPE_NAME = "\\\\.\\pipe\\Scribe-" + INSTANCE_ID

# Our messages and replies are tiny; anything bigger isn't ours.
MAX_MESSAGE_BYTES = 64 * 1024
# How long the server waits for a connected client to actually say something.
CLIENT_SEND_WAIT = 1.0
RETRY_SECONDS = 2.0        # between attempts to open the pipe
ACCEPT_BACKOFF = 1.0       # after an accept() error
LOG_EVERY = 60.0           # log a repeating pipe error at most this often

_ERROR_ALREADY_EXISTS = 183
_held_mutexes = []      # handles kept open (never closed) until Scribe exits


def acquire(name=MUTEX_NAME):
    """
    Claim the one-Scribe slot. True: this is the only copy - keep running.
    False: another copy already holds it - exit. If the mutex can't be
    created at all (never seen in practice) Scribe runs rather than refusing
    to start, and logs it.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return True                       # not Windows: nothing to guard with
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not handle:
        storage.log_error("single instance",
                          message=f"CreateMutex failed (error {err}); starting anyway")
        return True
    if err == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)      # we only opened someone else's
        return False
    _held_mutexes.append(handle)          # released by Windows when we exit
    return True


_SYNCHRONIZE = 0x00100000
_ERROR_FILE_NOT_FOUND = 2


def is_running(name=MUTEX_NAME):
    """
    Is a Scribe running right now (for this data folder)? Only OPENS the
    mutex, never creates it - so asking can't get in the way of a Scribe
    that is starting at that very moment (setup.bat and the updater wait on
    this before replacing files).
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return False
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    # No such mutex: nobody holds it. Anything else (access denied) means it
    # exists - someone has it.
    return ctypes.get_last_error() != _ERROR_FILE_NOT_FOUND


def _decode(data):
    """A message dict from raw bytes, or None if it isn't one."""
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return msg if isinstance(msg, dict) else None


def serve(handler, address=PIPE_NAME):
    """
    Answer control messages on a daemon thread (returned). The pipe is opened
    inside the thread and retried every RETRY_SECONDS until it can be (say, a
    previous Scribe's pipe is still closing); an accept() error is logged and
    the loop carries on after ACCEPT_BACKOFF - the channel never silently
    dies for the rest of the session. handler(msg) runs for each message
    (keep it quick - it blocks the next one); its dict return value is the
    reply, and None means a plain {"ok": true}.
    """
    def loop():
        last_log = [0.0]

        def log(what, exc):
            now = time.monotonic()
            if now - last_log[0] >= LOG_EVERY:
                last_log[0] = now
                storage.log_error(what, exc)

        listener = None
        while listener is None:
            try:
                listener = Listener(address, family="AF_PIPE")
            except OSError as exc:
                log("control pipe", exc)
                time.sleep(RETRY_SECONDS)
        while True:
            try:
                conn = listener.accept()
            except Exception as exc:
                # Any error - OSError, or multiprocessing's own AssertionError
                # from a connection that failed half-way: log it, keep serving.
                log("control pipe accept", exc)
                time.sleep(ACCEPT_BACKOFF)
                continue
            try:
                with conn:
                    if not conn.poll(CLIENT_SEND_WAIT):
                        continue                   # connected but said nothing
                    msg = _decode(conn.recv_bytes(MAX_MESSAGE_BYTES))
                    if msg is None:
                        continue
                    reply = handler(msg)
                    conn.send_bytes(json.dumps(
                        reply if reply is not None else {"ok": True}).encode("utf-8"))
            except Exception as exc:
                storage.log_error("control message", exc)

    thread = threading.Thread(target=loop, name="control-pipe", daemon=True)
    thread.start()
    return thread


def request(message, address=PIPE_NAME, timeout=2.0):
    """Send one message and wait for its reply dict. None if no Scribe is
    running, or it didn't answer within `timeout` seconds. Never raises."""
    try:
        with Client(address, family="AF_PIPE") as conn:
            conn.send_bytes(json.dumps(message).encode("utf-8"))
            if not conn.poll(timeout):
                return None
            return _decode(conn.recv_bytes(MAX_MESSAGE_BYTES))
    except (OSError, EOFError, ValueError):
        return None


def send(message, address=PIPE_NAME, timeout=2.0):
    """Send one message; True if a running Scribe received it."""
    return request(message, address=address, timeout=timeout) is not None
