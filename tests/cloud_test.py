"""
Standalone probe for the cloud-transcription code path added to app.py.

Run from the project root:

    venv/Scripts/python.exe tests/cloud_test.py

What it checks (each block prints PASS/FAIL):
  1. The `groq` SDK is installed and imports.
  2. _audio_to_wav_bytes produces a valid WAV: round-trip a 1 kHz sine and
     verify sample rate, channels, sample width, and that decoded samples
     match the input within int16 quantization error.
  3. _audio_to_wav_bytes clips out-of-range floats so a hot mic does not
     wrap around to noise.
  4. save_config / load_config round-trip all cloud fields.
  5. transcribe_cloud raises a clear error when GROQ_API_KEY is empty.
  6. transcribe_audio uses local when USE_CLOUD is False.
  7. transcribe_audio falls back to local when the cloud call raises.
  8. transcribe_audio rebuilds the Groq client when the API key changes
     (no stale client reused after a key update from Settings).

Uses tests/harness.py, which mocks app.py's heavy module-level side effects
(Whisper model load, mic stream, single-instance port) and points it at a
temp data folder - so this probe needs no microphone or network, and never
touches your real settings or a running Scribe.
"""

import io
import os
import sys
import tempfile
import wave
from types import SimpleNamespace
from unittest import mock

import numpy as np


# tests/ is on sys.path when this file runs, so the shared harness imports
# directly. It imports app.py against a temp data folder with the model,
# mic and single-instance port mocked - see tests/harness.py.
from harness import import_app_with_mocks  # noqa: E402


# -----------------------------------------------------------------------------

def test_groq_imports():
    import groq  # noqa: F401
    print("PASS  groq SDK imports.")


def test_wav_roundtrip(app):
    # 1 kHz sine, 0.5 s, mono - the kind of clean signal we want to send.
    t = np.arange(int(app.SAMPLE_RATE * 0.5), dtype=np.float32) / app.SAMPLE_RATE
    audio = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)

    wav_bytes = app._audio_to_wav_bytes(audio)
    assert isinstance(wav_bytes, (bytes, bytearray)), "should return bytes"

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getframerate() == app.SAMPLE_RATE, "sample rate must match"
        assert wf.getnchannels() == app.CHANNELS, "channels must match"
        assert wf.getsampwidth() == 2, "int16 expected (2 bytes per sample)"
        decoded = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)

    # Decoded should be input * 32767 within rounding.
    expected = (audio * 32767).astype(np.int16)
    assert decoded.shape == expected.shape, "frame count must match"
    # Allow off-by-one from rounding.
    assert np.max(np.abs(decoded.astype(int) - expected.astype(int))) <= 1, \
        "decoded samples must match input within int16 rounding"
    print("PASS  _audio_to_wav_bytes round-trips a sine wave correctly.")


def test_wav_clipping(app):
    # Out-of-range floats (a hot mic) must clip, not wrap.
    hot = np.array([2.0, -2.0, 0.0, 1.5, -1.5], dtype=np.float32)
    wav_bytes = app._audio_to_wav_bytes(hot)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        decoded = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    # All samples should clip to +/-32767, not wrap to negative.
    assert decoded[0] == 32767, f"+2.0 should clip to 32767, got {decoded[0]}"
    assert decoded[1] == -32767, f"-2.0 should clip to -32767, got {decoded[1]}"
    assert decoded[2] == 0
    print("PASS  _audio_to_wav_bytes clips out-of-range floats.")


def test_config_roundtrip(app):
    # The harness's temp data folder holds this app's config, so nothing here
    # touches your real one. The key is saved through the keystore (in a
    # temp folder that's config.json's fallback slot) - save_config never
    # writes it.
    original_use_cloud = app.USE_CLOUD
    original_key = app.GROQ_API_KEY
    original_cloud_model = app.CLOUD_MODEL
    try:
        app.USE_CLOUD = True
        app.CLOUD_MODEL = "whisper-large-v3-turbo"
        app.save_config()
        app.keystore.set_key("gsk_test_key_xyz")

        # Clear them, then reload from disk.
        app.USE_CLOUD = False
        app.GROQ_API_KEY = ""
        app.CLOUD_MODEL = ""
        app.load_config()

        assert app.USE_CLOUD is True, f"USE_CLOUD lost: got {app.USE_CLOUD!r}"
        assert app.GROQ_API_KEY == "gsk_test_key_xyz", \
            f"API key lost: got {app.GROQ_API_KEY!r}"
        assert app.CLOUD_MODEL == "whisper-large-v3-turbo", \
            f"CLOUD_MODEL lost: got {app.CLOUD_MODEL!r}"
        print("PASS  Cloud settings and the keystore's key survive a reload.")
    finally:
        app.keystore.delete_key()
        app.USE_CLOUD = original_use_cloud
        app.GROQ_API_KEY = original_key
        app.CLOUD_MODEL = original_cloud_model
        app.save_config()


