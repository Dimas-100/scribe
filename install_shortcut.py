r"""
=============================================================================
 SCRIBE - Start Menu shortcut installer (Windows).
=============================================================================

 WHY THIS EXISTS:
   When Windows pins a running app to the taskbar, it looks for a Start
   Menu shortcut whose AppUserModelID (AUMID) matches the running window's
   AUMID. If there is no match, Windows falls back to the launcher
   executable's icon - which for Scribe means pythonw.exe -> Python logo.

   This script creates that missing shortcut, with the Scribe icon set
   AND the AUMID stamped to match the one app.py registers at startup
   ("Scribe.VoiceDictation"). With the shortcut in place, pinning shows
   the Scribe icon and groups the running window with the shortcut.

 HOW TO USE:
   setup.bat runs it for you. By hand:
       venv\Scripts\python.exe install_shortcut.py

   Then launch Scribe from the new "Scribe" entry in the Start menu, and
   right-click its taskbar button -> Pin to taskbar. The pinned icon will
   stay correct across restarts.

 WHAT IT CREATES:
   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Scribe.lnk

   - Target:        venv\Scripts\pythonw.exe (or .venv\Scripts\pythonw.exe)
   - Arguments:     "app.py" --show  (--show: a click opens the dashboard)
   - WorkingDir:    the project folder
   - IconLocation:  scribe.ico
   - AppUserModelID: Scribe.VoiceDictation   (this is the magic key)

   It also re-points an existing pinned taskbar shortcut that launches this
   Scribe, so a pin made by an older version gets --show too.
=============================================================================
"""

import os
import sys

# pywin32 - bundles the COM and Shell helpers we need to set the AUMID
# property on a .lnk file. If you ran 'pip install -r requirements.txt'
# this is already installed.
try:
    import pythoncom
    from win32com.client import Dispatch
    from win32com.propsys import propsys, pscon
except ImportError:
    print("ERROR: pywin32 is not installed in this Python environment.")
    print("       Run:  pip install pywin32")
    sys.exit(1)


# -----------------------------------------------------------------------------
#  Paths - all derived from where THIS file lives, so a move/rename of the
#  project folder needs only one re-run of this script to fix everything.
# -----------------------------------------------------------------------------

APP_DIR   = os.path.dirname(os.path.abspath(__file__))
APP_PY    = os.path.join(APP_DIR, "app.py")
ICON_FILE = os.path.join(APP_DIR, "scribe.ico")

# "--show": a click on the shortcut means "show me Scribe" - its dashboard
# opens (and a running Scribe brings its window up). Sign-in starts pass no
# flag, so Scribe starts quietly in the tray.
ARGUMENTS = f'"{APP_PY}" --show'

# Must match the AUMID set in app.py's main() via
# SetCurrentProcessExplicitAppUserModelID. Windows uses this exact string
# to link the pinned shortcut to the running window.
APP_USER_MODEL_ID = "Scribe.VoiceDictation"

START_MENU_PROGRAMS = os.path.join(
    os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs",
)
SHORTCUT_PATH = os.path.join(START_MENU_PROGRAMS, "Scribe.lnk")
PINNED_TASKBAR = os.path.join(
    os.environ["APPDATA"], "Microsoft", "Internet Explorer", "Quick Launch",
    "User Pinned", "TaskBar",
)


def find_pythonw():
    """venv\\ (what setup.bat and the README create) or .venv\\ (many
    editors' default). None if neither exists."""
    for folder in ("venv", ".venv"):
        path = os.path.join(APP_DIR, folder, "Scripts", "pythonw.exe")
        if os.path.exists(path):
            return path
    return None


