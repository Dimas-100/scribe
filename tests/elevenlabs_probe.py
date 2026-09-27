r"""
Live probe: stream a spoken sentence to the REAL ElevenLabs service, the way
a dictation does, and report how fast the text comes back after "release".

Run from the project root:   venv\Scripts\python tests\elevenlabs_probe.py
                             venv\Scripts\python tests\elevenlabs_probe.py --usage

NOT part of the safe suite: it uses your ElevenLabs key (from Windows
Credential Manager, "Scribe/elevenlabs") and a few seconds of your plan's
time. The sentence is made on the fly with Windows' built-in voice, so the
repo needs no audio file.

--usage also measures how many credits an hour of realtime transcription
uses (for elevenlabs_stream.CREDITS_PER_REALTIME_HOUR): it reads the
account's credits, streams about a minute of speech, and reads them again.
That needs the key to be allowed to read account info (User -> Read).
"""

import os
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import elevenlabs_stream as es  # noqa: E402
import keystore                 # noqa: E402

SENTENCE = ("Remind Kalshi to send the Vercel invoice by Friday, "
            "and ask Jose whether the deploy went out.")
CHUNK = 640          # 40 ms at 16 kHz - about what the mic hands Scribe


def spoken(text):
    """`text` spoken by Windows' built-in voice, as 16 kHz float32 audio."""
    path = os.path.join(tempfile.gettempdir(), "scribe_elevenlabs_probe.wav")
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
          "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
          " [System.Speech.AudioFormat.AudioChannel]::Mono);"
          f"$s.SetOutputToWaveFile('{path}', $f);"
          f"$s.Speak('{text}');"
          "$s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    with wave.open(path, "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768


def stream_like_a_dictation(key, audio, terms=()):
    """Feed `audio` in real time, as the mic would, then 'release'."""
    session = es.Session(key, terms).start()
    started = time.monotonic()
    for i in range(0, len(audio), CHUNK):
        session.feed(audio[i:i + CHUNK].reshape(-1, 1))
        # Real-time pacing: never run ahead of the clock.
        ahead = (i + CHUNK) / 16000 - (time.monotonic() - started)
        if ahead > 0:
            time.sleep(ahead)
    text = session.finish()
    return session, text


def main():
    key = keystore.get_key("elevenlabs")
    if not key:
        sys.exit("No ElevenLabs key in Credential Manager (Scribe/elevenlabs). "
                 "Add it in Scribe's Settings first.")
    print(f"Key: {keystore.key_hint(key)}")
    silence = np.zeros(8000, dtype=np.float32)            # 0.5 s, like a real take
    audio = np.concatenate([silence, spoken(SENTENCE), silence])
    print(f"Streaming {len(audio) / 16000:.1f} s of speech in real time...")
    session, text = stream_like_a_dictation(key, audio, es.keyterms(["Kalshi", "Vercel"]))
    print(f"Text:        {text!r}")
    print(f"Connected in {session.connect_ms:.0f} ms")
    print(f"Release -> text: {session.final_ms:.0f} ms   (Wispr-level target: under 700)")

    if "--usage" in sys.argv[1:]:
        before = es.get_usage(key)
        if before["status"] != "ok":
            sys.exit(f"Can't read account usage ({before['status']}) - allow the key "
                     f"User -> Read to measure credits per hour.")
        long_take = np.concatenate([audio] * max(1, int(60 * 16000 / len(audio))))
        print(f"Streaming {len(long_take) / 16000:.0f} s for the credit measurement...")
        session, _ = stream_like_a_dictation(key, long_take)
        time.sleep(5)                                       # let the account catch up
        after = es.get_usage(key)
        used = after["used"] - before["used"]
        per_hour = used * 3600 / session.streamed_seconds
        print(f"Credits used: {used} for {session.streamed_seconds:.0f} s "
              f"-> about {per_hour:.0f} credits per hour")


if __name__ == "__main__":
    main()
