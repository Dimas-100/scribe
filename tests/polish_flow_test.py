r"""
Standalone probe for AI polish inside app.py's dictation flow: when a take is
polished, what happens when Groq can't, and "fix that".

Run from the project root:   venv\Scripts\python tests\polish_flow_test.py

Safe: tests/harness.py (temp data folder, mocked mic and model) and a fake
Groq client - no network, no key.
"""

import json
import os
import sys
import time
from types import SimpleNamespace
from unittest import mock

import httpx
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402

REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
SPOKEN = "okay so um I think we should uh change the handle press function in app dot py"
POLISHED = "I think we should change the handle_press function in app.py."


class FakeClient:
    """Stands in for groq.Groq: chat.completions.create answers or raises."""

    def __init__(self, answer=POLISHED, error=None, delay=0.0):
        self.answer, self.error, self.delay, self.calls = answer, error, delay, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.answer))])


def _groq(app, **fake_kw):
    """Groq as the service with a healthy key, polish on; returns the fake."""
    app.USE_CLOUD, app.CLOUD_PROVIDER, app.POLISH = True, "groq", True
    app.GROQ_API_KEY, app.ELEVENLABS_API_KEY = "gsk_live", ""
    app.cloud_paused_until = app.polish_paused_until = 0.0
    app._rejected_key = None
    app._notice_last.clear()
    app.tray_icon = mock.MagicMock()
    app._tray_ready = True
    app.VOCAB_CORRECTIONS = []
    return FakeClient(**fake_kw)


def _take(app, fake, spoken=SPOKEN):
    """One dictation whose transcript is `spoken`: (delivered output, kwargs)."""
    with mock.patch.object(app, "_get_groq_client", return_value=fake), \
         mock.patch.object(app, "_transcribe_take", return_value=(spoken, "groq")), \
         mock.patch.object(app, "_deliver_job") as dj, \
         mock.patch.object(app, "handle_whole_utterance_action") as action:
        app.process_audio(np.zeros(16000, dtype=np.float32), None)
    if not dj.called:
        return None, {"action": action.called}
    return dj.call_args.args[0], dj.call_args.kwargs


def _titles(app):
    return [c.args[1] for c in app.tray_icon.notify.call_args_list]


def test_a_take_is_polished_and_logged_with_its_raw_text(app):
    fake = _groq(app)
    with mock.patch.object(app, "_get_groq_client", return_value=fake), \
         mock.patch.object(app, "_transcribe_take", return_value=(SPOKEN, "groq")), \
         mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "deliver") as deliver, \
         mock.patch.object(app, "_any_modifier_down", return_value=False):
        app.process_audio(np.zeros(16000, dtype=np.float32), 123, "App", "app.exe")
    assert deliver.call_args.args[0].strip() == POLISHED
    entry = json.loads(open(app.LOG_FILE, encoding="utf-8").read().splitlines()[-1])
    assert entry["text"] == POLISHED and entry["raw"] == SPOKEN and entry["polished"] is True
    assert fake.calls[0]["model"] == app.polish.MODEL
    print("PASS  a take is polished; history keeps what was said and what was typed.")


def test_when_polish_is_skipped(app):
    cases = {
        "polish off": lambda: setattr(app, "POLISH", False),
        "cloud off": lambda: setattr(app, "USE_CLOUD", False),
        "no Groq key": lambda: setattr(app, "GROQ_API_KEY", ""),
        "rejected key": lambda: setattr(app, "_rejected_key", "gsk_live"),
        "Groq paused": lambda: setattr(app, "cloud_paused_until", time.monotonic() + 60),
        "polish paused": lambda: setattr(app, "polish_paused_until", time.monotonic() + 60),
    }
    for why, arrange in cases.items():
        fake = _groq(app)
        arrange()
        output, _ = _take(app, fake)
        assert not fake.calls, why
        assert output.strip().startswith("Okay so"), (why, output)
    fake = _groq(app)
    output, _ = _take(app, fake, spoken="send it now")
    assert not fake.calls and output.strip() == "Send it now", output
    for command in ("scratch that", "new line", "Um, uh, scratch that.",
                    "Um, uh, fix that.", "Um, uh, new line."):
        fake = _groq(app)                  # 4 words: long enough to be polished
        output, kw = _take(app, fake, spoken=command)
        assert not fake.calls, command
    print("PASS  no polish when off, cloud off, no/rejected key, paused, 3 words or fewer, or a command.")


