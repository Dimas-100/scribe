r"""
Standalone probe for dashboard.py's welcome + Settings bridge: the page never
receives the API key, keys are checked and stored, "Finish setup" saves the
choices and tells Scribe, and live status comes over the control pipe.

Run from the project root:   venv\Scripts\python tests\welcome_test.py
(No window opens; Groq, the registry and the pipe are mocked.)
"""

import json
import os
import sys
import tempfile
from unittest import mock

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="scribe-welcome-test-")
os.environ["SCRIBE_DATA_DIR"] = TMP
for name in ("storage", "devices", "dashboard", "keystore", "instance", "autostart"):
    sys.modules.pop(name, None)
import dashboard  # noqa: E402
import keystore   # noqa: E402
import storage    # noqa: E402
import autostart  # noqa: E402

autostart_available = autostart.available

REQ = httpx.Request("GET", "https://api.groq.com/openai/v1/models")


def _fresh():
    """Start a test with no settings at all (a first run)."""
    for name in ("config.json", "config.json.bak"):
        try:
            os.remove(os.path.join(TMP, name))
        except FileNotFoundError:
            pass


def _quiet():
    """Mocks for the outside world: mics, icons, registry, pipe."""
    return [mock.patch.object(dashboard.devices, "list_input_devices", return_value=["Mic A"]),
            mock.patch.object(dashboard, "build_app_icons", return_value={}),
            mock.patch.object(dashboard.autostart, "is_enabled", return_value=False),
            mock.patch.object(dashboard.autostart, "set_enabled"),
            mock.patch.object(dashboard.instance, "send", return_value=True)]


def _run(fn):
    patches = _quiet()
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in patches:
            p.stop()


def test_page_never_sees_the_key():
    _fresh()
    storage.save_config_changes({"groq_api_key": "gsk_secret_1a2b"})
    data = _run(lambda: dashboard.JsApi().get_initial_data())
    assert "groq_api_key" not in data["config"]
    assert "gsk_secret" not in json.dumps(data)
    assert data["config"]["groq_key_hint"] == "ends in 1a2b"
    # None: this test runs on its own data folder (like a portable copy), so
    # sign-in isn't Scribe's to manage and the page hides the switch.
    assert data["config"]["start_at_login"] is None and data["welcome"] is False
    assert _run(lambda: dashboard.JsApi(welcome=True).get_initial_data())["welcome"] is True
    print("PASS  the page gets a key hint, never the key.")


def test_save_settings_stores_a_new_key_and_hides_it():
    _fresh()
    out = _run(lambda: dashboard.JsApi().save_settings(
        {"use_cloud": True, "groq_api_key": "gsk_new_key_9z9z"}))
    assert "groq_api_key" not in out and out["groq_key_hint"] == "ends in 9z9z"
    assert out["key_storage"] is None            # portable mode: nothing to warn about
    assert keystore.resolve_key(storage.load_config()[0]) == "gsk_new_key_9z9z"
    print("PASS  a new key is stored; the reply only carries its hint.")


def test_cloud_without_a_key_is_refused():
    _fresh()
    try:
        _run(lambda: dashboard.JsApi().save_settings({"use_cloud": True}))
    except storage.StorageError as exc:
        assert "Add a Groq API key" in str(exc)
    else:
        raise AssertionError("cloud with no key must be refused")
    print("PASS  turning the cloud on without a key is refused with a clear reason.")


def test_remove_key_and_cloud_off_turns_local_on():
    _fresh()
    _run(lambda: dashboard.JsApi().save_settings(
        {"use_cloud": True, "groq_api_key": "gsk_to_remove", "local_model": False}))
    out = _run(lambda: dashboard.JsApi().save_settings(
        {"use_cloud": False, "remove_groq_key": True, "local_model": False}))
    cfg = storage.load_config()[0]
    assert out["groq_key_hint"] == "" and keystore.resolve_key(cfg) == ""
    assert cfg["local_model"] is True, "with the cloud off, local is the only way to transcribe"
    print("PASS  Remove forgets the key; cloud off always keeps a local model.")