def test_transcribe_cloud_no_key(app):
    app.GROQ_API_KEY = ""
    try:
        app.transcribe_cloud(np.zeros(1600, dtype=np.float32))
    except RuntimeError as exc:
        assert "key" in str(exc).lower(), \
            f"error should mention 'key', got: {exc!r}"
        print("PASS  transcribe_cloud raises a clear error with no API key.")
        return
    raise AssertionError("expected RuntimeError when key is empty")


def test_dispatch_local_when_cloud_off(app):
    app.USE_CLOUD = False
    app.GROQ_API_KEY = ""
    app._fake_local_model.transcribe.reset_mock()
    text = app.transcribe_audio(np.zeros(1600, dtype=np.float32))
    assert text == "local stub said hello", \
        f"local backend should be used, got {text!r}"
    assert app._fake_local_model.transcribe.called, \
        "local model.transcribe must have been called"
    print("PASS  Dispatcher uses LOCAL when USE_CLOUD is False.")


def test_dispatch_falls_back_on_cloud_error(app):
    """Cloud raises -> dispatcher quietly switches to local."""
    app.USE_CLOUD = True
    app.GROQ_API_KEY = "gsk_fake_for_fallback_test"
    app._fake_local_model.transcribe.reset_mock()
    with mock.patch.object(
        app, "transcribe_cloud", side_effect=RuntimeError("simulated outage")
    ):
        text = app.transcribe_audio(np.zeros(1600, dtype=np.float32))
    assert text == "local stub said hello", \
        f"fallback should yield local text, got {text!r}"
    assert app._fake_local_model.transcribe.called, \
        "local model should be hit as fallback"
    print("PASS  Dispatcher falls back to LOCAL when cloud raises.")


def test_dispatch_skips_cloud_when_key_missing(app):
    """USE_CLOUD=True but no key -> goes straight to local, never tries cloud."""
    app.USE_CLOUD = True
    app.GROQ_API_KEY = ""
    app._fake_local_model.transcribe.reset_mock()
    with mock.patch.object(app, "transcribe_cloud") as cloud_spy:
        text = app.transcribe_audio(np.zeros(1600, dtype=np.float32))
    assert text == "local stub said hello"
    assert not cloud_spy.called, \
        "cloud should NOT be called when key is missing"
    print("PASS  Dispatcher skips cloud when key is empty.")


def test_client_rebuilds_on_key_change(app):
    """Pasting a new API key in Settings must invalidate the cached client."""
    # Reset the cache so the test starts clean.
    app._groq_client = None
    app._groq_client_key = None

    fake_groq_module = SimpleNamespace(Groq=mock.MagicMock(
        side_effect=lambda api_key, **kwargs: SimpleNamespace(api_key=api_key)
    ))
    with mock.patch.dict(sys.modules, {"groq": fake_groq_module}):
        app.GROQ_API_KEY = "key-one"
        client_one = app._get_groq_client()
        client_one_again = app._get_groq_client()
        assert client_one is client_one_again, \
            "same key -> same client instance"
        assert fake_groq_module.Groq.call_count == 1, \
            "Groq() should be called once for repeated same-key access"

        app.GROQ_API_KEY = "key-two"
        client_two = app._get_groq_client()
        assert client_two is not client_one, \
            "new key -> new client must be built"
        assert fake_groq_module.Groq.call_count == 2, \
            "Groq() must be called again for the new key"
    print("PASS  _get_groq_client rebuilds when the API key changes.")


# -----------------------------------------------------------------------------
#  Silence trimming. Whisper "hears" words in silence - with Scribe's prompt,
#  5 s of plain room tone comes back from Groq as "Thank you for watching!".
#  trim_silence() must cut the non-speech out before anything is uploaded.
# -----------------------------------------------------------------------------