def test_failures_type_the_words_as_spoken(app):
    import groq
    resp = lambda status, headers=None: httpx.Response(status, request=REQ, headers=headers or {})  # noqa: E731
    cases = [  # error, notice title, polish pause, marks key rejected
        (groq.AuthenticationError("bad", response=resp(401), body=None),
         "Groq API key rejected", 0, True),
        (groq.RateLimitError("slow", response=resp(429, {"retry-after": "120"}), body=None),
         "AI polish paused", 120, False),
        (groq.APIConnectionError(request=REQ), "AI polish unavailable", 30, False),
        (groq.InternalServerError("boom", response=resp(500), body=None),
         "AI polish unavailable", 30, False),
        (groq.APITimeoutError(request=REQ), "AI polish skipped", 0, False),
    ]
    for error, title, pause, rejects in cases:
        fake = _groq(app, error=error)
        output, _ = _take(app, fake)
        assert output.strip().startswith("Okay so"), (title, output)
        assert _titles(app) == [title], (title, _titles(app))
        # (+1e-6: Windows' clock ticks every ~15 ms, so "now" can be the
        # same instant the pause was set - and the sum rounds a hair over.)
        left = app.polish_paused_until - time.monotonic()
        assert (pause - 5 < left <= pause + 1e-6) if pause else left <= 1e-6, (title, left)
        assert app.cloud_paused_until == 0.0, "a polish problem never pauses Groq transcription"
        assert (app._rejected_key == "gsk_live") == rejects
    fake = _groq(app, answer="Here's how to change it: open app.py and edit the function.")
    output, _ = _take(app, fake)
    assert output.strip().startswith("Okay so") and _titles(app) == []
    log = open(os.path.join(app._test_data_dir, "error_log.txt"), encoding="utf-8").read()
    assert "suspicious" in log and "Here's how" not in log, "logged - but never the text"
    print("PASS  every polish failure types the words as spoken, with one clear notice.")


def test_a_forbidden_model_pauses_only_polish(app):
    import groq
    err = groq.PermissionDeniedError("model not allowed", response=httpx.Response(
        403, request=REQ), body=None)
    fake = _groq(app, error=err)
    output, _ = _take(app, fake)
    assert output.strip().startswith("Okay so")
    assert app._rejected_key is None and app.cloud_available(), "Groq still transcribes"
    assert app.polish_paused_until > app.time.monotonic() + 3600 * 24, "paused for the session"
    assert _titles(app) == ["AI polish unavailable"]
    assert "turn off AI polish" in app.tray_icon.notify.call_args.args[0]
    # Changing the Groq key or the polish switch in Settings ends the pause.
    app.storage.save_config_changes({"use_cloud": True, "groq_api_key": "gsk_other_key",
                                     "polish": True})
    app.reload_config()
    assert app.polish_paused_until == 0.0
    app.polish_paused_until = app.time.monotonic() + 3600
    app.reload_config()                               # nothing changed: still paused
    assert app.polish_paused_until > app.time.monotonic()
    app.storage.save_config_changes({"polish": False})
    app.reload_config()
    assert app.polish_paused_until == 0.0
    app.storage.save_config_changes({"use_cloud": False, "groq_api_key": ""})
    app.reload_config()
    # "fix that" with a forbidden model: told, and the key is NOT rejected.
    fake = _groq(app, error=err)
    _fix(app, fake)
    assert app._rejected_key is None and _titles(app) == ["Couldn't fix that"]
    print("PASS  a 403 on the polish model pauses only polish, until Settings change.")


def test_rate_limit_notice_says_how_long(app):
    import groq
    err = groq.RateLimitError("slow", response=httpx.Response(
        429, request=REQ, headers={"retry-after": "120"}), body=None)
    fake = _groq(app, error=err)
    _take(app, fake)
    message = app.tray_icon.notify.call_args.args[0]
    assert "2 minutes" in message, message
    print("PASS  a polish rate limit says how long Scribe types unpolished.")


