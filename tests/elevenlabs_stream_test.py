r"""
Standalone probe for elevenlabs_stream.py - streaming a dictation to
ElevenLabs while it's recorded, and getting the text back on release.

Run from the project root:   venv\Scripts\python tests\elevenlabs_stream_test.py

Safe: every test talks to a FAKE ElevenLabs server on 127.0.0.1 (websockets'
own server), so no key, no network and no cost.
"""

import base64
import json
import os
import sys
import threading
import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import numpy as np
from websockets.sync.server import serve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import elevenlabs_stream as es  # noqa: E402


class FakeElevenLabs:
    """
    A local stand-in for the realtime endpoint. Options:
      status      - refuse the handshake with this HTTP status
      first       - a message sent INSTEAD of session_started (then close)
      start_delay - seconds before session_started
      handshake_delay - seconds before the websocket handshake is answered
      reply       - fn(audio_bytes since the last commit) -> messages sent
                    after a commit (None = never answer)
      reply_delay - seconds before answering the FIRST commit
      min_commit  - refuse a commit of less audio than this many seconds the
                    way ElevenLabs does ("commit_throttled", then it hangs up)
      extra       - messages sent after the first audio chunk arrives
      close_after_audio - close the socket after this many audio messages
    """

    def __init__(self, **opts):
        self.opts = opts
        self.path = self.headers = None
        self.audio = bytearray()
        self.since_commit = bytearray()
        self.commits = 0
        self.audio_messages = 0
        self.closed = threading.Event()
        self.server = serve(self._handler, "127.0.0.1", 0,
                            process_request=self._process_request)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            self.server.serve_forever()
        except OSError:
            pass    # Windows: stop() closed the socket under select() - expected

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/v1/speech-to-text/realtime"

    def _process_request(self, connection, request):
        self.path, self.headers = request.path, request.headers
        time.sleep(self.opts.get("handshake_delay", 0))
        if self.opts.get("status"):
            return connection.respond(self.opts["status"], "refused\n")
        return None

    def _handler(self, ws):
        try:
            if self.opts.get("first"):
                ws.send(json.dumps(self.opts["first"]))
                return
            time.sleep(self.opts.get("start_delay", 0))
            ws.send(json.dumps({"message_type": "session_started", "session_id": "s1"}))
            sent_extra = False
            for raw in ws:
                msg = json.loads(raw)
                assert msg["message_type"] == "input_audio_chunk" and msg["sample_rate"] == 16000
                chunk = base64.b64decode(msg["audio_base_64"])
                if chunk:
                    self.audio += chunk
                    self.since_commit += chunk
                    self.audio_messages += 1
                    if not sent_extra:
                        sent_extra = True
                        for m in self.opts.get("extra", []):
                            ws.send(json.dumps(m))
                    if self.audio_messages == self.opts.get("close_after_audio"):
                        return
                if msg["commit"]:
                    if len(self.since_commit) < self.opts.get("min_commit", 0) * 32000:
                        ws.send(json.dumps({"message_type": "commit_throttled",
                                            "error": "You need at least 0.3s of uncommitted "
                                                     "audio before committing"}))
                        return
                    self.commits += 1
                    if self.commits == 1:
                        time.sleep(self.opts.get("reply_delay", 0))
                    reply = self.opts.get("reply", lambda audio: [
                        {"message_type": "committed_transcript", "text": "hello world"}])
                    segment, self.since_commit = bytes(self.since_commit), bytearray()
                    if reply is not None:
                        for m in reply(segment):
                            ws.send(json.dumps(m))
        finally:
            self.closed.set()

    def stop(self):
        self.server.shutdown()


def ramp(n, start=0):
    """Audio whose every sample is distinct, so order and loss both show."""
    return ((np.arange(start, start + n) % 20000) / 32767.0).astype(np.float32)


def expected_pcm(audio):
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def stream(fake, chunks, terms=(), timeout=2.0, **kw):
    s = es.Session("xi_test_key", terms, url=fake.url, **kw).start()
    for c in chunks:
        s.feed(c.reshape(-1, 1))           # the mic hands (n, 1) arrays
    return s, s.finish(timeout)


