"""
PIECE (c): Global hotkey listener test.

Goal: prove we can detect you holding Ctrl + Win down and then releasing it,
no matter which app is focused. "Global" means the listener sees the keys
even though this script's window is in the background.

This test does NOT record anything yet - it just prints when the hotkey
goes DOWN and UP, so we can confirm the detection logic is solid.
"""

from pynput import keyboard

# === The ONE place to change the hotkey ===
# Every key in this set must be held down together to trigger.
# Key.ctrl = Control key, Key.cmd = the Windows key.
HOTKEY = {keyboard.Key.ctrl, keyboard.Key.cmd}

# Set of keys currently held down (after normalizing - see below).
pressed = set()
# Are we currently "inside" a hotkey hold? Prevents repeat triggers.
active = False


def normalize(key):
    """
    Windows sends LEFT and RIGHT versions of Ctrl/Win as different keys
    (ctrl_l vs ctrl_r). We collapse them into one generic key so it doesn't
    matter which side of the keyboard you use.
    """
    if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
        return keyboard.Key.ctrl
    if key in (keyboard.Key.cmd_l, keyboard.Key.cmd_r):
        return keyboard.Key.cmd
    return key


def on_press(key):
    """Called every time ANY key is pressed, anywhere in Windows."""
    global active
    pressed.add(normalize(key))
    # Trigger only on the first moment ALL hotkey keys are held.
    # (Holding a key makes Windows fire on_press repeatedly - 'active'
    #  guards against starting over and over.)
    if not active and HOTKEY.issubset(pressed):
        active = True
        print(">>> HOTKEY DOWN  - (recording would START here)")


def on_release(key):
    """Called every time ANY key is released."""
    global active
    k = normalize(key)
    # If we were holding the hotkey and one of its keys let go, it's over.
    if active and k in HOTKEY:
        active = False
        print("<<< HOTKEY UP    - (recording would STOP here)")
    pressed.discard(k)
    # Esc quits this test.
    if key == keyboard.Key.esc:
        print("Esc pressed - exiting test.")
        return False  # returning False stops the listener


print("Hotkey test running.")
print("Hold Ctrl + Win together, then release. Try it a few times.")
print("You can also click into another window first - it still works.")
print("Press Esc to quit.\n")

# The listener runs on its own thread; .join() keeps the script alive.
with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
    listener.join()