def test_a_slow_groq_never_holds_a_take_up(app):
    import groq
    fake = _groq(app, error=groq.APITimeoutError(request=REQ))
    t0 = time.monotonic()
    output, _ = _take(app, fake)
    assert time.monotonic() - t0 < 1.5 and output.strip().startswith("Okay so")
    assert fake.calls[0]["timeout"].read == app.polish.TIMEOUT
    print("PASS  a slow Groq never holds a take up: the words go out as spoken.")


def test_dictionary_corrections_win_over_the_model(app):
    fake = _groq(app, answer="I think we should ask kalshi about the handle press function.")
    app.VOCAB_CORRECTIONS = [("kalshi", "Kalshi")]
    output, _ = _take(app, fake, spoken="okay so um I think we should uh ask kalshi about "
                                        "the handle press function")
    assert "Kalshi" in output and "kalshi" not in output, output
    print("PASS  Dictionary corrections still apply to polished text.")


def _fix(app, fake, original="okay so we should uh fix it"):
    """'Fix that' on a last dictation of `original`: (delivered, backspaces)."""
    app.last_output, app.last_duration = original + " ", 2.0
    app.last_output_hwnd, app.last_output_logged = 123, False
    app.last_output_app = ("App", "app.exe")
    kbd = mock.MagicMock()
    with mock.patch.object(app, "_get_groq_client", return_value=fake), \
         mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "deliver") as deliver, \
         mock.patch.object(app, "kbd", kbd), \
         mock.patch.object(app, "_any_modifier_down", return_value=False):
        app.ai_fix_last_output()
    return (deliver.call_args.args[0] if deliver.called else None), kbd.press.call_count


def test_fix_that_polishes_the_last_dictation(app):
    fake = _groq(app, answer="We should fix it.")
    delivered, backspaces = _fix(app, fake)
    assert delivered == "We should fix it. " and backspaces == len("okay so we should uh fix it ")
    assert fake.calls[0]["model"] == app.polish.MODEL
    assert fake.calls[0]["timeout"].read == app.polish.FIX_TIMEOUT
    assert app.last_output == "We should fix it. "
    fake = _groq(app, answer="Send it.")
    delivered, _ = _fix(app, fake, original="send it")
    assert delivered == "Send it. ", "you asked: even two words are polished"
    app.USE_CLOUD = False                  # asked for explicitly: cloud mode not needed
    fake = FakeClient(answer="Send it now.")
    delivered, _ = _fix(app, fake, original="send it now")
    assert delivered == "Send it now. "
    print("PASS  'fix that' polishes the last dictation again with the live model.")


def test_fix_that_says_when_it_cant(app):
    import groq
    fake = _groq(app)
    app.GROQ_API_KEY = ""
    delivered, _ = _fix(app, fake)
    assert delivered is None and _titles(app) == ["Couldn't fix that"] and not fake.calls
    fake = _groq(app, error=groq.APIConnectionError(request=REQ))
    delivered, backspaces = _fix(app, fake)
    assert delivered is None and backspaces == 0
    assert _titles(app) == ["Couldn't fix that"]
    assert app.tray_icon.notify.call_args.args[0].endswith("Your text is unchanged.")
    fake = _groq(app, error=groq.AuthenticationError(
        "bad", response=httpx.Response(401, request=REQ), body=None))
    _fix(app, fake)
    assert app._rejected_key == "gsk_live"
    for answer in ("okay so we should uh fix it",
                   "Here's a fix: rewrite the whole function from scratch using classes."):
        fake = _groq(app, answer=answer)
        delivered, backspaces = _fix(app, fake)
        assert delivered is None and backspaces == 0, answer
        assert _titles(app) == ["Nothing to fix"], (answer, _titles(app))
    print("PASS  'fix that' says so when it can't - no key, Groq down, nothing to change.")


def test_groq_is_warmed_up_before_the_first_polish(app):
    fake = _groq(app)
    client = mock.MagicMock()
    with mock.patch.object(app, "_get_groq_client", return_value=client):
        app._warm_polish().join(2)
    assert client.models.list.called, "the connection is opened ahead of time"
    client.reset_mock()
    app.POLISH = False
    with mock.patch.object(app, "_get_groq_client", return_value=client):
        assert app._warm_polish() is None
    assert not client.models.list.called, "no warm-up when polish is off"
    with mock.patch.object(app, "_warm_polish") as warm:
        app.reload_config()
    assert warm.called, "a settings reload warms up again"
    del fake
    print("PASS  Groq is warmed up in the background, only when polish can run.")