def test_start_at_login_goes_to_the_registry():
    _fresh()
    patches = _quiet()
    for p in patches:
        p.start()
    try:
        dashboard.autostart.available = lambda: True   # a normal (non-portable) copy
        dashboard.autostart.is_enabled.return_value = True
        out = dashboard.JsApi().save_settings({"start_at_login": True})
        dashboard.autostart.set_enabled.assert_called_once_with(True)
        dashboard.instance.send.assert_called_with({"cmd": "reload-config"})
    finally:
        for p in patches:
            p.stop()
        dashboard.autostart.available = autostart_available
    assert out["start_at_login"] is True
    assert "start_at_login" not in storage.read_json(storage.CONFIG_FILE)[1]
    print("PASS  start-at-sign-in lives in the registry, not config.json.")


def test_check_groq_key_outcomes():
    import groq
    target = "groq.resources.models.Models.list"
    cases = [
        (mock.patch(target, return_value=[]), "ok"),
        (mock.patch(target, side_effect=groq.AuthenticationError(
            "bad key", response=httpx.Response(401, request=REQ), body=None)), "rejected"),
        (mock.patch(target, side_effect=groq.APIConnectionError(request=REQ)), "offline"),
        (mock.patch(target, side_effect=groq.InternalServerError(
            "boom", response=httpx.Response(500, request=REQ), body=None)), "error"),
    ]
    for patcher, expected in cases:
        with patcher:
            result = dashboard.check_groq_key("gsk_x")
        assert result["status"] == expected and result["message"], (expected, result)
    assert dashboard.check_groq_key("   ")["status"] == "rejected"
    print("PASS  a key check says works / rejected / offline / error.")


def test_finish_setup_cloud():
    _fresh()
    patches = _quiet()
    for p in patches:
        p.start()
    try:
        dashboard.autostart.available = lambda: True   # a normal (non-portable) copy
        out = dashboard.JsApi(welcome=True).finish_setup({
            "use_cloud": True, "groq_api_key": "gsk_welcome_abcd", "local_model": False,
            "model_size": "small.en", "hotkey": "Ctrl + Alt",
            "mic_device": "(system default)", "start_at_login": True})
        dashboard.autostart.set_enabled.assert_called_once_with(True)
        assert dashboard.instance.send.call_args.args[0] == {"cmd": "setup-done"}
    finally:
        for p in patches:
            p.stop()
        dashboard.autostart.available = autostart_available
    cfg = storage.load_config()[0]
    assert out["ok"] and out["scribe_running"]
    assert cfg["use_cloud"] is True and cfg["local_model"] is False
    assert cfg["hotkey"] == "Ctrl + Alt" and cfg["mic_device"] is None
    assert keystore.resolve_key(cfg) == "gsk_welcome_abcd"
    assert cfg["check_updates"] is True, "cloud users hear about updates"
    print("PASS  Finish setup (cloud) saves choices + key, sets sign-in, tells Scribe.")


def test_finish_setup_local_keeps_local_on():
    _fresh()
    _run(lambda: dashboard.JsApi(welcome=True).finish_setup({
        "use_cloud": False, "local_model": False, "model_size": "medium.en",
        "hotkey": "Ctrl + Win", "mic_device": "Mic A", "start_at_login": False}))
    cfg = storage.load_config()[0]
    assert cfg["use_cloud"] is False and cfg["local_model"] is True
    assert cfg["model_size"] == "medium.en" and cfg["mic_device"] == "Mic A"
    assert cfg["check_updates"] is False, "'On this PC' is offline: no daily GitHub check"
    print("PASS  Finish setup (local) keeps a local model and makes no update checks.")


def test_finish_setup_cloud_without_key_saves_nothing():
    _fresh()
    try:
        _run(lambda: dashboard.JsApi(welcome=True).finish_setup({"use_cloud": True}))
    except storage.StorageError:
        pass
    else:
        raise AssertionError("cloud without a key must be refused")
    assert not os.path.exists(storage.CONFIG_FILE), "a refused setup must not end the first run"
    print("PASS  a refused Finish setup saves nothing.")


def test_setup_status():
    api = dashboard.JsApi()
    with mock.patch.object(dashboard.instance, "request", return_value=None):
        assert api.get_setup_status() == {"running": False}
    reply = {"setup_complete": True, "model": {"state": "ready"}}
    with mock.patch.object(dashboard.instance, "request", return_value=reply):
        assert api.get_setup_status() == dict(reply, running=True)
    print("PASS  setup status says whether Scribe is running, and what it reports.")


def test_open_groq_keys_page():
    with mock.patch.object(dashboard.webbrowser, "open") as op:
        dashboard.JsApi().open_groq_keys_page()
    op.assert_called_once_with("https://console.groq.com/keys")
    print("PASS  'Get a free key' opens Groq's key page (and only that).")


