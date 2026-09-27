r"""
Standalone probe for keystore.py - where the API key lives.

Run from the project root:   venv\Scripts\python tests\keystore_test.py

Safe: the file-mode checks run against a temp SCRIBE_DATA_DIR, the vault is
simulated for the migration checks, and the one real Credential Manager
check writes a uniquely named throwaway credential and deletes it.
"""

import os
import sys
import tempfile
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="scribe-key-test-")
os.environ["SCRIBE_DATA_DIR"] = TMP
for name in ("storage", "keystore"):
    sys.modules.pop(name, None)
import storage   # noqa: E402
import keystore  # noqa: E402


def _simulated_vault():
    """Patch the three vault calls with a dict, and turn file mode off."""
    vault = {}
    return vault, [
        mock.patch.object(keystore, "_file_mode", return_value=False),
        mock.patch.object(keystore, "_vault_write", side_effect=lambda t, s: vault.__setitem__(t, s)),
        mock.patch.object(keystore, "_vault_read", side_effect=lambda t: vault.get(t)),
        mock.patch.object(keystore, "_vault_delete", side_effect=lambda t: vault.pop(t, None)),
    ]


def test_file_mode_under_override():
    assert storage.USING_OVERRIDE and keystore._file_mode()
    assert keystore.get_key() == ""
    assert keystore.set_key("  gsk_file_key  ") == "file"
    cfg, _ = storage.load_config()
    assert cfg["groq_api_key"] == "gsk_file_key"
    assert keystore.resolve_key(cfg) == "gsk_file_key"
    keystore.delete_key()
    assert storage.load_config()[0]["groq_api_key"] == ""
    print("PASS  with SCRIBE_DATA_DIR set, the key lives in that folder's config.json.")


def test_blank_key_is_refused():
    try:
        keystore.set_key("   ")
    except ValueError:
        pass
    else:
        raise AssertionError("a blank key must be refused")
    print("PASS  a blank key is refused.")


def test_key_hint():
    assert keystore.key_hint("gsk_abcdefgh1a2b") == "ends in 1a2b"
    assert keystore.key_hint("short") == "saved"
    assert keystore.key_hint("") == ""
    print("PASS  key_hint shows only the last four characters.")


def test_vault_mode_set_get_delete():
    vault, patches = _simulated_vault()
    storage.save_config_changes({"groq_api_key": "gsk_old_copy"})
    for p in patches:
        p.start()
    try:
        assert keystore.set_key("gsk_vault_key") == "vault"
        assert vault[keystore.TARGET] == "gsk_vault_key"
        assert storage.load_config()[0]["groq_api_key"] == "", \
            "a stray config.json copy is cleared once the vault holds the key"
        assert keystore.get_key() == "gsk_vault_key"
        keystore.delete_key()
        assert keystore.TARGET not in vault and keystore.get_key() == ""
    finally:
        for p in patches:
            p.stop()
    print("PASS  with the vault available, the key goes there and config.json stays clean.")


def test_vault_refusal_falls_back_to_the_file():
    with mock.patch.object(keystore, "_file_mode", return_value=False), \
         mock.patch.object(keystore, "_vault_write", side_effect=keystore.VaultError("denied")), \
         mock.patch.object(keystore, "_vault_read", return_value=None):
        assert keystore.set_key("gsk_fallback") == "file"
    assert storage.load_config()[0]["groq_api_key"] == "gsk_fallback"
    storage.save_config_changes({"groq_api_key": ""})
    print("PASS  if Credential Manager refuses, the key is kept in config.json (never lost).")


def test_migration_moves_key_into_vault():
    vault, patches = _simulated_vault()
    keystore._vault_failed = False
    storage.save_config_changes({"groq_api_key": "gsk_move_me"})
    for p in patches:
        p.start()
    try:
        cfg, _ = storage.load_config()
        assert keystore.migrate_from_config(cfg) == []
        assert vault[keystore.TARGET] == "gsk_move_me"
        after, _ = storage.load_config()
        assert after["groq_api_key"] == ""
        assert keystore.resolve_key(after) == "gsk_move_me"
        assert keystore.migrate_from_config(after) == [], "nothing left to move"
    finally:
        for p in patches:
            p.stop()
    bak = storage.read_json(storage.CONFIG_FILE + ".bak")[1]
    assert bak["groq_api_key"] == "", "the .bak must not keep a copy of the key"
    print("PASS  a key in config.json moves into the vault; config.json and its .bak are cleared.")