def test_fix_that_output_gets_the_dictionary(app):
    fake = _groq(app, answer="we should ask kalshi about it")
    app.VOCAB_CORRECTIONS = [("kalshi", "Kalshi")]
    delivered, _ = _fix(app, fake, original="um we should uh ask kalshi about it")
    assert delivered == "We should ask Kalshi about it ", delivered
    entry = json.loads(open(app.LOG_FILE, encoding="utf-8").read().splitlines()[-1])
    assert entry["raw"] == "um we should uh ask kalshi about it" and entry["polished"] is True
    print("PASS  'fix that' output gets the Dictionary's last word; history keeps the original.")


def test_a_broken_install_or_a_gone_model_pauses_polish_and_says_so(app):
    import groq
    fake = _groq(app)
    with mock.patch.object(app, "_get_groq_client", side_effect=ImportError("no groq")), \
         mock.patch.object(app, "_transcribe_take", return_value=(SPOKEN, "groq")), \
         mock.patch.object(app, "_deliver_job") as dj:
        for _ in range(2):
            app.process_audio(np.zeros(16000, dtype=np.float32), None)
    assert dj.call_args.args[0].strip().startswith("Okay so")
    assert _titles(app) == ["AI polish unavailable"], _titles(app)
    assert app.polish_paused_until > app.time.monotonic() + 3600 * 24
    gone = groq.NotFoundError("model not found", response=httpx.Response(404, request=REQ),
                              body=None)
    fake = _groq(app, error=gone)
    _take(app, fake)
    assert _titles(app) == ["AI polish unavailable"]
    assert "turn off AI polish" in app.tray_icon.notify.call_args.args[0]
    assert app.polish_paused_until > app.time.monotonic() + 3600 * 24
    print("PASS  a broken install or a retired model pauses polish, with one notice.")


def test_tests_never_reach_the_network(app):
    import groq
    app.GROQ_API_KEY = "gsk_fake_for_the_guard"
    app._groq_client = None
    t0 = time.monotonic()
    try:
        app._get_groq_client().models.list()
    except groq.APIConnectionError:
        pass                                   # blocked by the harness
    else:
        raise AssertionError("a harness test reached the network")
    assert time.monotonic() - t0 < 1.0
    app._groq_client = None
    print("PASS  the harness blocks every real HTTP call (warm-ups included).")


def test_fix_that_keeps_the_line_breaks(app):
    fake = _groq(app, answer="Dear Bob,\nThanks for the help yesterday.\n\nBest, Sam")
    delivered, _ = _fix(app, fake,
                        original="Dear Bob,\num thanks for the help yesterday\n\nBest, Sam")
    assert delivered == "Dear Bob,\nThanks for the help yesterday.\n\nBest, Sam ", repr(delivered)
    print("PASS  'fix that' keeps the line breaks you made.")


def test_the_polish_style_setting_is_used(app):
    for style in ("light", "full"):
        fake = _groq(app, answer="Okay, so I think we should change the handle_press function in app.py.")
        app.POLISH_STYLE = style
        _take(app, fake)
        prompt = fake.calls[0]["messages"][0]["content"]
        assert ("lightly clean" in prompt) == (style == "light"), (style, prompt[:60])
    app.POLISH_STYLE = "full"
    print("PASS  each take is polished in the style chosen in Settings.")


if __name__ == "__main__":
    app = import_app_with_mocks()
    test_tests_never_reach_the_network(app)
    test_a_take_is_polished_and_logged_with_its_raw_text(app)
    test_when_polish_is_skipped(app)
    test_failures_type_the_words_as_spoken(app)
    test_rate_limit_notice_says_how_long(app)
    test_a_forbidden_model_pauses_only_polish(app)
    test_a_slow_groq_never_holds_a_take_up(app)
    test_dictionary_corrections_win_over_the_model(app)
    test_fix_that_polishes_the_last_dictation(app)
    test_fix_that_says_when_it_cant(app)
    test_groq_is_warmed_up_before_the_first_polish(app)
    test_fix_that_output_gets_the_dictionary(app)
    test_fix_that_keeps_the_line_breaks(app)
    test_a_broken_install_or_a_gone_model_pauses_polish_and_says_so(app)
    test_the_polish_style_setting_is_used(app)
    print("\nAll polish flow tests passed.")
