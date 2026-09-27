r"""
Standalone probe for model_manager.py - find, download, load and swap the
local speech model on a background thread.

Run from the project root:   venv\Scripts\python tests\model_manager_test.py

Safe: find/download/load are fakes - no Whisper, no network.
"""

import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["SCRIBE_DATA_DIR"] = tempfile.mkdtemp(prefix="scribe-model-test-")
for name in ("storage", "model_manager"):
    sys.modules.pop(name, None)
import model_manager  # noqa: E402


def make(find=None, download=None, load=None):
    changes = []
    m = model_manager.ModelManager(
        find_cached=find or (lambda n: "/cache/" + n),
        download=download or (lambda n, report: "/cache/" + n),
        load=load or (lambda path: ("model", path)),
        on_change=changes.append)
    m.progress_interval = 0            # report every update in tests
    return m, changes


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_cached_model_loads_without_download():
    downloads = []
    m, changes = make(download=lambda n, r: downloads.append(n))
    m.request("small.en")
    assert m.wait_ready(5)
    m.join(5)
    assert m.get() == ("model", "/cache/small.en") and downloads == []
    snap = m.snapshot()
    assert snap["state"] == "ready" and snap["name"] == "small.en" and snap["error"] is None
    assert changes[-1]["state"] == "ready"
    print("PASS  a cached model loads with no download.")


def test_missing_model_downloads_with_progress():
    def download(name, report):
        for done in (0, 50, 100):
            report(done, 100)
        return "/cache/" + name
    m, changes = make(find=lambda n: None, download=download)
    m.request("base.en")
    assert m.wait_ready(5)
    m.join(5)
    states = [c["state"] for c in changes]
    assert "downloading" in states and states[-1] == "ready", states
    assert any(c["state"] == "downloading" and c["done"] == 100 and c["total"] == 100
               for c in changes)
    print("PASS  a missing model is downloaded with byte progress, then loaded.")


def test_progress_never_passes_the_total():
    """Files whose size isn't known up front (a compressed tokenizer.json)
    add bytes to the counter without adding to the total - real downloads
    overshoot by a few MB. The bar must never read past 100%."""
    def download(name, report):
        report(120, 100)
        return "/cache/" + name
    m, changes = make(find=lambda n: None, download=download)
    m.request("small.en")
    assert m.wait_ready(5)
    seen = [(c["done"], c["total"]) for c in changes if c["state"] == "downloading"]
    assert (100, 100) in seen and all(d <= t for d, t in seen if t), seen
    print("PASS  download progress is capped at its total.")


def test_failure_then_retry():
    attempts = []

    def download(name, report):
        attempts.append(name)
        if len(attempts) == 1:
            raise ConnectionError("getaddrinfo failed")
        return "/cache/" + name
    m, _ = make(find=lambda n: None, download=download)
    m.request("small.en")
    assert wait_for(lambda: m.state == "failed")
    snap = m.snapshot()
    assert "internet" in snap["error"] and m.get() is None
    assert m.wait_ready(0.1) is False
    m.retry()
    assert m.wait_ready(5) and len(attempts) == 2
    print("PASS  a failed download explains itself; retry succeeds.")


def test_newer_request_supersedes():
    gate = threading.Event()

    def load(path):
        if path.endswith("small.en"):
            gate.wait(5)
        return ("model", path)
    m, _ = make(load=load)
    m.request("small.en")
    time.sleep(0.05)
    m.request("medium.en")
    gate.set()
    assert m.wait_ready(5)
    m.join(5)
    assert m.get() == ("model", "/cache/medium.en")
    print("PASS  a newer request wins; the superseded model is dropped.")


def test_superseded_download_is_abandoned():
    """Switching models mid-download stops the old download at its next
    chunk: its bytes are never shown under the new name, and it is never
    loaded (seconds of CPU and up to a GB of RAM for nothing)."""
    started, gate = threading.Event(), threading.Event()
    loads, chunks_after_switch = [], []

    def download(name, report):
        if name == "medium.en":
            report(10, 1000)
            started.set()
            gate.wait(5)
            report(20, 1000)               # the next chunk: must stop here
            chunks_after_switch.append(name)
        else:
            report(50, 50)
        return "/cache/" + name

    def load(path):
        loads.append(path)
        return ("model", path)
    m, _ = make(find=lambda n: None, download=download, load=load)
    m.request("medium.en")
    assert started.wait(5)
    m.request("base.en")
    snap = m.snapshot()
    assert snap["name"] == "base.en" and snap["done"] == 0, \
        f"medium.en's bytes shown under base.en: {snap}"
    gate.set()
    assert m.wait_ready(5)
    m.join(5)
    assert chunks_after_switch == [], "the superseded download kept going"
    assert loads == ["/cache/base.en"], f"the superseded model was loaded: {loads}"
    print("PASS  switching models mid-download abandons the old download.")


