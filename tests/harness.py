"""
Shared test harness: import app.py safely.

Importing app.py runs its whole startup (settings, model load, mic, the
single-instance guard). This helper imports it with every side effect
contained, so a test can never touch your real data, microphone, or a
running Scribe:
  - SCRIBE_DATA_DIR -> a fresh temp folder (with a config.json, so it isn't
    a first run), set BEFORE storage.py is imported
  - model_manager.find_cached -> "already on disk" (never a download), and
    faster_whisper.WhisperModel -> a stub that "transcribes" to a fixed line
  - sounddevice.InputStream -> a MagicMock (or your own side effect)
  - instance.py names derive from the temp data folder, so the single-
    instance mutex and control pipe can never reach a running Scribe
  - httpx can't send and urllib can't open: no test reaches the network
    (Groq, warm-ups, GitHub's update check...)
"""

import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

import urllib.error

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# sound_cues off: a test that exercises stop_recording must never play a real
# tone on the speakers of the machine running it. polish off: a dictation
# that happens to have a Groq key must never call the real Groq to polish
# (polish_flow_test turns it on with a fake client).
DEFAULT_TEST_CONFIG = {"hotkey": "Ctrl + Win", "sound_cues": False, "polish": False}


def import_app_with_mocks(config=DEFAULT_TEST_CONFIG, config_text=None,
                          input_stream=None):
    """
    Import a fresh app module. `config` is written as config.json (pass None
    for no file = first run); `config_text` writes raw text instead (for
    corrupt-file tests). `input_stream` replaces sounddevice.InputStream
    (e.g. a MagicMock with side_effect=... to simulate no microphone).
    """
    data_dir = tempfile.mkdtemp(prefix="scribe-test-")
    os.environ["SCRIBE_DATA_DIR"] = data_dir
    if config_text is not None:
        with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as f:
            f.write(config_text)
    elif config is not None:
        config = dict(config)
        config.setdefault("polish", False)    # never the real Groq by accident
        with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as f:
            json.dump(config, f)

    def fake_transcribe(*args, **kwargs):
        # A FRESH iterator per call, like faster-whisper's generator.
        return (iter([SimpleNamespace(text="local stub said hello")]),
                SimpleNamespace())
    fake_model = SimpleNamespace(transcribe=mock.MagicMock(side_effect=fake_transcribe))

    import ctypes
    # Fresh copies of Scribe's modules, imported AFTER SCRIBE_DATA_DIR is set
    # (they read it at import) and BEFORE the patches below (which patch them).
    for name in ("app", "storage", "devices", "instance", "keystore",
                 "autostart", "model_manager", "updates"):
        sys.modules.pop(name, None)
    patches = [
        mock.patch("faster_whisper.WhisperModel", mock.MagicMock(return_value=fake_model)),
        mock.patch("sounddevice.InputStream", input_stream or mock.MagicMock()),
        mock.patch("sounddevice.query_devices", return_value=[]),
        # Every model is "already downloaded" (to the temp folder): a test
        # never touches the network; loading gives the stub model above.
        mock.patch("model_manager.find_cached", return_value=data_dir),
        # app.py's crash handler explains fatal errors in a native message
        # box. In a test that would pop up on the desktop and block, so the
        # Win32 call itself is stubbed (returns IDOK).
        mock.patch.object(ctypes.windll.user32, "MessageBoxW", return_value=1),
        # No test ever reaches the network: every real HTTP request (Groq's
        # SDK, a polish warm-up thread, an account check) fails at once as if
        # offline. Tests that need an answer patch the client themselves.
        mock.patch("httpx.Client.send",
                   side_effect=httpx.ConnectError("tests never reach the network")),
        mock.patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("tests never reach the network")),
    ]
    for p in patches:
        p.start()
    try:
        import app  # noqa: E402
    finally:
        # app.py installs its own sys.excepthook (log + message box). Put the
        # standard one back so a failing test prints its traceback normally.
        # (threading.excepthook stays - notify_test checks it.)
        sys.excepthook = sys.__excepthook__
    # The model loads on a background thread; let it finish (and report) so
    # a test starts from a settled app.
    app.models.join(5)
    app._fake_local_model = fake_model
    app._test_data_dir = data_dir
    return app
