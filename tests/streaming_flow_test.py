r"""
Standalone probe for app.py's ElevenLabs path: a session per dictation, fed
by the mic, finished on release - and every failure falling back to Groq or
the local model with one clear notice.

Run from the project root:   venv\Scripts\python tests\streaming_flow_test.py

Safe: tests/harness.py (temp data folder, mocked mic and model); the
ElevenLabs sessions are fakes, so no network and no key.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402


class FakeSession:
    """Stands in for elevenlabs_stream.Session: records what app.py does."""
    made = []

    def __init__(self, key, terms=(), **kw):
        self.key, self.api_key, self.terms = key, key, list(terms)
        self.fed, self.finished, self.cancelled = [], False, False
        self.result, self.error = "hello from eleven", None
        self.streamed_seconds, self.connect_ms, self.final_ms = 1.5, 120.0, 200.0
        FakeSession.made.append(self)

    def start(self):
        return self

    def feed(self, chunk):
        self.fed.append(chunk)

    def finish(self, timeout=2.0):
        self.finished = True
        if self.error is not None:
            raise self.error
        return self.result

    def cancel(self):
        self.cancelled = True


def _eleven(app, groq_key=""):
    """ElevenLabs as the cloud service (Groq only if `groq_key`), all healthy."""
    app.USE_CLOUD, app.CLOUD_PROVIDER = True, "elevenlabs"
    app.ELEVENLABS_API_KEY, app.GROQ_API_KEY = "xi_live", groq_key
    app.eleven_paused_until = app.cloud_paused_until = 0.0
    app._eleven_rejected_key = app._rejected_key = None
    app._notice_last.clear()
    app.tray_icon = mock.MagicMock()
    app._tray_ready = True
    app._stream_session = None
    FakeSession.made.clear()


def _titles(app):
    return [c.args[1] for c in app.tray_icon.notify.call_args_list]


def _run_take(app, session, audio=None):
    """process_audio with delivery captured: returns (output, kwargs), or
    (None, None) if nothing was delivered."""
    audio = np.zeros(16000, dtype=np.float32) if audio is None else audio
    with mock.patch.object(app, "_deliver_job") as dj:
        app.process_audio(audio, None, session=session, released_at=time.monotonic())
    if not dj.called:
        return None, None
    return dj.call_args.args[0], dj.call_args.kwargs


def test_press_starts_a_session_the_mic_feeds_it_release_hands_it_over(app):
    _eleven(app)
    with mock.patch.object(app.elevenlabs_stream, "Session", FakeSession), \
         mock.patch.object(app, "process_audio") as pa:
        app.recording = True
        assert app.start_recording()
        s = FakeSession.made[-1]
        assert s.key == "xi_live" and app._stream_session is s
        chunk = np.full((160, 1), 0.1, dtype=np.float32)
        app.audio_callback(chunk, 160, None, None)
        assert len(s.fed) == 1 and np.array_equal(s.fed[0], chunk)
        app._finish_recording()
        time.sleep(0.2)
    assert app._stream_session is None
    assert pa.call_args.kwargs["session"] is s and pa.call_args.kwargs["released_at"]
    print("PASS  press opens a session, the mic feeds it, release hands it to the worker.")


def test_no_session_unless_elevenlabs_is_the_usable_service(app):
    with mock.patch.object(app.elevenlabs_stream, "Session", FakeSession):
        _eleven(app)
        app.CLOUD_PROVIDER, app.GROQ_API_KEY = "groq", "gsk_x"
        app.start_recording()
        app._cancel_recording()
        assert not FakeSession.made, "Groq is the service"
        _eleven(app, groq_key="gsk_x")
        app.eleven_paused_until = time.monotonic() + 60
        app.start_recording()
        app._cancel_recording()
        assert not FakeSession.made, "paused, and Groq can take it"
        _eleven(app)
        app.eleven_paused_until = time.monotonic() + 60
        with mock.patch.object(app, "_local_model", return_value=None):
            app.start_recording()
            app._cancel_recording()
        assert FakeSession.made, "paused but the only backend: still tried"
        assert FakeSession.made[-1].cancelled
    print("PASS  a session opens only when ElevenLabs is the service (or the only backend).")


def test_release_uses_the_streamed_text(app):
    _eleven(app)
    s = FakeSession("xi_live")
    output, kw = _run_take(app, s)
    assert s.finished and output.strip() == "Hello from eleven", output
    assert kw["engine"] == "elevenlabs"
    print("PASS  on release the streamed text is used (engine: elevenlabs).")


FAILURES = [  # kind, notice title, pause seconds (None = key rejected)
    ("auth", "ElevenLabs API key rejected", None),
    ("quota", "ElevenLabs time used up", 3600),
    ("busy", "ElevenLabs is busy", 60),
    ("terms", "Accept ElevenLabs' terms", 3600),
    ("network", "Couldn't reach ElevenLabs", 30),
    ("timeout", "Couldn't reach ElevenLabs", 30),
    ("server", "ElevenLabs couldn't transcribe that", 0),
]


def test_every_failure_falls_back_with_one_notice(app):
    es = app.elevenlabs_stream
    for kind, title, pause in FAILURES:
        _eleven(app, groq_key="gsk_backup")
        s = FakeSession("xi_live")
        s.error = es.StreamError(kind, "detail")
        with mock.patch.object(app, "transcribe_cloud", return_value="groq text"):
            output, kw = _run_take(app, s)
        assert output.strip() == "Groq text" and kw["engine"] == "groq", (kind, output)
        assert _titles(app) == [title], (kind, _titles(app))
        assert "Scribe used Groq instead." in app.tray_icon.notify.call_args.args[0]
        if pause is None:
            assert app._eleven_rejected_key == "xi_live" and not app._eleven_configured()
        else:
            # (+1e-6: Windows' clock ticks every ~15 ms, so "now" can be the
            # same instant the pause was set - and the sum rounds a hair over.)
            left = app.eleven_paused_until - time.monotonic()
            assert (pause - 5 < left <= pause + 1e-6) if pause else left <= 1e-6, (kind, left)
    _eleven(app)                                     # no Groq: the local model
    s = FakeSession("xi_live")
    s.error = es.StreamError("network")
    output, kw = _run_take(app, s)
    assert output.strip() == "Local stub said hello" and kw["engine"] == "local", output
    assert "Scribe used the local model instead." in app.tray_icon.notify.call_args.args[0]
    print("PASS  every ElevenLabs failure falls back (Groq, then local) with one notice.")


def test_only_backend_failure_says_why_once(app):
    _eleven(app)
    s = FakeSession("xi_live")
    s.error = app.elevenlabs_stream.StreamError("quota", "used up")
    with mock.patch.object(app, "_local_model", return_value=None):
        output, _kw = _run_take(app, s)
    assert output is None
    assert _titles(app) == ["Couldn't transcribe that"], _titles(app)
    msg = app.tray_icon.notify.call_args.args[0]
    assert "hours for this month are used up" in msg and msg.endswith("Nothing was typed."), msg
    assert app.eleven_paused_until > time.monotonic()
    print("PASS  ElevenLabs as the only backend: one notice that says why; nothing typed.")


def test_cancel_and_short_takes_never_commit(app):
    _eleven(app)
    s = FakeSession("xi_live")
    app._stream_session, app.recording = s, True
    app._cancel_recording()
    assert s.cancelled and not s.finished and app._stream_session is None
    s = FakeSession("xi_live")
    output, _ = _run_take(app, s, audio=np.zeros(1000, dtype=np.float32))
    assert output is None and s.cancelled and not s.finished
    print("PASS  a cancelled or too-short take closes its stream without a commit.")


def test_groq_speculation_is_off_when_elevenlabs_streams(app):
    _eleven(app, groq_key="gsk_x")
    app.PIPELINE_CLOUD = True
    with mock.patch.object(app.elevenlabs_stream, "Session", FakeSession), \
         mock.patch.object(app, "_speculative_worker") as worker:
        app.start_recording()
        time.sleep(0.1)
        app._cancel_recording()
    assert not worker.called
    print("PASS  Groq's speculative pipeline doesn't run alongside a stream.")


def test_usage_is_tagged_and_groq_quota_ignores_it(app):
    _eleven(app)
    os.makedirs(os.path.dirname(app.CLOUD_USAGE_LOG), exist_ok=True)
    open(app.CLOUD_USAGE_LOG, "w").close()
    s = FakeSession("xi_live")
    s.streamed_seconds = 25000.0                     # would be 87% of Groq's day
    _run_take(app, s)
    lines = [json.loads(line) for line in open(app.CLOUD_USAGE_LOG, encoding="utf-8")]
    assert lines[-1]["provider"] == "elevenlabs" and lines[-1]["audio_seconds"] == 25000.0
    app.GROQ_API_KEY = "gsk_x"
    app._quota_cache_sig = None
    assert app._compute_quota_state() == ("ok", 0.0)
    old = (datetime.now() - timedelta(days=20)).isoformat(timespec="seconds")
    with open(app.CLOUD_USAGE_LOG, "w", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": old, "audio_seconds": 3, "model": "m"}) + "\n")
        f.write(json.dumps({"timestamp": old, "audio_seconds": 4, "model": "s",
                            "provider": "elevenlabs"}) + "\n")
    app._trim_cloud_usage_log()
    kept = [json.loads(line) for line in open(app.CLOUD_USAGE_LOG, encoding="utf-8")]
    assert [k.get("provider") for k in kept] == ["elevenlabs"], kept
    print("PASS  streamed seconds are logged as ElevenLabs; Groq's quota ignores them; kept 40 days.")


def test_history_records_engine_and_latency(app):
    _eleven(app)
    with mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "deliver"), \
         mock.patch.object(app, "_any_modifier_down", return_value=False):
        app._deliver_job("Hi. ", 123, "App", "app.exe", None, 1.0, log_text="Hi.",
                         engine="elevenlabs", released_at=time.monotonic() - 0.4)
    last = json.loads(open(app.LOG_FILE, encoding="utf-8").read().splitlines()[-1])
    assert last["engine"] == "elevenlabs" and 0.3 < last["latency"] < 5, last
    print("PASS  history records which service transcribed and how fast it landed.")


def test_blocker_counts_elevenlabs(app):
    _eleven(app)
    with mock.patch.object(app, "_local_model", return_value=None):
        assert app._dictation_blocker() is None
        app._eleven_rejected_key = "xi_live"
        assert app._dictation_blocker()[0] == "no_backend"
    print("PASS  ElevenLabs counts as a backend; a rejected key doesn't.")


def test_controller_errors_never_leave_a_stream_open(app):
    _eleven(app)
    # An exception mid-recording: the listener's error handler closes it.
    s = FakeSession("xi_live")
    app._stream_session, app.recording = s, True
    app.log_listener_error("test", RuntimeError("boom"))
    assert s.cancelled and app._stream_session is None
    # A leftover session is never silently replaced by the next one.
    stale = FakeSession("xi_live")
    app._stream_session = stale
    with mock.patch.object(app.elevenlabs_stream, "Session", FakeSession):
        app.start_recording()
        app._cancel_recording()
    assert stale.cancelled
    # The worker thread can't start: its stream is closed, and the error
    # still reaches the controller's handler.
    s = FakeSession("xi_live")
    app._stream_session, app.recording = s, True
    with mock.patch.object(app.threading, "Thread", side_effect=RuntimeError("no threads")):
        try:
            app._finish_recording()
        except RuntimeError:
            pass
        else:
            raise AssertionError("the error must still surface")
    assert s.cancelled
    print("PASS  a hotkey error, a leftover or a failed worker never leaves a stream open.")


def test_offline_notices_say_what_really_happened(app):
    import groq
    import httpx
    offline = groq.APIConnectionError(request=httpx.Request("POST", "https://api.groq.com"))
    # Offline, backup model on disk: Groq fails too, the local model answers.
    _eleven(app, groq_key="gsk_x")
    s = FakeSession("xi_live")
    s.error = app.elevenlabs_stream.StreamError("network")
    with mock.patch.object(app, "transcribe_cloud", side_effect=offline):
        output, kw = _run_take(app, s)
    assert kw["engine"] == "local", kw
    messages = [c.args[0] for c in app.tray_icon.notify.call_args_list]
    assert _titles(app) == ["Cloud transcription unavailable", "Couldn't reach ElevenLabs"], _titles(app)
    assert messages[-1] == "Scribe used the local model instead.", messages
    assert not any("Groq for now" in m or "Groq instead" in m for m in messages), messages
    # Offline, cloud only: nothing can transcribe - the last notice says why.
    _eleven(app, groq_key="gsk_x")
    s = FakeSession("xi_live")
    s.error = app.elevenlabs_stream.StreamError("network")
    with mock.patch.object(app, "transcribe_cloud", side_effect=offline), \
         mock.patch.object(app, "_local_model", return_value=None):
        output, _kw = _run_take(app, s)
    assert output is None
    assert _titles(app) == ["Cloud transcription unavailable", "Couldn't transcribe that"], _titles(app)
    last = app.tray_icon.notify.call_args.args[0]
    assert last.startswith("Couldn't reach ElevenLabs.") and last.endswith("Nothing was typed."), last
    print("PASS  offline: every notice says what really happened - no 'using Groq' that didn't.")


def test_a_new_key_or_service_ends_the_pause(app):
    _eleven(app)
    app.storage.save_config_changes({"use_cloud": True, "cloud_provider": "elevenlabs",
                                     "elevenlabs_api_key": "xi_live"})
    app.reload_config()
    app.eleven_paused_until = app.time.monotonic() + 3600
    app.reload_config()                               # nothing changed: still paused
    assert app.eleven_paused_until > app.time.monotonic()
    app.storage.save_config_changes({"elevenlabs_api_key": "xi_new_key"})
    app.reload_config()                               # a new key: try it right away
    assert app.ELEVENLABS_API_KEY == "xi_new_key" and app.eleven_available()
    app.eleven_paused_until = app.time.monotonic() + 3600
    app.storage.save_config_changes({"cloud_provider": "groq"})
    app.reload_config()                               # another service: pause over
    assert app.eleven_paused_until <= app.time.monotonic()
    app.storage.save_config_changes({"cloud_provider": "groq", "use_cloud": False,
                                     "elevenlabs_api_key": ""})
    app.reload_config()
    print("PASS  a new ElevenLabs key or service is tried at once, not after the pause.")


def test_a_loading_backup_model_is_waited_for(app):
    _eleven(app)
    s = FakeSession("xi_live")
    s.error = app.elevenlabs_stream.StreamError("network")
    with mock.patch.object(app, "_local_model", return_value=None), \
         mock.patch.object(type(app.models), "state", new_callable=mock.PropertyMock,
                           return_value="loading"), \
         mock.patch.object(app, "_use_speculative_or_transcribe",
                           return_value="local text after loading") as fallback:
        output, _kw = _run_take(app, s)
    assert fallback.called, "a model that is loading is a backup - wait for it"
    assert output.strip() == "Local text after loading", output
    print("PASS  an ElevenLabs failure while the backup model loads waits for it.")


def test_a_rejected_key_is_told_once_and_only_for_that_key(app):
    es = app.elevenlabs_stream
    _eleven(app, groq_key="gsk_backup")
    with mock.patch.object(app, "transcribe_cloud", return_value="groq text"):
        for _ in range(2):                     # two overlapping takes, same bad key
            s = FakeSession("xi_live")
            s.error = es.StreamError("auth", "401")
            _run_take(app, s)
    assert _titles(app).count("ElevenLabs API key rejected") == 1, _titles(app)
    _eleven(app, groq_key="gsk_backup")
    app.ELEVENLABS_API_KEY = "xi_new"          # replaced in Settings mid-take
    s = FakeSession("xi_old")
    s.error = es.StreamError("auth", "401")
    with mock.patch.object(app, "transcribe_cloud", return_value="groq text"):
        _run_take(app, s)
    assert app._eleven_rejected_key == "xi_old" and app._eleven_configured()
    assert "ElevenLabs API key rejected" not in _titles(app)
    print("PASS  a rejected ElevenLabs key: told once, and never blamed on a new key.")


def test_the_mic_stops_before_the_stream_is_handed_over(app):
    _eleven(app)
    order = []
    real_take = app._take_stream_session

    def stop():
        order.append("mic stopped")
        return np.zeros(16000, dtype=np.float32)

    def take():
        order.append("stream handed over")
        return real_take()
    app._stream_session, app.recording = FakeSession("xi_live"), True
    with mock.patch.object(app, "stop_recording", side_effect=stop), \
         mock.patch.object(app, "_take_stream_session", side_effect=take), \
         mock.patch.object(app, "process_audio"):
        app._finish_recording()
    assert order == ["mic stopped", "stream handed over"], order
    print("PASS  the mic stops before the stream goes to the worker (so it has every chunk).")


def test_streamed_time_is_logged_when_a_take_fails(app):
    _eleven(app, groq_key="gsk_backup")
    open(app.CLOUD_USAGE_LOG, "w").close()
    s = FakeSession("xi_live")
    s.streamed_seconds = 3.0
    s.error = app.elevenlabs_stream.StreamError("network")
    with mock.patch.object(app, "transcribe_cloud", return_value="groq text"):
        _run_take(app, s)
    lines = [json.loads(line) for line in open(app.CLOUD_USAGE_LOG, encoding="utf-8")]
    assert [(e["provider"], e["audio_seconds"]) for e in lines] == [("elevenlabs", 3.0)], lines
    print("PASS  streamed time is counted even when ElevenLabs then fails (it's billed).")


if __name__ == "__main__":
    app = import_app_with_mocks()
    test_press_starts_a_session_the_mic_feeds_it_release_hands_it_over(app)
    test_no_session_unless_elevenlabs_is_the_usable_service(app)
    test_release_uses_the_streamed_text(app)
    test_every_failure_falls_back_with_one_notice(app)
    test_only_backend_failure_says_why_once(app)
    test_cancel_and_short_takes_never_commit(app)
    test_groq_speculation_is_off_when_elevenlabs_streams(app)
    test_usage_is_tagged_and_groq_quota_ignores_it(app)
    test_history_records_engine_and_latency(app)
    test_blocker_counts_elevenlabs(app)
    test_controller_errors_never_leave_a_stream_open(app)
    test_offline_notices_say_what_really_happened(app)
    test_a_new_key_or_service_ends_the_pause(app)
    test_a_loading_backup_model_is_waited_for(app)
    test_a_rejected_key_is_told_once_and_only_for_that_key(app)
    test_the_mic_stops_before_the_stream_is_handed_over(app)
    test_streamed_time_is_logged_when_a_take_fails(app)
    print("\nAll streaming flow tests passed.")