def test_every_sample_arrives_in_order_and_the_text_comes_back():
    fake = FakeElevenLabs()
    audio = ramp(16000)
    s, text = stream(fake, np.array_split(audio, 25), terms=["Kalshi", "Sam"])
    assert text == "hello world"
    assert bytes(fake.audio) == expected_pcm(audio), "audio lost or reordered"
    assert fake.commits == 1
    q = parse_qs(urlparse(fake.path).query)
    assert q["model_id"] == ["scribe_v2_realtime"] and q["audio_format"] == ["pcm_16000"]
    assert q["commit_strategy"] == ["manual"] and q["language_code"] == ["en"]
    assert q["no_verbatim"] == ["true"] and q["keyterms"] == ["Kalshi", "Sam"]
    assert fake.headers["xi-api-key"] == "xi_test_key"
    assert abs(s.streamed_seconds - 1.0) < 1e-6 and s.final_ms is not None
    fake.stop()
    print("PASS  every sample arrives in order; one commit; the text comes back.")


def test_audio_recorded_while_connecting_is_sent():
    fake = FakeElevenLabs(start_delay=0.4)
    audio = ramp(8000, 5)
    _s, text = stream(fake, np.array_split(audio, 10))
    assert text == "hello world" and bytes(fake.audio) == expected_pcm(audio)
    fake.stop()
    print("PASS  audio recorded before the connection opened is sent once it opens.")


def test_committed_segments_join():
    fake = FakeElevenLabs(
        extra=[{"message_type": "committed_transcript", "text": "Hello there."}],
        reply=lambda a: [{"message_type": "partial_transcript", "text": "How"},
                         {"message_type": "committed_transcript", "text": "How are you?"}])
    s = es.Session("k", url=fake.url).start()
    s.feed(ramp(3200))
    time.sleep(0.3)                          # the unsolicited segment arrives first
    assert s.finish(2.0) == "Hello there. How are you?"
    fake.stop()
    print("PASS  every committed segment is kept, in order.")


def test_error_messages_map_to_kinds():
    cases = {"auth_error": "auth", "quota_exceeded": "quota", "rate_limited": "busy",
             "queue_overflow": "busy", "resource_exhausted": "busy",
             "unaccepted_terms": "terms", "input_error": "server",
             "commit_throttled": "server"}
    for message_type, kind in cases.items():
        fake = FakeElevenLabs(first={"message_type": message_type, "error": "nope"})
        try:
            stream(fake, [ramp(1600)])
        except es.StreamError as exc:
            assert exc.kind == kind, (message_type, exc.kind)
        else:
            raise AssertionError(f"{message_type} should fail")
        fake.stop()
    print("PASS  each ElevenLabs error message becomes the right kind of failure.")


def test_handshake_refusals_and_no_server():
    for status, kind in ((401, "auth"), (403, "auth"), (402, "quota"), (429, "busy"),
                         (500, "network")):
        fake = FakeElevenLabs(status=status)
        try:
            stream(fake, [ramp(1600)])
        except es.StreamError as exc:
            assert exc.kind == kind, (status, exc.kind)
        else:
            raise AssertionError(f"HTTP {status} should fail")
        fake.stop()
    fake = FakeElevenLabs()
    fake.stop()                              # nothing listens on that port now
    t0 = time.monotonic()
    try:
        stream(fake, [ramp(1600)])
    except es.StreamError as exc:
        assert exc.kind == "network"
    else:
        raise AssertionError("no server should fail")
    assert time.monotonic() - t0 < 6
    print("PASS  refused handshakes (401/403/402/429/5xx) and an unreachable server.")


def test_error_after_commit_and_a_dropped_connection():
    fake = FakeElevenLabs(reply=lambda a: [{"message_type": "transcriber_error", "error": "boom"}])
    try:
        stream(fake, [ramp(1600)])
    except es.StreamError as exc:
        assert exc.kind == "server" and "boom" in exc.detail
    else:
        raise AssertionError("transcriber_error should fail")
    fake.stop()
    fake = FakeElevenLabs(close_after_audio=1)
    s = es.Session("k", url=fake.url).start()
    s.feed(ramp(1600))
    fake.closed.wait(2)
    s.feed(ramp(1600))
    try:
        s.finish(2.0)
    except es.StreamError as exc:
        assert exc.kind == "network"
    else:
        raise AssertionError("a dropped connection should fail")
    fake.stop()
    print("PASS  a server error after commit, and a dropped connection.")


def test_an_explained_close_keeps_its_reason():
    fake = FakeElevenLabs(close_after_audio=1,
                          extra=[{"message_type": "quota_exceeded", "error": "out of credits"}])
    s = es.Session("k", url=fake.url).start()
    s.feed(ramp(1600))
    fake.closed.wait(2)
    time.sleep(0.1)
    s.feed(ramp(1600))
    try:
        s.finish(2.0)
    except es.StreamError as exc:
        assert exc.kind == "quota", exc.kind
    else:
        raise AssertionError("expected quota")
    fake.stop()
    print("PASS  when ElevenLabs explains and then closes, the explanation wins.")


