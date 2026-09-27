"""
PIECE (a): Microphone capture test.

Goal of this throwaway script: prove your microphone can record audio into
memory in the EXACT format faster-whisper will need later. If this works,
the whole "audio input" half of the app is trustworthy.

We record 5 seconds, report how loud it was, then play it back so you can
hear it with your own ears.
"""

import sounddevice as sd
import numpy as np

# --- Audio settings (chosen to match what Whisper expects later) ---
SAMPLE_RATE = 16000   # 16 kHz. Whisper was trained on 16 kHz audio, so we
                      # record at 16 kHz from the start and avoid converting.
CHANNELS = 1          # Mono = one channel. Whisper wants mono, not stereo.
DURATION = 5          # How many seconds to record for this test.

# Show every audio device Windows sees. The one marked with ">" under
# "input" is your default microphone.
print("=== Audio devices on this system ===")
print(sd.query_devices())
print()

input(f"Press Enter, then speak for {DURATION} seconds...")

print("RECORDING... speak now!")
# sd.rec() returns a NumPy array of shape (samples, channels).
# dtype="float32" means each sample is a number roughly between -1.0 and 1.0.
audio = sd.rec(
    int(DURATION * SAMPLE_RATE),  # total number of samples = seconds * rate
    samplerate=SAMPLE_RATE,
    channels=CHANNELS,
    dtype="float32",
)
sd.wait()  # block here until the 5-second recording is fully captured
print("Recording finished.")

# Flatten the (samples, 1) array into a plain 1-D array for analysis.
audio = audio.flatten()

# Peak amplitude tells us if the mic actually picked up sound.
peak = np.max(np.abs(audio))
print(f"\nPeak amplitude: {peak:.4f}   (0.0 = pure silence, ~1.0 = very loud)")

if peak < 0.01:
    print("WARNING: almost no sound detected. Mic may be muted or wrong device.")
else:
    print("Good - sound was detected.")

print("\nPlaying your recording back...")
sd.play(audio, samplerate=SAMPLE_RATE)
sd.wait()
print("Done.")
