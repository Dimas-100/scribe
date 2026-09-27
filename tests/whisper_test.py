"""
PIECE (b): Local Whisper transcription test.

Goal: prove faster-whisper can turn recorded audio into text, fully offline,
on your CPU. This reuses the exact recording approach from piece (a), then
feeds that audio straight into the speech-to-text model.

NOTE: the very first run downloads the model from the internet (one time).
Every run after that is 100% offline - no internet, no API key, no cost.
"""

import sounddevice as sd
from faster_whisper import WhisperModel

# === The ONE place to change the model size ===
# "tiny"  - fastest, least accurate
# "base"  - good balance, great starting point  <-- we use this
# "small" - more accurate, noticeably slower on CPU
MODEL_SIZE = "base"

# Audio settings - identical to piece (a) so the formats line up.
SAMPLE_RATE = 16000
CHANNELS = 1
DURATION = 6  # a bit longer so you can speak a full sentence

# --- Load the model ---
# device="cpu"        -> run on your processor (no GPU needed)
# compute_type="int8" -> a compressed math mode that is fast and light on CPU
print(f"Loading Whisper model '{MODEL_SIZE}' (first run downloads it)...")
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
print("Model loaded.")

# --- Record ---
input(f"\nPress Enter, then speak a full sentence for {DURATION} seconds...")
print("RECORDING... speak now!")
audio = sd.rec(
    int(DURATION * SAMPLE_RATE),
    samplerate=SAMPLE_RATE,
    channels=CHANNELS,
    dtype="float32",
)
sd.wait()
print("Recording finished.")

# faster-whisper accepts a flat 1-D float32 NumPy array directly as audio.
audio = audio.flatten()

# --- Transcribe ---
print("TRANSCRIBING... (this takes a few seconds on CPU)")
# transcribe() returns a generator of "segments" plus some info.
# language="en" skips auto-detection and is a little faster/more reliable.
segments, info = model.transcribe(audio, language="en")

# Join all segments into one string of text.
text = " ".join(segment.text.strip() for segment in segments)

print("\n=== TRANSCRIPTION RESULT ===")
print(text if text else "(no speech detected)")
print("============================")