def write_shortcut(path, pythonw):
    """Create or overwrite the .lnk at `path`: target, arguments, icon, AUMID."""
    # --- Step 1: write the .lnk via WScript.Shell ---------------------
    # Old, well-supported COM API; works on every Windows since forever.
    shell = Dispatch("WScript.Shell")
    lnk = shell.CreateShortcut(path)
    lnk.TargetPath       = pythonw
    lnk.Arguments        = ARGUMENTS
    lnk.WorkingDirectory = APP_DIR
    # ',0' picks the first icon in the .ico file. The .ico bundles multiple
    # sizes; Windows scales to the slot it needs (16, 32, 48, 256 px).
    lnk.IconLocation     = f"{ICON_FILE},0"
    lnk.Description      = "Scribe - voice dictation"
    lnk.Save()

    # --- Step 2: stamp the AUMID via the Property Store -------------------
    # The .lnk file format stores properties under named keys via the
    # Property Store API. WScript.Shell can't reach those keys; pywin32's
    # propsys can. PKEY_AppUserModel_ID is the well-known key Windows
    # reads when deciding which shortcut a running window belongs to.
    #
    # GPS_READWRITE (= 2) is required - the default GPS_DEFAULT (0) opens
    # the store read-only and SetValue would fail with "Access Denied".
    GPS_READWRITE = 2
    store = propsys.SHGetPropertyStoreFromParsingName(path, None, GPS_READWRITE)
    pv = propsys.PROPVARIANTType(APP_USER_MODEL_ID, pythoncom.VT_LPWSTR)
    store.SetValue(pscon.PKEY_AppUserModel_ID, pv)
    # Commit flushes the change back to the .lnk on disk. Without this,
    # the property would only live in our in-memory copy.
    store.Commit()


def launches_this_scribe(path):
    """True if an existing shortcut runs THIS folder's app.py."""
    lnk = Dispatch("WScript.Shell").CreateShortcut(path)
    return os.path.normcase(APP_PY) in os.path.normcase(lnk.Arguments or "")


def main():
    # A portable copy (its own data folder, SCRIBE_DATA_DIR - also how a test
    # install runs) must never take over the Start-menu "Scribe" entry or a
    # taskbar pin that belong to the Scribe you normally use.
    if os.environ.get("SCRIBE_DATA_DIR"):
        print("A portable copy (SCRIBE_DATA_DIR is set): the Start menu is left alone.")
        return
    pythonw = find_pythonw()
    if pythonw is None:
        print("ERROR: no virtual environment found (venv\\ or .venv\\).")
        print("       Run setup.bat first.")
        sys.exit(1)
    # Sanity-check that every path we'll reference exists. A wrong path
    # would silently produce a broken shortcut.
    for path, label in ((APP_PY, "app.py"), (ICON_FILE, "scribe.ico")):
        if not os.path.exists(path):
            print(f"ERROR: missing {label} at {path}")
            sys.exit(1)

    write_shortcut(SHORTCUT_PATH, pythonw)

    # A taskbar pin is a separate .lnk copy - re-point it too, or it keeps
    # launching the old way (without --show, or into an old venv).
    repinned = []
    if os.path.isdir(PINNED_TASKBAR):
        for name in os.listdir(PINNED_TASKBAR):
            path = os.path.join(PINNED_TASKBAR, name)
            if not name.lower().endswith(".lnk"):
                continue
            try:
                if launches_this_scribe(path):
                    write_shortcut(path, pythonw)
                    repinned.append(path)
            except Exception as exc:
                print(f"  (couldn't check {name}: {exc})")

    print("Done.")
    print()
    print(f"  Shortcut : {SHORTCUT_PATH}")
    print(f"  Target   : {pythonw} {ARGUMENTS}")
    print(f"  Icon     : {ICON_FILE}")
    print(f"  AUMID    : {APP_USER_MODEL_ID}")
    for path in repinned:
        print(f"  Taskbar  : {path} (updated)")
    print()
    print("To start Scribe when you sign in, turn on 'Start Scribe when I sign in'")
    print("in the welcome or in Settings. (An old shortcut you copied into")
    print("shell:startup still works - it starts Scribe quietly - but you can")
    print("delete it once the setting is on.)")


if __name__ == "__main__":
    main()
