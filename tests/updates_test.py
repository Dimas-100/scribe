r"""
Standalone probe for updates.py - "is there a newer Scribe?" and installing
it - plus app.py's daily check and its "quit" command.

Run from the project root:   venv\Scripts\python tests\updates_test.py

Safe: GitHub is never contacted (a fake answer, or a tiny server on
127.0.0.1), files are only written into temp folders, and app.py is
imported through tests/harness.py.
"""

import http.server
import io
import os
import socket
import sys
import tempfile
import threading
import urllib.error
import zipfile
from unittest import mock

# A throwaway data folder BEFORE any Scribe module is imported: updates.py
# imports instance.py, whose mutex and pipe names come from the data folder -
# so nothing here can reach (or ask to quit) the Scribe you're using.
os.environ["SCRIBE_DATA_DIR"] = tempfile.mkdtemp(prefix="scribe-upd-data-")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import ROOT, import_app_with_mocks  # noqa: E402

sys.path.insert(0, ROOT)
import updates  # noqa: E402


def _answer(tag, body="Fixes and polish."):
    return lambda url, timeout: {"tag_name": tag, "body": body, "html_url": "https://x"}


def _raise(exc):
    def fetch(url, timeout):
        raise exc
    return fetch


def test_versions():
    assert updates.parse_version("1.2.3") == (1, 2, 3)
    assert updates.parse_version("v1.2") == (1, 2, 0)
    assert updates.parse_version("latest") is None and updates.parse_version("") is None
    assert updates.is_newer("1.0.1", "1.0.0") and updates.is_newer("v1.10.0", "1.9.9")
    assert not updates.is_newer("1.0.0", "1.0.0") and not updates.is_newer("0.9", "1.0.0")
    assert not updates.is_newer("junk", "1.0.0")
    print("PASS  versions compare as numbers (1.10 after 1.9); junk is never newer.")


def test_check():
    r = updates.check("1.0.0", fetch=_answer("v1.1.0"))
    assert r["status"] == "available" and r["latest"] == "1.1.0" and r["current"] == "1.0.0"
    assert r["url"] == updates.RELEASES_PAGE, "a fixed page, never a URL from the answer"
    assert r["notes"] == "Fixes and polish."
    assert updates.check("1.1.0", fetch=_answer("v1.1.0"))["status"] == "current"
    no_release = urllib.error.HTTPError(updates.API_URL, 404, "Not Found", {}, None)
    assert updates.check("1.0.0", fetch=_raise(no_release))["status"] == "current"
    busy = urllib.error.HTTPError(updates.API_URL, 403, "rate limited", {}, None)
    assert updates.check("1.0.0", fetch=_raise(busy))["status"] == "error"
    assert updates.check("1.0.0", fetch=_raise(urllib.error.URLError("down")))["status"] == "offline"
    assert updates.check("1.0.0", fetch=_raise(TimeoutError()))["status"] == "offline"
    assert updates.check("1.0.0", fetch=_raise(ValueError("bad json")))["status"] == "error"
    assert updates.check("1.0.0", fetch=lambda u, t: {"tag_name": "nightly"})["status"] == "error"
    assert updates.check("1.0.0", fetch=lambda u, t: ["not", "a", "dict"])["status"] == "error"
    print("PASS  check() says available / current / offline / error - and never raises.")


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    path = tempfile.mktemp(suffix=".zip")
    with open(path, "wb") as f:
        f.write(buf.getvalue())
    return path


def test_apply_zip_writes_only_scribe_files():
    app_dir = tempfile.mkdtemp(prefix="scribe-upd-")
    os.makedirs(os.path.join(app_dir, "venv"))
    with open(os.path.join(app_dir, "venv", "keep.txt"), "w") as f:
        f.write("mine")
    with open(os.path.join(app_dir, "old_module.py"), "w") as f:
        f.write("old")
    path = _zip({
        "Scribe/app.py": "new app",
        "Scribe/docs/note.txt": "nested",
        "Scribe/venv/keep.txt": "overwritten?",
        "Scribe/../escape.txt": "outside!",
        "Other/app.py": "not ours",
    })
    assert updates.apply_zip(path, app_dir) == 2
    read = lambda *p: open(os.path.join(app_dir, *p)).read()  # noqa: E731
    assert read("app.py") == "new app" and read("docs", "note.txt") == "nested"
    assert read("venv", "keep.txt") == "mine", "the Python environment is never touched"
    assert read("old_module.py") == "old", "files the release dropped are left alone"
    assert not os.path.exists(os.path.join(os.path.dirname(app_dir), "escape.txt"))
    print("PASS  an update writes Scribe's files only - never venv, never outside the folder.")