def test_a_slow_answer_times_out():
    fake = FakeElevenLabs(reply=None)
    t0 = time.monotonic()
    try:
        stream(fake, [ramp(1600)], timeout=0.3)
    except es.StreamError as exc:
        assert exc.kind == "timeout"
    else:
        raise AssertionError("expected timeout")
    assert time.monotonic() - t0 < 1.5
    assert fake.closed.wait(3), "the socket must be closed after a timeout"
    fake.stop()
    print("PASS  no answer within the deadline -> timeout, and the socket closes.")


def test_cancel_sends_no_commit():
    fake = FakeElevenLabs()
    s = es.Session("k", url=fake.url).start()
    s.feed(ramp(3200))
    time.sleep(0.3)
    s.cancel()
    assert fake.closed.wait(3) and fake.commits == 0
    fake.stop()
    print("PASS  cancel closes the stream without asking for text.")


def test_no_speech_is_empty_text():
    fake = FakeElevenLabs(reply=lambda a: [{"message_type": "insufficient_audio_activity",
                                            "error": "no speech"}])
    _s, text = stream(fake, [ramp(1600)])
    assert text == ""
    fake.stop()
    print("PASS  'insufficient audio activity' means no speech: empty text, not an error.")


def test_missing_websockets_is_a_broken_install():
    with mock.patch.dict(sys.modules, {"websockets.sync.client": None}):
        s = es.Session("k", url="ws://127.0.0.1:9/x").start()
        try:
            s.finish(2.0)
        except es.StreamError as exc:
            assert exc.kind == "broken"
        else:
            raise AssertionError("expected broken")
    print("PASS  a missing websockets library is reported as a broken install.")


def test_keyterms_follow_elevenlabs_limits():
    terms = [f"term{i}" for i in range(60)] + ["a" * 21, "KALSHI", "Kalshi"]
    out = es.keyterms(terms, "Sam")
    assert out[0] == "Sam" and len(out) == 50
    assert "a" * 21 not in out
    assert "Kalshi" in out and "KALSHI" not in out, "newest spelling wins"
    assert "term59" in out and "term0" not in out, "past 50, the newest terms win"
    assert es.keyterms(["sam", "Other"], "Sam") == ["Sam", "Other"]
    assert es.keyterms([], "") == []
    print("PASS  keyterms: name first, <=20 chars, no duplicates, the newest 50.")


def test_check_key_outcomes():
    fake = FakeElevenLabs()
    assert es.check_key("xi_ok", url=fake.url)["status"] == "ok"
    assert fake.commits == 0 and fake.audio == bytearray(), "a check sends no audio"
    fake.stop()
    for opts, status in (({"status": 401}, "rejected"),
                         ({"first": {"message_type": "unaccepted_terms", "error": "x"}}, "error")):
        fake = FakeElevenLabs(**opts)
        r = es.check_key("xi_x", url=fake.url)
        assert r["status"] == status and r["message"], (opts, r)
        fake.stop()
    fake = FakeElevenLabs()
    fake.stop()
    assert es.check_key("xi_x", url=fake.url)["status"] == "offline"
    assert es.check_key("   ")["status"] == "rejected"
    print("PASS  check_key says works / rejected / offline / error, and costs nothing.")


def test_get_usage_outcomes():
    import httpx
    req = httpx.Request("GET", es.SUBSCRIPTION_URL)

    def answer(status, body):
        return lambda url, headers, timeout: httpx.Response(status, json=body, request=req)
    ok = answer(200, {"character_count": 1200, "character_limit": 30000,
                      "next_character_count_reset_unix": 1791936000, "tier": "starter"})
    assert es.get_usage("xi", get=ok) == {"status": "ok", "used": 1200, "limit": 30000,
                                          "resets_at": 1791936000, "tier": "starter"}
    perm = answer(401, {"detail": {"status": "missing_permissions",
                                   "message": "missing the permission user_read"}})
    assert es.get_usage("xi", get=perm)["status"] == "no_permission"
    bad = answer(401, {"detail": {"status": "invalid_api_key"}})
    assert es.get_usage("xi", get=bad)["status"] == "rejected"
    assert es.get_usage("xi", get=answer(500, {}))["status"] == "error"
    assert es.get_usage("xi", get=answer(200, {"tier": "x"}))["status"] == "error"

    def offline(url, headers, timeout):
        raise httpx.ConnectError("no route", request=req)
    assert es.get_usage("xi", get=offline)["status"] == "offline"
    assert es.get_usage("  ")["status"] == "rejected"
    print("PASS  get_usage: numbers, no permission, rejected, offline, error.")


