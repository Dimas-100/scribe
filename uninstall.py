"""
=============================================================================
 SCRIBE UNINSTALL - remove Scribe from this PC (run uninstall.bat).
=============================================================================

 Always removed (they only point at this folder):
   - "Start Scribe when I sign in" - the Run entry, if it starts THIS copy
   - the Start-menu shortcut and a taskbar pin that start this copy

 Removed only if you say yes:
   - your data: dictation history, settings, Dictionary (%APPDATA%\\Scribe)
   - your API keys, saved in Windows Credential Manager
   - the downloaded speech models (they can be hundreds of MB; other apps
     built on the same faster-whisper models share them)

 Then delete this folder yourself - Windows won't let a program delete the
 folder it is running from. What's left is pip's download cache
 (%LOCALAPPDATA%\\pip\\Cache), shared by every Python program on the PC.
=============================================================================
"""

import os
import shutil
import subprocess
import sys

import autostart
import install_shortcut
import keystore
import storage
import updates

# Files that show a folder really is Scribe's data folder (anything else
# is never deleted - the data folder can be moved with SCRIBE_DATA_DIR).
DATA_MARKERS = ("config.json", "dictation_log.jsonl", "vocabulary.json")
MODEL_NAMES = storage.MODEL_CHOICES


def remove_shortcuts(folders=(install_shortcut.START_MENU_PROGRAMS,
                              install_shortcut.PINNED_TASKBAR)):
    """Delete every .lnk in `folders` that starts this copy's app.py.
    Returns the paths removed."""
    removed = []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not name.lower().endswith(".lnk"):
                continue
            try:
                if install_shortcut.launches_this_scribe(path):
                    os.remove(path)
                    removed.append(path)
            except Exception as exc:
                print(f"  (couldn't check {name}: {exc})")
    return removed


def looks_like_data_folder(path):
    """Only a real Scribe data folder may be deleted: it exists, holds one of
    Scribe's own files, and isn't a drive, your home folder, or this folder."""
    if not path or not os.path.isdir(path):
        return False
    full = os.path.normcase(os.path.abspath(path))
    never = {os.path.normcase(os.path.abspath(p)) for p in (
        os.path.expanduser("~"), os.environ.get("APPDATA", "~"), storage.APP_DIR)}
    if full in never or os.path.dirname(full) == full:
        return False
    return any(os.path.exists(os.path.join(path, m)) for m in DATA_MARKERS)


def model_folders():
    """The downloaded speech-model folders Scribe can use, where they exist."""
    try:
        import faster_whisper.utils
        from huggingface_hub import constants
    except ImportError:
        return []
    out = []
    for name in MODEL_NAMES:
        repo = faster_whisper.utils._MODELS.get(name)
        if repo:
            path = os.path.join(constants.HF_HUB_CACHE, "models--" + repo.replace("/", "--"))
            if os.path.isdir(path):
                out.append(path)
    return out


def folder_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1e6


def ask(question):
    """True only for an explicit yes."""
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main():
    print("Uninstalling Scribe.\n")
    if not updates.stop_scribe():
        print("Scribe is still running. Quit it from its tray icon (right-click -> Quit),")
        print("then run uninstall.bat again.")
        return 1

    if autostart.available():
        try:
            autostart.set_enabled(False)      # only ever removes THIS copy's entry
            print("- Scribe no longer starts when you sign in.")
        except storage.StorageError as exc:
            print(f"- Couldn't remove the sign-in entry: {exc}")
    for path in remove_shortcuts():
        print(f"- Removed the shortcut {path}")

    print()
    data = storage.DATA_DIR
    if looks_like_data_folder(data) and ask(
            f"Delete your dictation history, settings and Dictionary ({data})?"):
        shutil.rmtree(data, ignore_errors=True)
        print("- Deleted your data." if not os.path.exists(data)
              else "- Some of your data couldn't be deleted (a file is in use).")
    if ask("Delete your saved API keys (Groq, ElevenLabs) from Windows Credential Manager?"):
        for service in keystore.SERVICES:
            keystore.delete_key(service)
        print("- Deleted your API keys.")
    models = model_folders()
    if models:
        size = sum(folder_mb(p) for p in models)
        if ask(f"Delete the downloaded speech models ({size:,.0f} MB)? "
               "Other apps that use the same models would download them again."):
            for path in models:
                shutil.rmtree(path, ignore_errors=True)
            print("- Deleted the speech models.")

    print()
    print("Scribe is uninstalled. To finish, close this window and delete this folder:")
    print(f"    {storage.APP_DIR}")
    print("(If Scribe is still pinned to the taskbar, right-click it -> Unpin.)")
    try:                                      # show it, selected, in Explorer
        subprocess.Popen(["explorer", "/select,", storage.APP_DIR])
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