def room_noise(app, seconds, rms=0.01, seed=7):
    """Pink-ish (1/f) noise: closer to a laptop fan / room tone than white."""
    n = int(seconds * app.SAMPLE_RATE)
    spectrum = np.fft.rfft(np.random.default_rng(seed).standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / app.SAMPLE_RATE)
    freqs[0] = 1
    x = np.fft.irfft(spectrum / np.sqrt(freqs), n)
    return (x / np.sqrt(np.mean(x ** 2)) * rms).astype(np.float32)


def tts_speech(app):
    """
    A real spoken sentence, made on the fly with Windows' built-in text-to-
    speech (System.Speech) so the repo needs no binary audio fixture. The VAD
    only fires on actual speech - a sine wave would not count.
    """
    import subprocess
    path = os.path.join(tempfile.gettempdir(), "scribe_vad_test_speech.wav")
    if not os.path.exists(path):
        ps = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
            f"{app.SAMPLE_RATE}, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
            " [System.Speech.AudioFormat.AudioChannel]::Mono);"
            f"$s.SetOutputToWaveFile('{path}', $f);"
            "$s.Speak('Remove the Monday help session. We only have Wednesday.');"
            "$s.Dispose()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    with wave.open(path, "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768


def test_trim_silence_drops_pure_noise(app):
    trimmed = app.trim_silence(room_noise(app, 5))
    assert trimmed is None, \
        f"5 s of room tone holds no speech; expected None, got {len(trimmed)} samples"
    print("PASS  trim_silence returns None for room tone with no speech.")


def test_trim_silence_keeps_speech(app, speech):
    sr = app.SAMPLE_RATE
    # The shape of a real push-to-talk take: a beat of dead air after the
    # hotkey goes down, the sentence, then a pause before the release.
    audio = np.concatenate([room_noise(app, 1, seed=1), speech,
                            room_noise(app, 4, seed=2)])
    trimmed = app.trim_silence(audio)
    assert trimmed is not None, "speech was present; must not return None"
    assert len(trimmed) < len(audio) - 3 * sr, \
        f"most of the 5 s of dead air should be cut: {len(audio)/sr:.1f}s -> {len(trimmed)/sr:.1f}s"
    assert len(trimmed) >= 0.8 * len(speech), \
        f"speech must survive: {len(speech)/sr:.1f}s spoken, {len(trimmed)/sr:.1f}s kept"
    print(f"PASS  trim_silence keeps the speech and cuts the dead air "
          f"({len(audio)/sr:.1f}s -> {len(trimmed)/sr:.1f}s).")


def _fake_groq_client():
    """A Groq stand-in whose transcription call records what it was sent."""
    create = mock.MagicMock(return_value=SimpleNamespace(text="stub text"))
    client = SimpleNamespace(audio=SimpleNamespace(
        transcriptions=SimpleNamespace(create=create)))
    return client, create


def test_transcribe_cloud_skips_groq_on_silence(app):
    app.GROQ_API_KEY = "gsk_fake_for_vad_test"
    client, create = _fake_groq_client()
    with mock.patch.object(app, "_get_groq_client", return_value=client), \
         mock.patch.object(app, "log_cloud_call") as log_spy:
        text = app.transcribe_cloud(room_noise(app, 5))
    assert text == "", f"no speech -> no text, got {text!r}"
    assert not create.called, "Groq must not be called when there is no speech"
    assert not log_spy.called, "a skipped call must not count against the quota"
    print("PASS  transcribe_cloud skips the Groq call when there is no speech.")


def test_transcribe_cloud_uploads_trimmed_audio(app, speech):
    sr = app.SAMPLE_RATE
    app.GROQ_API_KEY = "gsk_fake_for_vad_test"
    audio = np.concatenate([room_noise(app, 1, seed=3), speech,
                            room_noise(app, 4, seed=4)])
    client, create = _fake_groq_client()
    with mock.patch.object(app, "_get_groq_client", return_value=client), \
         mock.patch.object(app, "log_cloud_call") as log_spy:
        text = app.transcribe_cloud(audio)
    assert text == "stub text"
    _name, wav_bytes, _mime = create.call_args.kwargs["file"]
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sent_seconds = wf.getnframes() / sr
    assert sent_seconds < len(audio) / sr - 3, \
        f"upload should be trimmed: {len(audio)/sr:.1f}s recorded, {sent_seconds:.1f}s sent"
    logged_seconds = log_spy.call_args.args[0]
    assert abs(logged_seconds - sent_seconds) < 0.01, \
        "quota log must count the seconds actually sent, not the raw recording"
    print(f"PASS  transcribe_cloud uploads only the speech "
          f"({len(audio)/sr:.1f}s recorded -> {sent_seconds:.1f}s sent).")


# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
#  Network failures. The Groq SDK's defaults could stall a dictation ~3 min;
#  Scribe must fail fast, classify the error, tell the user once, and fall
#  back to the local model.
# -----------------------------------------------------------------------------

import httpx  # noqa: E402


def _req():
    return httpx.Request("POST", "https://api.groq.com/openai/v1/audio/transcriptions")


def _reset_cloud(app):
    app.USE_CLOUD, app.GROQ_API_KEY = True, "gsk_live"
    app.cloud_paused_until = 0.0
    app._rejected_key = None
    app._notice_last.clear()
    app.tray_icon = mock.MagicMock()
    app._tray_ready = True


def test_client_has_bounded_timeout_and_no_retries(app):
    import groq
    app._groq_client = None
    with mock.patch.object(groq, "Groq") as ctor:
        app.GROQ_API_KEY = "gsk_timeout_check"
        app._get_groq_client()
    kwargs = ctor.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["timeout"].read == app.CLOUD_TIMEOUT_SECONDS
    assert kwargs["timeout"].connect == app.CLOUD_CONNECT_TIMEOUT_SECONDS
    app._groq_client = None
    print("PASS  Groq client: 12 s read / 4 s connect timeout, no hidden retries.")


def test_the_client_is_labelled_with_the_key_it_was_built_with(app):
    import groq
    app._groq_client = None
    app.GROQ_API_KEY = "gsk_old_key"
    built_with = []

    def build(**kw):
        built_with.append(kw["api_key"])
        app.GROQ_API_KEY = "gsk_new_key"     # Settings swapped the key meanwhile
        return mock.MagicMock()
    with mock.patch.object(groq, "Groq", side_effect=build):
        app._get_groq_client()
    assert app._groq_client_key == built_with[0] == "gsk_old_key", app._groq_client_key
    app._groq_client = None
    print("PASS  the cached Groq client is labelled with the key it was really built with.")


def test_rejected_key_warns_once_and_stops_trying(app):
    import groq
    _reset_cloud(app)
    err = groq.AuthenticationError("bad key", response=httpx.Response(401, request=_req()), body=None)
    with mock.patch.object(app, "transcribe_cloud", side_effect=err) as cloud:
        assert app.transcribe_audio(np.zeros(1600, dtype=np.float32)) == "local stub said hello"
        app.transcribe_audio(np.zeros(1600, dtype=np.float32))
    assert cloud.call_count == 1, "a rejected key must not be retried every dictation"
    assert app.cloud_available() is False
    app.GROQ_API_KEY = "gsk_new_key"                 # user fixes it in Settings
    assert app.cloud_available() is True
    titles = [c.args[1] for c in app.tray_icon.notify.call_args_list]
    assert titles.count("Groq API key rejected") == 1, titles
    print("PASS  a rejected key: one notice, local fallback, retried only after a new key.")


def test_rate_limit_pauses_for_retry_after(app):
    import groq
    _reset_cloud(app)
    resp = httpx.Response(429, request=_req(), headers={"retry-after": "120"})
    err = groq.RateLimitError("slow down", response=resp, body=None)
    with mock.patch.object(app, "transcribe_cloud", side_effect=err):
        app.transcribe_audio(np.zeros(1600, dtype=np.float32))
    remaining = app.cloud_paused_until - app.time.monotonic()
    assert 110 < remaining <= 120, remaining
    msgs = [c.args[0] for c in app.tray_icon.notify.call_args_list]
    assert any("2 minutes" in m for m in msgs), msgs
    print("PASS  a 429 pauses the cloud for Groq's retry-after and says how long.")


def test_network_down_skips_cloud_instantly_for_a_while(app):
    import groq
    _reset_cloud(app)
    err = groq.APIConnectionError(request=_req())
    with mock.patch.object(app, "transcribe_cloud", side_effect=err) as cloud:
        for _ in range(3):
            assert app.transcribe_audio(np.zeros(1600, dtype=np.float32)) == "local stub said hello"
    assert cloud.call_count == 1, "no repeated connect timeouts while offline"
    titles = [c.args[1] for c in app.tray_icon.notify.call_args_list]
    assert titles == ["Cloud transcription unavailable"], titles
    print("PASS  offline: one notice, then 30 s of instant local fallback.")