def test_disable_stops_a_download():
    started, gate = threading.Event(), threading.Event()
    loads = []

    def download(name, report):
        report(10, 1000)
        started.set()
        gate.wait(5)
        report(20, 1000)
        return "/cache/" + name
    m, _ = make(find=lambda n: None, download=download,
                load=lambda p: loads.append(p) or ("model", p))
    m.request("small.en")
    assert started.wait(5)
    m.disable()                            # cloud-only: backup turned off
    gate.set()
    m.join(5)
    assert loads == [] and m.state == "off" and m.get() is None
    print("PASS  turning the local model off stops its download.")


def test_same_request_during_a_download_keeps_its_progress():
    started, gate = threading.Event(), threading.Event()

    def download(name, report):
        report(10, 100)
        started.set()
        gate.wait(5)
        report(60, 100)
        return "/cache/" + name
    m, changes = make(find=lambda n: None, download=download)
    m.request("small.en")
    assert started.wait(5)
    m.request("small.en")                  # e.g. a settings save, same size
    assert m.snapshot()["state"] == "downloading" and m.snapshot()["done"] == 10
    gate.set()
    assert m.wait_ready(5)
    assert any(c["done"] == 60 for c in changes)
    print("PASS  re-requesting the model being downloaded changes nothing.")


def test_size_change_keeps_old_model_until_new_is_ready():
    gate = threading.Event()

    def load(path):
        if path.endswith("medium.en"):
            gate.wait(5)
        return ("model", path)
    m, _ = make(load=load)
    m.request("small.en")
    assert m.wait_ready(5)
    m.request("medium.en")
    time.sleep(0.05)
    assert m.get() == ("model", "/cache/small.en"), \
        "keep serving the old model while the new one loads"
    gate.set()
    assert wait_for(lambda: m.get() == ("model", "/cache/medium.en"))
    print("PASS  changing size swaps only once the new model is loaded.")


def test_disable_and_waiting():
    m, _ = make()
    m.set_waiting()
    assert m.state == "waiting" and m.wait_ready(0.05) is False
    m.request("small.en")
    assert m.wait_ready(5)
    m.disable()
    assert m.get() is None and m.state == "off"
    print("PASS  waiting before setup; disable drops the model (cloud-only).")


def test_wait_ready_times_out_while_downloading():
    gate = threading.Event()

    def download(name, report):
        report(10, 100)
        gate.wait(5)
        return "/cache/" + name
    m, _ = make(find=lambda n: None, download=download)
    m.request("small.en")
    assert wait_for(lambda: m.state == "downloading")
    assert m.wait_ready(0.2) is False
    gate.set()
    assert m.wait_ready(5)
    print("PASS  wait_ready gives up after its timeout.")


def test_describe_error():
    assert "internet" in model_manager.describe_error(ConnectionError("x"))
    assert model_manager.describe_error(OSError(28, "No space left on device")) == "Your disk is full."

    class LocalEntryNotFoundError(Exception):
        pass
    assert "internet" in model_manager.describe_error(
        LocalEntryNotFoundError("cannot find the requested files in the local cache"))
    assert model_manager.describe_error(ValueError("weird")).startswith("ValueError: weird")
    # A Wi-Fi drop in the MIDDLE of a download surfaces as httpx's ReadError /
    # RemoteProtocolError (HF only retries connect errors and timeouts) - the
    # most common failure of a long download must read as plain words too.
    import httpx
    req = httpx.Request("GET", "https://huggingface.co/x")
    for exc in (httpx.ReadError("[WinError 10054] connection reset", request=req),
                httpx.RemoteProtocolError("peer closed connection", request=req)):
        assert "internet" in model_manager.describe_error(exc), type(exc).__name__
    print("PASS  errors become short, plain reasons.")


def test_progress_bar_class_counts_bytes():
    reports = []
    cls = model_manager._progress_bar_class(lambda d, t: reports.append((d, t)))
    bar = cls(total=0, unit="B", unit_scale=True, desc="Downloading")
    bar.total += 100
    bar.update(40)
    bar.update(60)
    bar.close()
    assert reports == [(40, 100), (100, 100)], reports
    files = cls(total=3, desc="Fetching 3 files")    # the file-count bar
    files.update(1)
    files.close()
    assert len(reports) == 2, "only the byte counter is reported"
    print("PASS  the tqdm stand-in reports bytes and draws nothing.")