def test_migration_keeps_key_when_vault_fails():
    keystore._vault_failed = False
    storage.save_config_changes({"groq_api_key": "gsk_stay"})
    with mock.patch.object(keystore, "_file_mode", return_value=False), \
         mock.patch.object(keystore, "_vault_write", side_effect=keystore.VaultError("nope")), \
         mock.patch.object(keystore, "_vault_read", return_value=None):
        cfg, _ = storage.load_config()
        assert [n["key"] for n in keystore.migrate_from_config(cfg)] == ["key_store_failed"]
        assert storage.load_config()[0]["groq_api_key"] == "gsk_stay"
        assert keystore.migrate_from_config(cfg) == [], "said once per run, not on every reload"
    keystore._vault_failed = False
    storage.save_config_changes({"groq_api_key": ""})
    print("PASS  a refusing vault keeps the key where it is, and says so once.")


def test_migration_needs_a_matching_readback():
    keystore._vault_failed = False
    storage.save_config_changes({"groq_api_key": "gsk_verify"})
    with mock.patch.object(keystore, "_file_mode", return_value=False), \
         mock.patch.object(keystore, "_vault_write"), \
         mock.patch.object(keystore, "_vault_read", return_value="something else"):
        cfg, _ = storage.load_config()
        assert [n["key"] for n in keystore.migrate_from_config(cfg)] == ["key_store_failed"]
    assert storage.load_config()[0]["groq_api_key"] == "gsk_verify"
    keystore._vault_failed = False
    storage.save_config_changes({"groq_api_key": ""})
    print("PASS  the key is only removed from config.json after a verified read-back.")


def test_real_credential_manager_roundtrip():
    target = f"Scribe-test/{uuid.uuid4().hex}"
    try:
        assert keystore._vault_read(target) is None
        keystore._vault_write(target, "gsk_tëst_ключ")
        assert keystore._vault_read(target) == "gsk_tëst_ключ"
        keystore._vault_delete(target)
        assert keystore._vault_read(target) is None
        keystore._vault_delete(target)          # deleting twice is fine
    finally:
        try:
            keystore._vault_delete(target)
        except keystore.VaultError:
            pass
    print("PASS  the real Credential Manager stores, reads back and deletes a secret.")


def test_services_are_kept_apart():
    vault, patches = _simulated_vault()
    for p in patches:
        p.start()
    try:
        assert keystore.set_key("gsk_groq_1111") == "vault"
        assert keystore.set_key("xi_eleven_2222", "elevenlabs") == "vault"
        assert vault == {"Scribe/groq": "gsk_groq_1111", "Scribe/elevenlabs": "xi_eleven_2222"}
        assert keystore.get_key("elevenlabs") == "xi_eleven_2222"
        keystore.delete_key("elevenlabs")
        assert keystore.get_key("elevenlabs") == "" and keystore.get_key() == "gsk_groq_1111"
    finally:
        for p in patches:
            p.stop()
    print("PASS  the Groq and ElevenLabs keys live under their own names.")


def test_file_mode_keeps_each_service_in_its_own_field():
    assert keystore.set_key("xi_file_key", "elevenlabs") == "file"
    cfg, _ = storage.load_config()
    assert cfg["elevenlabs_api_key"] == "xi_file_key" and cfg["groq_api_key"] == ""
    assert keystore.resolve_key(cfg, "elevenlabs") == "xi_file_key"
    keystore.delete_key("elevenlabs")
    assert storage.load_config()[0]["elevenlabs_api_key"] == ""
    print("PASS  file mode: each service's key has its own config.json field.")


def test_migration_moves_every_service():
    storage.save_config_changes({"groq_api_key": "gsk_old_aaaa", "elevenlabs_api_key": "xi_old_bbbb"})
    vault, patches = _simulated_vault()
    for p in patches:
        p.start()
    try:
        notices = keystore.migrate_from_config(storage.load_config()[0])
    finally:
        for p in patches:
            p.stop()
    cfg = storage.load_config()[0]
    assert notices == [] and cfg["groq_api_key"] == "" and cfg["elevenlabs_api_key"] == ""
    assert vault == {"Scribe/groq": "gsk_old_aaaa", "Scribe/elevenlabs": "xi_old_bbbb"}
    print("PASS  migration moves every service's key into the vault.")


if __name__ == "__main__":
    test_file_mode_under_override()
    test_blank_key_is_refused()
    test_key_hint()
    test_vault_mode_set_get_delete()
    test_vault_refusal_falls_back_to_the_file()
    test_migration_moves_key_into_vault()
    test_migration_keeps_key_when_vault_fails()
    test_migration_needs_a_matching_readback()
    test_services_are_kept_apart()
    test_file_mode_keeps_each_service_in_its_own_field()
    test_migration_moves_every_service()
    test_real_credential_manager_roundtrip()
    print("\nAll keystore tests passed.")