def silence(n):
    return np.zeros(n, dtype=np.float32)


def seconds_reply(audio):
    """Answer a commit with how many whole seconds it covered: "[22s]"."""
    return [{"message_type": "committed_transcript", "text": f"[{len(audio) // 32000}s]"}]


def test_a_long_take_is_committed_at_a_pause():
    # ElevenLabs commits on its own after ~36 s - and that text comes back
    # with most of the take missing. So Scribe commits first, at a pause.
    fake = FakeElevenLabs(reply=seconds_reply)
    audio = np.concatenate([ramp(22 * 16000), silence(8000), ramp(10 * 16000, 7)])
    _s, text = stream(fake, np.array_split(audio, 200))
    assert fake.commits == 2, fake.commits
    assert text == "[22s] [10s]", text
    assert bytes(fake.audio) == expected_pcm(audio), "every sample still arrives, in order"
    fake.stop()
    print("PASS  a long take is committed at a pause after 20 s, then on release.")


def _levels_audio(levels):
    """100 ms of 440 Hz tone per level, each with exactly that RMS."""
    t = np.arange(1600) / 16000
    return np.concatenate([np.sqrt(2) * lv * np.sin(2 * np.pi * 440 * t) for lv in levels]).astype(np.float32)


def test_the_pause_threshold_follows_the_mic():
    q = es.quiet_threshold
    assert q([0.02] * 5) == es.QUIET_RMS            # too little to judge yet
    assert q([0.3] * 50) == es.QUIET_RMS            # a steady sound: no contrast
    # A laptop's raw mic: room at 0.007, the voice barely above (0.009-0.016).
    laptop = [0.007, 0.012, 0.009, 0.009, 0.0095, 0.014, 0.007, 0.012, 0.016, 0.011] * 20
    thr = q(laptop)
    assert 0.007 < thr < 0.009, thr                                  # the voice's dips are speech
    # A close headset: dead quiet between words, loud voice.
    headset = [0.0002, 0.05, 0.04, 0.06, 0.03, 0.0002, 0.05, 0.07, 0.04, 0.05] * 20
    assert 0.0002 < q(headset) < 0.02, q(headset)
    # Noise suppression gates the pauses to exact zeros: still a pause.
    gated = [0.0, 0.0, 0.03, 0.04, 0.02, 0.0, 0.05, 0.03, 0.04, 0.03] * 20
    assert 0.0 < q(gated) < 0.01, q(gated)
    print("PASS  a pause is judged against this take's own quiet and loud levels.")


def test_a_quiet_mic_is_not_committed_mid_sentence():
    # The same laptop voice for 22 s: its softer syllables are under the
    # old fixed 0.01 for 0.3 s at a time, which used to count as a pause and
    # cut the take mid-sentence. Only the real pause (0.5 s of room) counts.
    speech = [0.007, 0.012, 0.009, 0.009, 0.0095, 0.014, 0.007, 0.012, 0.016, 0.011]
    audio = np.concatenate([_levels_audio(speech * 22), _levels_audio([0.007] * 5),
                            _levels_audio(speech * 10)])
    fake = FakeElevenLabs(reply=seconds_reply)
    _s, text = stream(fake, np.array_split(audio, 200))
    assert fake.commits == 2 and text == "[22s] [10s]", (fake.commits, text)
    fake.stop()
    print("PASS  a quiet mic's long take is split at its real pause, not mid-sentence.")


def test_without_a_pause_a_commit_is_forced_before_36_seconds():
    fake = FakeElevenLabs(reply=seconds_reply)
    _s, text = stream(fake, np.array_split(ramp(40 * 16000), 300))
    assert fake.commits == 2 and text == "[30s] [10s]", (fake.commits, text)
    fake.stop()
    print("PASS  without a pause, Scribe still commits at 30 s.")


def test_release_waits_for_every_commit_in_flight():
    fake = FakeElevenLabs(reply=seconds_reply, reply_delay=0.5)
    audio = np.concatenate([ramp(21 * 16000), silence(8000), ramp(2 * 16000, 3)])
    _s, text = stream(fake, np.array_split(audio, 200))
    assert text == "[21s] [2s]", text
    fake.stop()
    print("PASS  on release, every committed piece still on its way is waited for.")


