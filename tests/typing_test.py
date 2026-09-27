"""
PIECE (d): Auto-typing test.

Goal: prove we can make text appear in ANY app by simulating keystrokes -
as if an invisible person were typing on your keyboard. This is how the
final app gets transcribed text into Word, Slack, your browser, etc.

This script does NOT listen or transcribe. It just waits a few seconds for
you to click into a text box, then types a fixed sentence into it.
"""

import time
from pynput.keyboard import Controller

# Controller is the "virtual keyboard" - it can press keys programmatically.
keyboard = Controller()

# The sentence we'll type as a test.
TEXT_TO_TYPE = "Hello from my voice dictation app - this text was typed automatically."

# Seconds to wait so you can click into the target window.
COUNTDOWN = 5

print("Auto-typing test.")
print(f"Quick! You have {COUNTDOWN} seconds to click into Notepad")
print("(or any text box - browser address bar, a Word doc, etc.)\n")

for i in range(COUNTDOWN, 0, -1):
    print(f"  typing in {i}...")
    time.sleep(1)

print("\nTYPING NOW...")
# .type() sends the whole string one character at a time, into whatever
# window currently has keyboard focus.
keyboard.type(TEXT_TO_TYPE)
print("Done. Check the window you clicked into - the sentence should be there.")
