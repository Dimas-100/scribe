"""
=============================================================================
 SCRIBE UPDATES - is there a newer Scribe, and installing it.
=============================================================================

 Scribe's releases live on GitHub (REPO). Two jobs:

   check()     -> is there a newer release than this copy? One small request
                  to GitHub's public API - made once a day by the running
                  Scribe (you can switch it off in Settings -> About) and
                  when you press "Check for updates". Nothing about you or
                  your dictations is sent.

   install()   -> bring this copy up to date. Run in a console window, by
                  update.bat or by Settings' "Update now":
                    1. downloads the release's Scribe.zip and unpacks it into
                       a temporary folder - every file read in full, so a
                       broken download stops HERE, with Scribe untouched;
                    2. asks the running Scribe to quit (it must: Windows won't
                       let files a running program uses be replaced);
                    3. swaps the new files in (`git pull` instead, for a git
                       clone);
                    4. runs setup.bat, which installs any new dependencies,
                       refreshes the Start-menu shortcut and starts Scribe.
                  If anything fails after step 2, Scribe is started again
                  and the console says what happened (running update.bat
                  again finishes an update that stopped partway).

 Your data isn't touched: it lives in %APPDATA%\\Scribe, not in this folder.

 Run it yourself:   venv\\Scripts\\python updates.py --install
=============================================================================
"""

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

import instance
from version import VERSION

REPO = "Dimas-100/scribe"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
# The release asset every release carries: this folder's files under "Scribe/".
ZIP_URL = f"https://github.com/{REPO}/releases/latest/download/Scribe.zip"
ZIP_PREFIX = "Scribe/"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 8                  # seconds for the check; the download gets longer
DOWNLOAD_TIMEOUT = 60
MAX_ZIP_BYTES = 50 * 1024 * 1024   # a Scribe release is well under 1 MB
# Folders in this copy that an update never writes into: the Python
# environment, setup's tools, and git's own folder.
KEEP = frozenset({"venv", ".venv", ".tools", ".git"})
QUIT_WAIT = 20               # seconds for a running Scribe to quit when asked


class UpdateError(Exception):
    """The update couldn't be done; the message says why, for the console."""


# =============================================================================
#  IS THERE A NEWER SCRIBE?
# =============================================================================

def parse_version(text):
    """(1, 2, 0) for "1.2.0", "v1.2" or "1.2.0"; None for anything else."""
    m = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", (text or "").strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