def test_no_backend_raises_transcription_failed(app):
    import groq
    _reset_cloud(app)
    with mock.patch.object(app, "_local_model", return_value=None),          mock.patch.object(app, "transcribe_cloud",
                           side_effect=groq.APIConnectionError(request=_req())):
        try:
            app.transcribe_audio(np.zeros(1600, dtype=np.float32))
        except app.TranscriptionFailed as exc:
            assert "local model" in str(exc)
        else:
            raise AssertionError("expected TranscriptionFailed")
    print("PASS  no backend available -> TranscriptionFailed, not a silent empty result.")


def test_process_audio_reports_failed_transcription(app):
    _reset_cloud(app)
    with mock.patch.object(app, "_use_speculative_or_transcribe",
                           side_effect=app.TranscriptionFailed("No connection.")), \
         mock.patch.object(app, "deliver") as deliver:
        app.process_audio(np.zeros(16000, dtype=np.float32), None)
    assert not deliver.called
    app.tray_icon.notify.assert_called_with("No connection. Nothing was typed.",
                                            "Couldn't transcribe that")
    print("PASS  a failed transcription is reported; nothing is typed.")


def test_other_groq_errors_are_reported(app):
    import groq
    _reset_cloud(app)
    err = groq.NotFoundError("model `whisper-large-v9` does not exist",
                             response=httpx.Response(404, request=_req()), body=None)
    with mock.patch.object(app, "transcribe_cloud", side_effect=err):
        assert app.transcribe_audio(np.zeros(1600, dtype=np.float32)) == "local stub said hello"
    title, msg = app.tray_icon.notify.call_args.args[1], app.tray_icon.notify.call_args.args[0]
    assert title == "Groq couldn't transcribe that" and "404" in msg and "model" in msg, (title, msg)
    print("PASS  a retired/mistyped model (404) is reported instead of silently going local.")


def test_missing_groq_package_still_falls_back_to_local(app):
    _reset_cloud(app)
    with mock.patch.object(app, "transcribe_cloud", side_effect=ImportError("No module named 'groq'")), \
         mock.patch.dict(sys.modules, {"groq": None}):
        assert app.transcribe_audio(np.zeros(1600, dtype=np.float32)) == "local stub said hello"
    print("PASS  a broken groq install never costs the dictation - local takes over.")


def test_cloud_only_mode_keeps_trying_during_a_pause(app):
    _reset_cloud(app)
    app.cloud_paused_until = app.time.monotonic() + 30
    with mock.patch.object(app, "_local_model", return_value=None),          mock.patch.object(app, "transcribe_cloud", return_value="cloud text"):
        assert app.transcribe_audio(np.zeros(1600, dtype=np.float32)) == "cloud text"
    print("PASS  with no local model, a pause never blocks the only backend.")

if __name__ == "__main__":
    test_groq_imports()
    app = import_app_with_mocks()
    test_wav_roundtrip(app)
    test_wav_clipping(app)
    test_config_roundtrip(app)
    test_transcribe_cloud_no_key(app)
    test_dispatch_local_when_cloud_off(app)
    test_dispatch_falls_back_on_cloud_error(app)
    test_dispatch_skips_cloud_when_key_missing(app)
    test_client_rebuilds_on_key_change(app)
    speech = tts_speech(app)
    test_trim_silence_drops_pure_noise(app)
    test_trim_silence_keeps_speech(app, speech)
    test_transcribe_cloud_skips_groq_on_silence(app)
    test_transcribe_cloud_uploads_trimmed_audio(app, speech)
    test_client_has_bounded_timeout_and_no_retries(app)
    test_the_client_is_labelled_with_the_key_it_was_built_with(app)
    test_rejected_key_warns_once_and_stops_trying(app)
    test_rate_limit_pauses_for_retry_after(app)
    test_network_down_skips_cloud_instantly_for_a_while(app)
    test_no_backend_raises_transcription_failed(app)
    test_process_audio_reports_failed_transcription(app)
    test_other_groq_errors_are_reported(app)
    test_missing_groq_package_still_falls_back_to_local(app)
    test_cloud_only_mode_keeps_trying_during_a_pause(app)
    print("\nAll cloud-transcription tests passed.")