def test_portable_mode_hides_sign_in_and_never_claims_the_vault_failed():
    _fresh()
    data = _run(lambda: dashboard.JsApi().get_initial_data())
    assert data["config"]["start_at_login"] is None
    out = _run(lambda: dashboard.JsApi().save_settings(
        {"use_cloud": True, "groq_api_key": "gsk_portable_1111"}))
    assert out["key_storage"] is None and out["warnings"] == []
    res = _run(lambda: dashboard.JsApi(welcome=True).finish_setup({
        "use_cloud": True, "groq_api_key": "gsk_portable_2222", "start_at_login": True}))
    assert res["warnings"] == []
    print("PASS  portable mode: no sign-in toggle, no false 'vault unavailable'.")


def test_save_settings_reports_partial_failures_as_warnings():
    _fresh()
    patches = _quiet()
    for p in patches:
        p.start()
    try:
        dashboard.autostart.available = lambda: True
        dashboard.autostart.set_enabled.side_effect = storage.StorageError("Registry said no.")
        out = dashboard.JsApi().save_settings({"user_name": "Sam", "start_at_login": True})
        dashboard.instance.send.assert_called_with({"cmd": "reload-config"})
    finally:
        for p in patches:
            p.stop()
        dashboard.autostart.available = autostart_available
    assert out["warnings"] == ["Registry said no."], out["warnings"]
    assert storage.load_config()[0]["user_name"] == "Sam"
    print("PASS  a failed sign-in change is a warning; the rest is saved and applied.")


def test_setup_status_resends_a_lost_setup_done():
    api = dashboard.JsApi(welcome=True)
    api._setup_finished_at = dashboard.time.monotonic() - 5
    with mock.patch.object(dashboard.instance, "request",
                           return_value={"setup_complete": False, "model": {}}), \
         mock.patch.object(dashboard.instance, "send", return_value=True) as send:
        api.get_setup_status()
    send.assert_called_once_with({"cmd": "setup-done"})
    print("PASS  a lost 'setup-done' is sent again.")


def test_finish_setup_stores_the_key_in_the_real_vault():
    import uuid
    _fresh()
    run = uuid.uuid4().hex
    # EVERY service points at a throwaway name - not just the one the test
    # uses - because page_config() looks up all of them.
    throwaway = {service: (f"Scribe-test/{run}/{service}", field)
                 for service, (_target, field) in keystore.SERVICES.items()}
    target = throwaway["groq"][0]
    real = {"read": keystore._vault_read, "write": keystore._vault_write,
            "delete": keystore._vault_delete}

    def throwaway_only(action):
        # The one guard that matters: this test must NEVER read, write or
        # delete the user's real "Scribe/..." credentials, whatever the
        # keystore looks up - checked BEFORE the real call is made.
        def guarded(name, *args):
            assert name.startswith("Scribe-test/"), f"test tried to {action} the real {name!r}"
            return real[action](name, *args)
        return guarded
    try:
        with mock.patch.object(keystore, "_file_mode", return_value=False), \
             mock.patch.dict(keystore.SERVICES, throwaway), \
             mock.patch.object(keystore, "_vault_read", side_effect=throwaway_only("read")), \
             mock.patch.object(keystore, "_vault_write", side_effect=throwaway_only("write")), \
             mock.patch.object(keystore, "_vault_delete", side_effect=throwaway_only("delete")):
            res = _run(lambda: dashboard.JsApi(welcome=True).finish_setup({
                "use_cloud": True, "groq_api_key": "gsk_real_vault_9876",
                "start_at_login": False}))
            assert res["warnings"] == [], res["warnings"]
            assert keystore._vault_read(target) == "gsk_real_vault_9876"
        assert storage.load_config()[0]["groq_api_key"] == ""
    finally:
        for name, _field in throwaway.values():
            keystore._vault_delete(name)
    print("PASS  the welcome saves a cloud key into the real Credential Manager.")