def is_newer(latest, current):
    """True when version `latest` comes after `current`."""
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def _get_json(url, timeout):
    """GitHub's answer for `url` as a dict. GitHub asks every API client to
    name itself (User-Agent)."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"Scribe/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(1_000_000).decode("utf-8"))


def check(current=VERSION, timeout=TIMEOUT, fetch=_get_json):
    """
    Compare this copy with the newest release. Returns {"status": ...,
    "current": "1.0.0"} plus, when GitHub answered, "latest", "notes" (the
    release notes, trimmed) and "url" (the release page). status:
      "available" - a newer release is out
      "current"   - this is the newest (or nothing has been released yet)
      "offline"   - GitHub couldn't be reached
      "error"     - GitHub answered, but not with a release we understand
    Never raises. `fetch` is for tests.
    """
    out = {"current": current}
    try:
        data = fetch(API_URL, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:                    # no release published yet
            return dict(out, status="current")
        return dict(out, status="error", message=f"GitHub returned an error ({exc.code}).")
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        # (timeouts, no network, a connection cut off mid-answer)
        return dict(out, status="offline",
                    message="Couldn't reach GitHub — check your internet connection.")
    except ValueError:                         # not JSON
        return dict(out, status="error", message="GitHub's answer couldn't be read.")
    latest = str((data or {}).get("tag_name") or "") if isinstance(data, dict) else ""
    if not parse_version(latest):
        return dict(out, status="error", message="GitHub's answer couldn't be read.")
    out.update(latest=latest.lstrip("v"), url=RELEASES_PAGE,
               notes=str(data.get("body") or "").strip()[:800])
    out["status"] = "available" if is_newer(latest, current) else "current"
    return out


# =============================================================================
#  INSTALLING IT
# =============================================================================

def _say(text=""):
    print(text, flush=True)


def stop_scribe(wait=QUIT_WAIT):
    """Ask a running Scribe to quit and wait until it has. True once no
    Scribe is running. The request is repeated every 2 s: a Scribe that has
    only just started may not be answering its pipe yet."""
    if not instance.is_running():
        return True
    _say("Asking Scribe to quit...")
    deadline = time.monotonic() + wait
    ask_at = 0.0
    while time.monotonic() < deadline:
        if not instance.is_running():
            time.sleep(1.0)                # let the dashboard window close too
            return True
        if time.monotonic() >= ask_at:
            instance.send({"cmd": "quit"})
            ask_at = time.monotonic() + 2.0
        time.sleep(0.3)
    return False


def start_scribe(app_dir=APP_DIR):
    """Start Scribe again - after an update that stopped short, so you're
    never left without it. Best effort: False if it couldn't be started."""
    for folder in ("venv", ".venv"):
        pythonw = os.path.join(app_dir, folder, "Scripts", "pythonw.exe")
        if os.path.exists(pythonw):
            try:
                subprocess.Popen([pythonw, os.path.join(app_dir, "app.py")], cwd=app_dir,
                                 creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
                return True
            except OSError:
                return False
    return False


def _git_pull(app_dir):
    """A git clone updates itself: fast-forward only, so local changes are
    never merged or thrown away - git refuses and says why instead."""
    git = shutil.which("git")
    if not git:
        raise UpdateError("This copy is a git clone, but git isn't installed. "
                          "Install git, or download Scribe again.")
    _say("Getting the new version with git...")
    if subprocess.call([git, "-C", app_dir, "pull", "--ff-only"]) != 0:
        raise UpdateError("git couldn't update this copy (see above). If you changed "
                          "Scribe's files yourself, commit or undo those changes first.")


def download(url=ZIP_URL, timeout=DOWNLOAD_TIMEOUT):
    """The release ZIP, saved to a temporary file; returns its path."""
    _say("Downloading the new version...")
    req = urllib.request.Request(url, headers={"User-Agent": f"Scribe/{VERSION}"})
    fd, path = tempfile.mkstemp(prefix="scribe-update-", suffix=".zip")
    try:
        # The file is wrapped FIRST, so it's closed however the download
        # ends - Windows can't delete a file that is still open.
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=timeout) as resp:
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise UpdateError("The download is far bigger than a Scribe release.")
                out.write(chunk)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        _remove(path)
        raise UpdateError(f"Couldn't download the update ({exc}). "
                          "Check your internet connection and try again.") from None
    except UpdateError:
        _remove(path)
        raise
    return path


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def stage(zip_path):
    """
    Unpack the release into a new temporary folder and return (folder,
    [relative paths]). Every file is read in full - so a damaged download
    (a bad checksum) fails here, before anything in this copy changes. Only
    files under ZIP_PREFIX, never anything for KEEP, and no name that could
    point outside the folder ("../x", "C:/x") is taken.
    """
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise UpdateError("The download isn't a valid ZIP file. Try again.") from None
    folder = tempfile.mkdtemp(prefix="scribe-update-")
    files = []
    try:
        with archive:
            members = [m for m in archive.infolist()
                       if m.filename.startswith(ZIP_PREFIX) and not m.is_dir()]
            if not any(m.filename == ZIP_PREFIX + "app.py" for m in members):
                raise UpdateError("The download doesn't look like Scribe. Nothing was changed.")
            for member in members:
                rel = member.filename[len(ZIP_PREFIX):]
                parts = rel.replace("\\", "/").split("/")
                if (not rel or parts[0] in KEEP or ".." in parts or os.path.isabs(rel)
                        or ":" in rel):
                    continue
                dest = os.path.join(folder, *parts)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with archive.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                files.append(os.path.join(*parts))
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise UpdateError(f"The download is damaged ({exc}). Try again.") from None
    except UpdateError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder, files


class PartialUpdate(UpdateError):
    """The update stopped partway and couldn't be undone: some files are new,
    some old. Running the update again finishes it."""


def _rename(src, dest, tries=6):
    """os.replace, with a few retries for a file something else holds open
    for a moment (OneDrive, antivirus, Explorer's preview)."""
    for attempt in range(tries):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.5)


