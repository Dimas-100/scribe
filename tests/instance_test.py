r"""
Standalone probe for instance.py - the single-instance mutex and the
control pipe.

Run from the project root:   venv\Scripts\python tests\instance_test.py

Safe: every mutex and pipe here has a unique throwaway name, and the module's
own names derive from a temp SCRIBE_DATA_DIR - nothing can reach a running
Scribe.
"""

import os
import sys
import tempfile
import time
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="scribe-inst-test-")
os.environ["SCRIBE_DATA_DIR"] = TMP
for name in ("storage", "instance"):
    sys.modules.pop(name, None)
import instance  # noqa: E402


def _pipe():
    return r"\\.\pipe\Scribe-test-" + uuid.uuid4().hex[:12]


def test_names_follow_the_data_folder():
    assert instance.INSTANCE_ID == instance._instance_id(TMP)
    assert instance._instance_id(TMP) != instance._instance_id(TMP + "-other")
    assert instance._instance_id(TMP.upper()) == instance._instance_id(TMP.lower()), \
        "Windows paths are case-insensitive"
    assert instance.MUTEX_NAME == "Local\\Scribe-" + instance.INSTANCE_ID
    assert instance.PIPE_NAME == "\\\\.\\pipe\\Scribe-" + instance.INSTANCE_ID
    real = instance._instance_id(os.path.join(os.environ.get("APPDATA", "x"), "Scribe"))
    assert instance.INSTANCE_ID != real, "a test run must never share the real Scribe's names"
    print("PASS  mutex and pipe names derive from the data folder.")


def test_second_acquire_is_refused():
    name = "Local\\Scribe-test-" + uuid.uuid4().hex[:12]
    assert instance.acquire(name) is True
    assert instance.acquire(name) is False
    print("PASS  a second copy is refused while the first holds the mutex.")


def test_is_running_asks_without_claiming():
    name = "Local\\Scribe-test-" + uuid.uuid4().hex[:12]
    assert instance.is_running(name) is False
    assert instance.is_running(name) is False, "asking must not claim the slot"
    assert instance.acquire(name) is True, "...so Scribe can still start after asking"
    assert instance.is_running(name) is True
    print("PASS  is_running() tells whether Scribe runs, without claiming its slot.")


def test_pipe_round_trip():
    address = _pipe()
    seen = []

    def handler(msg):
        seen.append(msg)
        return {"state": "ready"} if msg.get("cmd") == "status" else None
    assert instance.serve(handler, address=address) is not None
    assert instance.send({"cmd": "show"}, address=address) is True
    assert instance.request({"cmd": "status"}, address=address) == {"state": "ready"}
    assert seen == [{"cmd": "show"}, {"cmd": "status"}]
    print("PASS  commands arrive; status gets its reply.")


def test_no_server_fails_fast():
    started = time.monotonic()
    assert instance.send({"cmd": "show"}, address=_pipe()) is False
    assert instance.request({"cmd": "status"}, address=_pipe()) is None
    assert time.monotonic() - started < 1.0
    print("PASS  with no Scribe running, clients give up at once.")


def test_handler_errors_do_not_kill_the_server():
    address = _pipe()

    def handler(msg):
        if msg.get("cmd") == "boom":
            raise RuntimeError("bad handler")
        return {"ok": "still here"}
    instance.serve(handler, address=address)
    assert instance.request({"cmd": "boom"}, address=address, timeout=1.0) is None
    assert instance.request({"cmd": "ping"}, address=address) == {"ok": "still here"}
    print("PASS  a failing handler is logged; the pipe keeps serving.")


def test_garbage_is_ignored():
    from multiprocessing.connection import Client
    address = _pipe()
    instance.serve(lambda msg: {"echo": msg.get("cmd")}, address=address)
    with Client(address, family="AF_PIPE") as conn:
        conn.send_bytes(b"\x80 not json")
    with Client(address, family="AF_PIPE") as conn:
        conn.send_bytes(b"[1, 2, 3]")           # JSON, but not a message
    assert instance.request({"cmd": "hi"}, address=address) == {"echo": "hi"}
    print("PASS  non-JSON and non-object messages are ignored.")


def test_serve_waits_for_the_pipe_name_to_free_up():
    from multiprocessing.connection import Listener
    address = _pipe()
    squatter = Listener(address, family="AF_PIPE")         # the name is taken
    instance.RETRY_SECONDS = 0.2
    assert instance.serve(lambda m: {"ok": "late"}, address=address) is not None
    time.sleep(0.4)
    squatter.close()
    deadline = time.monotonic() + 5
    reply = None
    while reply is None and time.monotonic() < deadline:
        reply = instance.request({"cmd": "x"}, address=address, timeout=0.5)
    assert reply == {"ok": "late"}, reply
    print("PASS  the pipe comes up as soon as its name is free.")


def test_accept_errors_do_not_stop_the_server():
    address = _pipe()
    real_listener = instance.Listener
    # pywebview-free, but multiprocessing's PipeListener.accept can also fail
    # with an AssertionError (assert err == 0) - any error must be survived.
    errors = [OSError("simulated"), AssertionError()]

    class Flaky:
        def __init__(self, *a, **k):
            self._l = real_listener(*a, **k)

        def accept(self):
            if errors:
                raise errors.pop(0)
            return self._l.accept()
    instance.ACCEPT_BACKOFF = 0.2
    with mock.patch.object(instance, "Listener", Flaky):
        instance.serve(lambda m: {"ok": "still"}, address=address)
        time.sleep(0.9)
        assert instance.request({"cmd": "x"}, address=address) == {"ok": "still"}
    print("PASS  an accept error is logged and the pipe keeps serving.")


if __name__ == "__main__":
    test_names_follow_the_data_folder()
    test_second_acquire_is_refused()
    test_is_running_asks_without_claiming()
    test_pipe_round_trip()
    test_no_server_fails_fast()
    test_handler_errors_do_not_kill_the_server()
    test_garbage_is_ignored()
    test_serve_waits_for_the_pipe_name_to_free_up()
    test_accept_errors_do_not_stop_the_server()
    print("\nAll instance tests passed.")