def test_apply_zip_refuses_a_stranger():
    app_dir = tempfile.mkdtemp(prefix="scribe-upd-")
    for files in ({"Scribe/readme.txt": "no app"}, {"app.py": "no prefix"}):
        try:
            updates.apply_zip(_zip(files), app_dir)
        except updates.UpdateError:
            pass
        else:
            raise AssertionError(f"{files} is not a Scribe release")
    bad = tempfile.mktemp(suffix=".zip")
    with open(bad, "wb") as f:
        f.write(b"not a zip")
    try:
        updates.apply_zip(bad, app_dir)
    except updates.UpdateError:
        pass
    else:
        raise AssertionError("a broken download must be refused")
    assert os.listdir(app_dir) == [], "nothing is written when the download is wrong"
    print("PASS  a download that isn't a Scribe release changes nothing.")


def test_download_from_a_local_server():
    payload = open(_zip({"Scribe/app.py": "x"}), "rb").read()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        path = updates.download(f"http://127.0.0.1:{server.server_port}/Scribe.zip", timeout=5)
        assert open(path, "rb").read() == payload
        os.remove(path)
        with mock.patch.object(updates, "MAX_ZIP_BYTES", 10):
            try:
                updates.download(f"http://127.0.0.1:{server.server_port}/Scribe.zip", timeout=5)
            except updates.UpdateError:
                pass
            else:
                raise AssertionError("an oversized download must be refused")
    finally:
        server.shutdown()
    # A port nobody listens on: the connection is refused at once.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    before = set(os.listdir(tempfile.gettempdir()))
    try:
        updates.download(f"http://127.0.0.1:{closed_port}/nothing.zip", timeout=2)
    except updates.UpdateError as exc:
        assert "internet" in str(exc)
    else:
        raise AssertionError("an unreachable server must fail with a readable reason")
    left = [n for n in set(os.listdir(tempfile.gettempdir())) - before if n.startswith("scribe-update-")]
    assert not left, f"a failed download leaves nothing behind: {left}"
    print("PASS  the download is saved whole, capped in size, and fails readably.")


def test_install_flow():
    app_dir = tempfile.mkdtemp(prefix="scribe-upd-")
    zip_path = _zip({"Scribe/app.py": "new", "Scribe/setup.bat": "@echo off"})
    order = []
    with mock.patch.object(updates, "stop_scribe", side_effect=lambda: order.append("stop") or True), \
         mock.patch.object(updates, "download", side_effect=lambda: order.append("download") or zip_path), \
         mock.patch.object(updates.subprocess, "call", return_value=0) as call:
        assert updates.install(app_dir) == 0
    assert order == ["download", "stop"], "the new version is in hand before Scribe is asked to quit"
    assert open(os.path.join(app_dir, "app.py")).read() == "new"
    assert not os.path.exists(zip_path), "the download is cleaned up"
    assert not [n for n in os.listdir(app_dir) if n.endswith(".update-new")]
    assert call.call_args.args[0] == ["cmd", "/c", "call", os.path.join(app_dir, "setup.bat")]
    # A git clone updates with git; a Scribe that won't quit stops everything.
    os.makedirs(os.path.join(app_dir, ".git"))
    with mock.patch.object(updates, "stop_scribe", return_value=True), \
         mock.patch.object(updates.shutil, "which", return_value="git"), \
         mock.patch.object(updates.subprocess, "call", return_value=0) as call:
        assert updates.install(app_dir) == 0
    assert call.call_args_list[0].args[0][:4] == ["git", "-C", app_dir, "pull"]
    with mock.patch.object(updates, "stop_scribe", return_value=False), \
         mock.patch.object(updates, "_pause"), \
         mock.patch.object(updates.subprocess, "call") as call:
        assert updates.install(app_dir) == 1
    assert not call.called, "nothing runs while Scribe is still running"
    print("PASS  install: download + check, then quit Scribe, new files (or git pull), setup.bat.")