def install_files(folder, files, app_dir=APP_DIR):
    """
    Swap the staged files into this copy - all of them or none. Each old
    file is first COPIED aside (<name>.update-old) and the new one swapped
    in with a single rename, so every file is always there, old or new; if
    any file can't be swapped, every file already done is put back, and
    UpdateError says nothing changed. (PartialUpdate if even putting back
    failed.) Returns how many files were swapped. Files the new version no
    longer has are left alone - they're harmless, and this folder may hold
    things of yours.
    """
    root = os.path.abspath(app_dir)
    done = []                               # (dest, its old copy or None)
    try:
        for rel in files:
            dest = os.path.abspath(os.path.join(root, rel))
            if not dest.startswith(root + os.sep):   # stage() refused these already
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            new = dest + ".update-new"
            shutil.copyfile(os.path.join(folder, rel), new)
            old = None
            if os.path.exists(dest):
                old = dest + ".update-old"
                try:
                    shutil.copy2(dest, old)    # a copy: the file stays in place
                except OSError:
                    _remove(new)
                    raise
            try:
                _rename(new, dest)            # the one swap - old or new, never missing
            except OSError:
                _remove(new)
                if old:
                    _remove(old)
                raise
            done.append((dest, old))
    except OSError as exc:
        try:
            for dest, old in reversed(done):
                if old:
                    _rename(old, dest)
                else:
                    _remove(dest)
        except OSError:
            raise PartialUpdate(f"{rel} couldn't be replaced ({exc}), and Scribe's "
                                "files couldn't all be put back.") from None
        raise UpdateError(f"{rel} is in use by another program ({exc.strerror or exc}). "
                          "Nothing was changed - close other windows and try again.") from None
    for _dest, old in done:
        if old:
            _remove(old)
    return len(done)


def apply_zip(zip_path, app_dir=APP_DIR):
    """stage() + install_files(): the release's files over this copy.
    Returns the number of files written."""
    folder, files = stage(zip_path)
    try:
        return install_files(folder, files, app_dir)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def install(app_dir=APP_DIR):
    """The whole update, for a console (see the top of this file). Returns
    the exit code: 0 when Scribe was updated and started again."""
    _say(f"Updating Scribe (this is version {VERSION}).")
    _say()
    zip_path = folder = None
    stopped = changing = False
    try:
        is_clone = os.path.isdir(os.path.join(app_dir, ".git"))
        if not is_clone:
            zip_path = download()           # all of it, before Scribe is touched
            folder, files = stage(zip_path)
        was_running = instance.is_running()
        if not stop_scribe():
            raise UpdateError("Scribe is still running. Quit it from its tray icon "
                              "(right-click -> Quit), then run update.bat again.")
        stopped = was_running
        if is_clone:
            _git_pull(app_dir)              # fast-forward or nothing: never half-done
        else:
            changing = True
            _say(f"Updated {install_files(folder, files, app_dir)} files.")
    except Exception as exc:
        _say()
        reason = str(exc) if isinstance(exc, UpdateError) else f"{type(exc).__name__}: {exc}"
        # install_files puts everything back unless it says PartialUpdate; any
        # other surprise while files were being swapped may have left a mix.
        partial = isinstance(exc, PartialUpdate) or (changing and not isinstance(exc, UpdateError))
        if partial:
            _say(f"The update stopped partway: {reason}")
            _say(f"Run update.bat again (in {app_dir}) to finish it.")
        else:
            _say(f"The update didn't finish: {reason}")
            _say("Scribe is unchanged.")
        if stopped and start_scribe(app_dir):
            _say("Scribe has been started again.")
        _pause()
        return 1
    finally:
        if zip_path:
            _remove(zip_path)
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    # The NEW setup.bat: installs any new dependencies, refreshes the Start-
    # menu shortcut and starts Scribe. (Run by cmd, not read by this script,
    # so replacing it above was safe. "call" keeps a folder name with "&" in
    # it from being read as two commands.)
    _say()
    return subprocess.call(["cmd", "/c", "call", os.path.join(app_dir, "setup.bat")], cwd=app_dir)


def _pause():
    try:
        input("Press Enter to close this window.")
    except EOFError:
        pass


if __name__ == "__main__":
    if "--install" in sys.argv:
        sys.exit(install())
    result = check()
    print(json.dumps(result, indent=2))