def test_download_uses_plain_https():
    """Hugging Face's newer Xet transfer reports progress in a few big jumps
    (a bar stuck near 0% for most of a slow download); plain HTTPS reports
    every 10 MB. download() must switch Xet off before fetching."""
    from unittest import mock
    import huggingface_hub
    from huggingface_hub import constants
    seen = {}

    def fake_snapshot(repo_id, **kwargs):
        seen["xet_off"] = constants.HF_HUB_DISABLE_XET
        seen["repo"] = repo_id
        return "/cache/x"
    saved = constants.HF_HUB_DISABLE_XET
    constants.HF_HUB_DISABLE_XET = False
    try:
        with mock.patch.object(huggingface_hub, "snapshot_download", fake_snapshot):
            assert model_manager.download("small.en", lambda d, t: None) == "/cache/x"
    finally:
        constants.HF_HUB_DISABLE_XET = saved
    assert seen == {"xet_off": True, "repo": "Systran/faster-whisper-small.en"}, seen
    print("PASS  model downloads use plain HTTPS (steady progress).")


def test_wait_loaded_stops_when_a_download_starts():
    gate = threading.Event()

    def download(name, report):
        report(1, 100)
        gate.wait(5)
        return "/cache/" + name

    def slow_find(name):
        time.sleep(0.2)
        return None
    m, _ = make(find=slow_find, download=download)
    m.request("small.en")                  # "loading" until find_cached answers
    started = time.monotonic()
    assert m.wait_loaded(10) is False
    assert time.monotonic() - started < 2, "must not wait through the download"
    assert m.state == "downloading"
    gate.set()
    assert m.wait_ready(5)
    print("PASS  wait_loaded waits for a load, never for a download.")


def test_a_model_that_fails_to_load_is_downloaded_again():
    calls = []

    def download(name, report, force=False):
        calls.append(force)
        return "/cache/" + name
    loads = []

    def load(path):
        loads.append(path)
        if len(loads) == 1:
            raise RuntimeError("Unable to open file 'model.bin'")
        return ("model", path)
    m, _ = make(find=lambda n: "/cache/" + n, download=download, load=load)
    m.request("small.en")
    assert wait_for(lambda: m.state == "failed")
    assert m.snapshot()["error"] == model_manager.LOAD_FAILED
    m.retry()
    assert m.wait_ready(5) and calls == [True], calls
    print("PASS  a damaged cached model is re-downloaded on Retry.")


def test_a_load_error_after_a_fresh_download_is_reported_as_is():
    calls = []

    def download(name, report, force=False):
        calls.append(force)
        return "/cache/" + name

    def load(path):
        raise ImportError("DLL load failed while importing ctranslate2")
    m, _ = make(find=lambda n: None, download=download, load=load)
    m.request("small.en")
    assert wait_for(lambda: m.state == "failed")
    snap = m.snapshot()
    assert "DLL load failed" in snap["error"] and snap["error"] != model_manager.LOAD_FAILED
    assert snap["failed_stage"] == "load"
    m.retry()
    assert wait_for(lambda: m.state == "failed" and len(calls) == 2)
    assert calls == [False, False], "fresh files that won't load aren't 'damaged'"
    print("PASS  a load error after a fresh download is reported as it is.")


def test_a_forced_redownload_happens_once():
    calls = []

    def download(name, report, force=False):
        calls.append(force)
        return "/cache/" + name

    def load(path):
        raise RuntimeError("Unable to open file 'model.bin'")
    m, _ = make(find=lambda n: "/cache/" + n, download=download, load=load)
    m.request("small.en")
    assert wait_for(lambda: m.state == "failed")
    assert m.snapshot()["error"] == model_manager.LOAD_FAILED
    for _ in range(3):                                  # the user keeps pressing Retry
        m.retry()
        time.sleep(0.2)
        assert wait_for(lambda: m.state == "failed")
    assert calls == [True], calls
    assert m.snapshot()["error"] != model_manager.LOAD_FAILED
    print("PASS  a re-download is tried once, never on every Retry.")


if __name__ == "__main__":
    test_cached_model_loads_without_download()
    test_missing_model_downloads_with_progress()
    test_progress_never_passes_the_total()
    test_failure_then_retry()
    test_newer_request_supersedes()
    test_superseded_download_is_abandoned()
    test_disable_stops_a_download()
    test_same_request_during_a_download_keeps_its_progress()
    test_size_change_keeps_old_model_until_new_is_ready()
    test_disable_and_waiting()
    test_wait_ready_times_out_while_downloading()
    test_describe_error()
    test_progress_bar_class_counts_bytes()
    test_download_uses_plain_https()
    test_wait_loaded_stops_when_a_download_starts()
    test_a_model_that_fails_to_load_is_downloaded_again()
    test_a_load_error_after_a_fresh_download_is_reported_as_is()
    test_a_forced_redownload_happens_once()
    print("\nAll model manager tests passed.")