def test_opening_has_one_deadline():
    # A slow handshake AND a slow session start share one deadline.
    fake = FakeElevenLabs(handshake_delay=0.8, start_delay=5)
    t0 = time.monotonic()
    try:
        stream(fake, [ramp(1600)], connect_timeout=1.0)
    except es.StreamError as exc:
        assert exc.kind == "network"
    else:
        raise AssertionError("a session that never starts must fail")
    assert time.monotonic() - t0 < 1.5, time.monotonic() - t0
    fake.stop()
    # ...and a slow-but-in-time opening still works.
    fake = FakeElevenLabs(handshake_delay=0.4, start_delay=0.3)
    _s, text = stream(fake, [ramp(1600)], connect_timeout=1.0)
    assert text == "hello world"
    fake.stop()
    print("PASS  opening a session has one deadline: handshake + start together.")


def test_an_empty_error_field_is_not_a_failure():
    fake = FakeElevenLabs(extra=[{"message_type": "session_info", "error": None}])
    _s, text = stream(fake, [ramp(3200)])
    assert text == "hello world"
    fake.stop()
    print("PASS  a message with \"error\": null is not a failure.")


def test_a_failed_send_reports_what_the_server_said():
    class ClosedSocket:
        """The server explained, then hung up: sending fails, the reason waits."""
        def __init__(self, messages):
            self.messages = [json.dumps(m) for m in messages]

        def send(self, data):
            raise ConnectionError("closed")

        def recv(self, timeout=None):
            if self.messages:
                return self.messages.pop(0)
            raise ConnectionError("closed")
    session = es.Session("k")
    for said, kind in (([{"message_type": "quota_exceeded", "error": "out"}], "quota"),
                       ([], "network")):
        try:
            session._send(ClosedSocket(said), ramp(1600), commit=False)
        except es.StreamError as exc:
            assert exc.kind == kind, (said, exc.kind)
        else:
            raise AssertionError("a failed send must fail")
    print("PASS  a failed send reports the reason the server gave before hanging up.")


def test_a_short_last_tail_is_padded_to_elevenlabs_minimum():
    # Pause commit at 22.3 s, then only 0.1 s more before release: a commit of
    # 0.1 s would be refused - so the tail is padded with silence to 0.3 s.
    fake = FakeElevenLabs(reply=seconds_reply, min_commit=0.3)
    audio = np.concatenate([ramp(22 * 16000), silence(6400)])
    _s, text = stream(fake, np.array_split(audio, 200))
    assert fake.commits == 2 and text.startswith("[22s]"), (fake.commits, text)
    fake.stop()
    fake = FakeElevenLabs(reply=seconds_reply, min_commit=0.3)     # forced at 30 s
    _s, text = stream(fake, np.array_split(ramp(30 * 16000 + 1600), 300))
    assert fake.commits == 2 and text.startswith("[30s]"), (fake.commits, text)
    fake.stop()
    print("PASS  a last tail under 0.3 s is padded, so ElevenLabs never refuses it.")


if __name__ == "__main__":
    test_every_sample_arrives_in_order_and_the_text_comes_back()
    test_audio_recorded_while_connecting_is_sent()
    test_committed_segments_join()
    test_error_messages_map_to_kinds()
    test_handshake_refusals_and_no_server()
    test_error_after_commit_and_a_dropped_connection()
    test_an_explained_close_keeps_its_reason()
    test_a_slow_answer_times_out()
    test_cancel_sends_no_commit()
    test_no_speech_is_empty_text()
    test_missing_websockets_is_a_broken_install()
    test_keyterms_follow_elevenlabs_limits()
    test_check_key_outcomes()
    test_get_usage_outcomes()
    test_a_long_take_is_committed_at_a_pause()
    test_the_pause_threshold_follows_the_mic()
    test_a_quiet_mic_is_not_committed_mid_sentence()
    test_without_a_pause_a_commit_is_forced_before_36_seconds()
    test_release_waits_for_every_commit_in_flight()
    test_opening_has_one_deadline()
    test_an_empty_error_field_is_not_a_failure()
    test_a_failed_send_reports_what_the_server_said()
    test_a_short_last_tail_is_padded_to_elevenlabs_minimum()
    print("\nAll ElevenLabs streaming tests passed.")