def test_elevenlabs_key_is_kept_and_hidden():
    _fresh()
    api = dashboard.JsApi()
    try:
        _run(lambda: api.save_settings({"use_cloud": True, "cloud_provider": "elevenlabs"}))
    except storage.StorageError as exc:
        assert "ElevenLabs" in str(exc)
    else:
        raise AssertionError("ElevenLabs without its key must be refused")
    _run(lambda: api.save_settings({"groq_api_key": "gsk_only_1234"}))
    try:
        _run(lambda: api.save_settings({"use_cloud": True, "cloud_provider": "elevenlabs"}))
    except storage.StorageError:
        pass
    else:
        raise AssertionError("a Groq key doesn't make ElevenLabs usable")
    out = _run(lambda: api.save_settings({"use_cloud": True, "cloud_provider": "elevenlabs",
                                          "elevenlabs_api_key": "xi_secret_9876"}))
    assert out["elevenlabs_key_hint"] == "ends in 9876" and out["groq_key_hint"] == "ends in 1234"
    assert "xi_secret_9876" not in json.dumps(out) and "gsk_only_1234" not in json.dumps(out)
    cfg = storage.load_config()[0]
    assert cfg["cloud_provider"] == "elevenlabs"
    assert keystore.resolve_key(cfg, "elevenlabs") == "xi_secret_9876"
    out = _run(lambda: api.save_settings({"cloud_provider": "groq", "remove_elevenlabs_key": True}))
    assert out["elevenlabs_key_hint"] == ""
    print("PASS  the ElevenLabs key: required for ElevenLabs, stored, hinted, removable.")


def test_finish_setup_elevenlabs():
    _fresh()
    out = _run(lambda: dashboard.JsApi(welcome=True).finish_setup({
        "use_cloud": True, "cloud_provider": "elevenlabs",
        "elevenlabs_api_key": "xi_welcome_5555", "local_model": True,
        "model_size": "small.en", "hotkey": "Ctrl + Win",
        "mic_device": "(system default)", "start_at_login": False}))
    cfg = storage.load_config()[0]
    assert out["ok"] and cfg["cloud_provider"] == "elevenlabs" and cfg["use_cloud"]
    assert keystore.resolve_key(cfg, "elevenlabs") == "xi_welcome_5555"
    assert keystore.resolve_key(cfg) == ""
    print("PASS  Finish setup (ElevenLabs) saves the service and its key.")


def test_elevenlabs_bridge_calls():
    with mock.patch.object(dashboard.elevenlabs_stream, "check_key",
                           return_value={"status": "ok", "message": "Key works."}) as ck:
        assert dashboard.JsApi().check_elevenlabs_key("xi_1")["status"] == "ok"
        ck.assert_called_once_with("xi_1")
    with mock.patch.object(dashboard.webbrowser, "open") as op:
        dashboard.JsApi().open_elevenlabs_keys_page()
        op.assert_called_once_with(dashboard.elevenlabs_stream.KEYS_URL)
    print("PASS  the page's ElevenLabs key check and key-page link.")


def test_paste_hands_over_only_a_key():
    from unittest import mock
    api = dashboard.JsApi(welcome=True)
    for clip, out in [("  gsk_AbCdEf0123456789xyz  ", "gsk_AbCdEf0123456789xyz"),
                      ("sk_0123456789abcdef0123", "sk_0123456789abcdef0123"),
                      ("my bank password is hunter2", ""), ("short", ""),
                      ("line one\nline two of a private note", ""), ("", "")]:
        with mock.patch("pyperclip.paste", return_value=clip):
            assert api.read_clipboard_key() == out, (clip, out)
    with mock.patch("pyperclip.paste", side_effect=RuntimeError("clipboard busy")):
        assert api.read_clipboard_key() == ""
    print("PASS  Paste hands the page a key-like clipboard only - never other text.")


if __name__ == "__main__":
    test_page_never_sees_the_key()
    test_paste_hands_over_only_a_key()
    test_save_settings_stores_a_new_key_and_hides_it()
    test_cloud_without_a_key_is_refused()
    test_remove_key_and_cloud_off_turns_local_on()
    test_start_at_login_goes_to_the_registry()
    test_check_groq_key_outcomes()
    test_finish_setup_cloud()
    test_finish_setup_local_keeps_local_on()
    test_finish_setup_cloud_without_key_saves_nothing()
    test_setup_status()
    test_open_groq_keys_page()
    test_portable_mode_hides_sign_in_and_never_claims_the_vault_failed()
    test_save_settings_reports_partial_failures_as_warnings()
    test_setup_status_resends_a_lost_setup_done()
    test_finish_setup_stores_the_key_in_the_real_vault()
    test_elevenlabs_key_is_kept_and_hidden()
    test_finish_setup_elevenlabs()
    test_elevenlabs_bridge_calls()
    print("\nAll welcome tests passed.")