def test_a_failed_update_leaves_scribe_running():
    app_dir = tempfile.mkdtemp(prefix="scribe-upd-")
    with open(os.path.join(app_dir, "app.py"), "w") as f:
        f.write("old")
    # A download that fails: Scribe is never even asked to quit.
    with mock.patch.object(updates, "download", side_effect=updates.UpdateError("offline")), \
         mock.patch.object(updates, "stop_scribe") as stop, \
         mock.patch.object(updates, "_pause"):
        assert updates.install(app_dir) == 1
    assert not stop.called
    # A damaged download (bad checksum) is caught while staging - same.
    good = _zip({"Scribe/app.py": "new" * 200})
    data = bytearray(open(good, "rb").read())
    at = data.find(b"newnew")
    data[at:at + 6] = b"XXXXXX"                    # the stored bytes no longer match
    damaged = tempfile.mktemp(suffix=".zip")
    open(damaged, "wb").write(bytes(data))
    with mock.patch.object(updates, "download", return_value=damaged), \
         mock.patch.object(updates, "stop_scribe") as stop, \
         mock.patch.object(updates, "_pause"):
        assert updates.install(app_dir) == 1
    assert not stop.called and open(os.path.join(app_dir, "app.py")).read() == "old"
    # A file that stays locked while swapping in: every file already swapped
    # is put back, and Scribe is started again.
    with open(os.path.join(app_dir, "dashboard.py"), "w") as f:
        f.write("old")
    zip_path = _zip({"Scribe/app.py": "new", "Scribe/dashboard.py": "new", "Scribe/extra.py": "new"})
    real_replace = os.replace

    def locked_dashboard(src, dst):
        if os.path.basename(dst) == "dashboard.py" and src.endswith(".update-new"):
            raise PermissionError(13, "in use")           # the old file is held open
        return real_replace(src, dst)
    with mock.patch.object(updates, "download", return_value=zip_path), \
         mock.patch.object(updates.instance, "is_running", return_value=True), \
         mock.patch.object(updates, "stop_scribe", return_value=True), \
         mock.patch.object(updates.os, "replace", side_effect=locked_dashboard), \
         mock.patch.object(updates.time, "sleep"), \
         mock.patch.object(updates, "start_scribe", return_value=True) as start, \
         mock.patch.object(updates, "_pause"), \
         mock.patch.object(updates.subprocess, "call") as call:
        assert updates.install(app_dir) == 1
    assert start.called, "never leave the user without Scribe"
    assert not call.called, "setup.bat doesn't run after a failed update"
    for name in ("app.py", "dashboard.py"):
        assert open(os.path.join(app_dir, name)).read() == "old", f"{name} is put back"
    assert not os.path.exists(os.path.join(app_dir, "extra.py")), "nothing half-added"
    assert not [n for n in os.listdir(app_dir) if ".update-" in n], os.listdir(app_dir)
    print("PASS  a failed update puts every file back, says so, and restarts Scribe.")


def test_app_checks_daily_and_says_so_once(app):
    app._notice_last.clear()
    app.tray_icon = mock.MagicMock()
    app._tray_ready = True
    app.CHECK_UPDATES, app.UPDATE_NOTIFIED = True, ""
    available = {"status": "available", "current": "1.0.0", "latest": "1.1.0"}
    with mock.patch.object(app.updates, "check", return_value=available) as check:
        app._check_for_update()
        app._check_for_update()
    assert check.call_count == 2
    titles = [c.args[1] for c in app.tray_icon.notify.call_args_list]
    assert titles == ["Scribe 1.1.0 is available"], titles
    assert app.storage.load_config()[0]["update_notified"] == "1.1.0", "remembered across restarts"
    app.CHECK_UPDATES = False
    with mock.patch.object(app.updates, "check") as check:
        assert app._check_for_update() is None and not check.called
    app.CHECK_UPDATES = True
    # Not while the welcome is open: nothing may be saved before setup is done.
    app.setup_pending, app.UPDATE_NOTIFIED = True, ""
    with mock.patch.object(app.updates, "check", return_value=dict(available, latest="1.2.0")) as check, \
         mock.patch.object(app.storage, "save_config_changes") as save:
        assert app._check_for_update() is None
    assert not check.called and not save.called
    app.setup_pending = False
    print("PASS  the app tells you about a new version once, and not at all when switched off.")


def test_quit_command(app):
    while not app.ui_queue.empty():
        app.ui_queue.get_nowait()
    assert app._handle_control({"cmd": "quit"}) is None
    assert app.ui_queue.get_nowait() == "quit"
    print("PASS  the updater's 'quit' reaches the main thread's queue.")


if __name__ == "__main__":
    test_versions()
    test_check()
    test_apply_zip_writes_only_scribe_files()
    test_apply_zip_refuses_a_stranger()
    test_download_from_a_local_server()
    test_install_flow()
    test_a_failed_update_leaves_scribe_running()
    app = import_app_with_mocks()
    test_app_checks_daily_and_says_so_once(app)
    test_quit_command(app)
    print("\nAll updates tests passed.")
