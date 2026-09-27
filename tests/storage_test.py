r"""
Standalone probe for storage.py - Scribe's data folder and crash-safe file IO.

Run from the project root:   venv\Scripts\python tests\storage_test.py

Every check runs against a throwaway SCRIBE_DATA_DIR, never your real data.
"""

import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="scribe-storage-test-")
os.environ["SCRIBE_DATA_DIR"] = TMP
sys.modules.pop("storage", None)
import storage  # noqa: E402  - must import AFTER the env var is set


def test_data_dir_override():
    assert storage.DATA_DIR == os.path.abspath(TMP), storage.DATA_DIR
    assert storage.USING_OVERRIDE is True
    assert storage.CONFIG_FILE == os.path.join(TMP, "config.json")
    assert storage.LOG_FILE == os.path.join(TMP, "dictation_log.jsonl")
    print("PASS  SCRIBE_DATA_DIR points every data file at the override folder.")


def test_atomic_write_leaves_no_temp_files():
    path = os.path.join(TMP, "a.json")
    storage.atomic_write_json(path, {"x": 1})
    storage.atomic_write_json(path, {"x": 2})
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"x": 2}
    leftovers = [n for n in os.listdir(TMP) if n.endswith(".tmp")]
    assert not leftovers, leftovers
    print("PASS  atomic_write_json replaces the file and leaves no temp files.")


def test_atomic_write_retries_while_file_is_open():
    # Windows refuses os.replace onto a file another handle has open - the
    # dashboard reading the log at the wrong instant. The write must wait it out.
    path = os.path.join(TMP, "held.txt")
    storage.atomic_write_text(path, "old")
    handle = open(path, "r", encoding="utf-8")
    threading.Timer(0.3, handle.close).start()
    t0 = time.monotonic()
    storage.atomic_write_text(path, "new")
    waited = time.monotonic() - t0
    with open(path, encoding="utf-8") as f:
        assert f.read() == "new"
    print(f"PASS  atomic_write_text waited out an open handle ({waited:.2f}s) and succeeded.")


def test_read_json_statuses():
    missing = os.path.join(TMP, "nope.json")
    assert storage.read_json(missing) == ("missing", None)
    bom = os.path.join(TMP, "bom.json")
    with open(bom, "w", encoding="utf-8-sig") as f:
        f.write('{"a": 1}')
    assert storage.read_json(bom) == ("ok", {"a": 1}), storage.read_json(bom)
    bad = os.path.join(TMP, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert storage.read_json(bad)[0] == "corrupt"
    latin = os.path.join(TMP, "latin.json")
    with open(latin, "wb") as f:
        f.write(b'{"a": "caf\xe9"}')          # invalid UTF-8
    assert storage.read_json(latin)[0] == "corrupt"
    print("PASS  read_json tells missing / ok (BOM tolerated) / corrupt apart.")


def test_jsonl_append_read_rewrite():
    path = os.path.join(TMP, "log.jsonl")
    storage.append_jsonl(path, {"text": "one"})
    storage.append_jsonl(path, {"text": "two"})
    with open(path, "a", encoding="utf-8") as f:
        f.write("not json\n[1, 2]\n\n")       # junk a reader must skip
    storage.append_jsonl(path, {"text": "three"})
    assert [e["text"] for e in storage.read_jsonl(path)] == ["one", "two", "three"]

    def drop_last_json_line(lines):
        return lines[:-1]
    assert storage.rewrite_lines(path, drop_last_json_line) is True
    assert [e["text"] for e in storage.read_jsonl(path)] == ["one", "two"]
    assert storage.rewrite_lines(path, lambda lines: None) is False
    assert storage.rewrite_lines(os.path.join(TMP, "absent.jsonl"), lambda l: l) is False
    print("PASS  JSON Lines: append, read (junk skipped), atomic rewrite.")


def test_log_error_writes_traceback_and_caps_size():
    try:
        raise ValueError("boom")
    except ValueError as exc:
        text = storage.log_error("unit-test", exc)
    assert "unit-test" in text and "ValueError" in text and "Traceback" in text
    storage.log_error("note", message="just a message")
    with open(storage.ERROR_LOG, encoding="utf-8") as f:
        body = f.read()
    assert "boom" in body and "just a message" in body
    old_cap = storage.ERROR_LOG_MAX_BYTES
    storage.ERROR_LOG_MAX_BYTES = 2000
    try:
        for i in range(200):
            storage.log_error("flood", message="x" * 50)
        assert os.path.getsize(storage.ERROR_LOG) < 2000 + 200
    finally:
        storage.ERROR_LOG_MAX_BYTES = old_cap
    print("PASS  log_error records tracebacks and keeps the log size-capped.")


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _reset_data_dir():
    for name in os.listdir(TMP):
        p = os.path.join(TMP, name)
        if os.path.isfile(p):
            os.remove(p)


def test_validate_config():
    cfg, bad = storage.validate_config({
        "hotkey": ["Ctrl", "Win"],          # wrong type -> default, flagged
        "use_cloud": "false",               # string bool -> False
        "sound_cues": "yes",                # -> True
        "model_size": "gigantic",           # not a choice -> default, flagged
        "mic_device": "",                   # empty -> None
        "future_setting": 42,               # unknown -> preserved
    })
    assert cfg["hotkey"] == storage.DEFAULT_CONFIG["hotkey"]
    assert cfg["use_cloud"] is False and cfg["sound_cues"] is True
    assert cfg["model_size"] == storage.DEFAULT_CONFIG["model_size"]
    assert cfg["mic_device"] is None and cfg["future_setting"] == 42
    assert sorted(bad) == ["hotkey", "model_size"], bad
    print("PASS  validate_config coerces, resets only bad keys, keeps unknown keys.")


def test_validate_vocab():
    vocab, skipped = storage.validate_vocab({
        "terms": ["Vercel", "", 7, "  Kalshi "],
        "corrections": {"versel": "Vercel", "x": None, "": "Y", "ok": "  "},
        "dismissed": ["Kaoshi", 3],
    })
    assert vocab["terms"] == ["Vercel", "Kalshi"], vocab
    assert vocab["corrections"] == {"versel": "Vercel"}, vocab
    assert vocab["dismissed"] == ["kaoshi"], vocab
    assert skipped == 6, skipped
    print("PASS  validate_vocab keeps only real text entries.")


def test_corrupt_config_restores_backup():
    _reset_data_dir()
    storage.save_config_changes({"groq_api_key": "gsk_keep_me", "hotkey": "Ctrl + Alt"})
    assert os.path.exists(storage.CONFIG_FILE + ".bak")
    _write(storage.CONFIG_FILE, '{"groq_api_key": "gsk_keep_me",')   # truncated
    cfg, notices = storage.load_config()
    assert cfg["groq_api_key"] == "gsk_keep_me" and cfg["hotkey"] == "Ctrl + Alt"
    assert [n["key"] for n in notices] == ["settings_recovered"], notices
    kept = [n for n in os.listdir(TMP) if n.startswith("config.corrupt-")]
    assert len(kept) == 1, kept
    assert storage.read_json(storage.CONFIG_FILE)[0] == "ok"   # restored on disk
    print("PASS  a damaged config is kept aside and the last good copy restored.")


def test_corrupt_config_without_backup_resets():
    _reset_data_dir()
    _write(storage.CONFIG_FILE, "garbage")
    cfg, notices = storage.load_config()
    assert cfg == storage.DEFAULT_CONFIG
    assert [n["key"] for n in notices] == ["settings_reset"], notices
    assert "config.corrupt-" in notices[0]["message"]
    print("PASS  a damaged config with no backup falls back to defaults (file kept).")


def test_save_never_overwrites_corrupt_file():
    # The old dashboard bug: read -> defaults -> write defaults over the file.
    _reset_data_dir()
    _write(storage.CONFIG_FILE, '{"groq_api_key": "gsk_lost?"')   # corrupt
    storage.save_config_changes({"seen_milestones": ["count:10"]})
    kept = [n for n in os.listdir(TMP) if n.startswith("config.corrupt-")]
    assert len(kept) == 1, "the damaged original must be preserved"
    with open(os.path.join(TMP, kept[0]), encoding="utf-8") as f:
        assert "gsk_lost?" in f.read()
    print("PASS  saving after corruption preserves the damaged original.")


def test_two_corruptions_in_one_second_both_kept():
    _reset_data_dir()
    _write(storage.CONFIG_FILE, "bad one")
    storage.load_config()
    _write(storage.CONFIG_FILE, "bad two")
    storage.load_config()
    kept = sorted(n for n in os.listdir(TMP) if n.startswith("config.corrupt-"))
    assert len(kept) == 2, kept
    print("PASS  two damaged copies made in the same second are both kept.")


def test_save_config_rejects_invalid_values():
    _reset_data_dir()
    try:
        storage.save_config_changes({"hotkey": "Ctrl + Q"})
    except storage.StorageError as exc:
        assert "hotkey" in str(exc)
    else:
        raise AssertionError("expected StorageError for an invalid hotkey")
    print("PASS  save_config_changes rejects an invalid value with a clear error.")


def test_invalid_keys_notice():
    _reset_data_dir()
    _write(storage.CONFIG_FILE, '{"hotkey": ["Ctrl", "Win"], "paste_mode": false}')
    cfg, notices = storage.load_config()
    assert cfg["paste_mode"] is False
    assert [n["key"] for n in notices] == ["settings_invalid"], notices
    print("PASS  a wrong-typed setting resets alone and is reported.")


def test_vocab_recovery_and_save():
    _reset_data_dir()
    vocab, notices = storage.load_vocab()
    assert vocab == {"terms": [], "corrections": {}, "dismissed": [], "learned": {}} and not notices
    assert not os.path.exists(storage.VOCAB_FILE), "loading must not create the file"
    storage.save_vocab({"terms": ["Vercel"], "corrections": {"versel": "Vercel"}})
    _write(storage.VOCAB_FILE, '{"terms": ["Vercel"')
    vocab, notices = storage.load_vocab()
    assert vocab["terms"] == ["Vercel"] and notices[0]["key"] == "vocab_recovered"
    print("PASS  vocabulary: missing is fine, damaged is restored from backup.")


def test_migrate_legacy_files():
    legacy = tempfile.mkdtemp(prefix="scribe-legacy-")
    target = tempfile.mkdtemp(prefix="scribe-target-")
    _write(os.path.join(legacy, "config.json"), '{"hotkey": "Ctrl + Alt"}')
    _write(os.path.join(legacy, "dictation_log.jsonl"), '{"text": "hi"}\n')
    _write(os.path.join(target, "dictation_log.jsonl"), '{"text": "already here"}\n')
    os.makedirs(os.path.join(legacy, "app_icons"))
    _write(os.path.join(legacy, "app_icons", "a.png"), "png")
    old = (storage.DATA_DIR, storage.USING_OVERRIDE)
    storage.DATA_DIR, storage.USING_OVERRIDE = target, False
    try:
        moved = storage.migrate_legacy_files(legacy)
    finally:
        storage.DATA_DIR, storage.USING_OVERRIDE = old
    assert sorted(moved) == ["app_icons/", "config.json"], moved
    assert not os.path.exists(os.path.join(legacy, "config.json"))
    assert os.path.exists(os.path.join(target, "config.json"))
    assert os.path.exists(os.path.join(target, "app_icons", "a.png"))
    # present in both -> the app-folder copy is left alone, nothing clobbered
    assert os.path.exists(os.path.join(legacy, "dictation_log.jsonl"))
    with open(os.path.join(target, "dictation_log.jsonl"), encoding="utf-8") as f:
        assert "already here" in f.read()
    print("PASS  migration moves files, never clobbers, copies app_icons/.")


def test_migrate_keeps_copy_when_original_is_locked():
    legacy = tempfile.mkdtemp(prefix="scribe-legacy-")
    target = tempfile.mkdtemp(prefix="scribe-target-")
    _write(os.path.join(legacy, "vocabulary.json"), '{"terms": ["X"]}')
    old = (storage.DATA_DIR, storage.USING_OVERRIDE)
    storage.DATA_DIR, storage.USING_OVERRIDE = target, False
    real_remove = os.remove

    def locked_remove(p):
        if p.endswith("vocabulary.json") and legacy in p:
            raise PermissionError("in use")
        return real_remove(p)
    os.remove = locked_remove
    try:
        storage.migrate_legacy_files(legacy)
    finally:
        os.remove = real_remove
        storage.DATA_DIR, storage.USING_OVERRIDE = old
    assert os.path.exists(os.path.join(target, "vocabulary.json")), "copy must exist"
    assert os.path.exists(os.path.join(legacy, "vocabulary.json")), "original kept"
    print("PASS  a locked original is left in place; the new copy is still used.")


def test_migration_disabled_under_override():
    legacy = tempfile.mkdtemp(prefix="scribe-legacy-")
    _write(os.path.join(legacy, "config.json"), "{}")
    assert storage.migrate_legacy_files(legacy) == []
    assert os.path.exists(os.path.join(legacy, "config.json"))
    print("PASS  migration never runs against an override (test) folder.")


# --- final-review regressions -------------------------------------------------

def test_unreadable_config_is_never_overwritten():
    # A file briefly locked by another program (an antivirus scan) must not be
    # mistaken for "missing" and replaced with defaults.
    _reset_data_dir()
    storage.save_config_changes({"groq_api_key": "gsk_locked_but_real"})
    real = storage.read_json
    storage.read_json = lambda path: ("unreadable", None) if path == storage.CONFIG_FILE else real(path)
    try:
        cfg, notices = storage.load_config()
        assert [n["key"] for n in notices] == ["settings_unreadable"], notices
        try:
            storage.save_config_changes({"seen_milestones": ["count:10"]})
        except storage.StorageError:
            pass
        else:
            raise AssertionError("must refuse to write over a file it couldn't read")
    finally:
        storage.read_json = real
    assert storage.load_config()[0]["groq_api_key"] == "gsk_locked_but_real"
    print("PASS  a config that can't be read right now is reported, never overwritten.")


def test_invalid_settings_reported_once():
    _reset_data_dir()
    _write(storage.CONFIG_FILE, '{"hotkey": "Ctrl + Q", "groq_api_key": "k"}')
    _cfg, first = storage.load_config()
    _cfg, second = storage.load_config()
    assert [n["key"] for n in first] == ["settings_invalid"], first
    assert second == [], "the reset must be saved so it is reported only once"
    assert storage.read_json(storage.CONFIG_FILE)[1]["groq_api_key"] == "k"
    _write(storage.VOCAB_FILE, '{"terms": ["ok", 7], "corrections": {}}')
    _v, first = storage.load_vocab()
    _v, second = storage.load_vocab()
    assert [n["key"] for n in first] == ["vocab_invalid"] and second == []
    print("PASS  invalid settings/vocabulary are reported once, then saved cleaned.")


def test_migration_records_conflicts_and_failures():
    legacy = tempfile.mkdtemp(prefix="scribe-legacy-")
    target = tempfile.mkdtemp(prefix="scribe-target-")
    _write(os.path.join(legacy, "dictation_log.jsonl"), "{}\n")
    _write(os.path.join(target, "dictation_log.jsonl"), "{}\n")
    _write(os.path.join(legacy, "config.json"), "{}")
    old = (storage.DATA_DIR, storage.USING_OVERRIDE)
    storage.DATA_DIR, storage.USING_OVERRIDE = target, False
    real_copy = storage.shutil.copy2

    def failing_copy(src, dst, *a, **k):
        if src.endswith("config.json"):
            raise OSError("disk full")
        return real_copy(src, dst, *a, **k)
    storage.shutil.copy2 = failing_copy
    try:
        storage.migrate_legacy_files(legacy)
    finally:
        storage.shutil.copy2 = real_copy
        storage.DATA_DIR, storage.USING_OVERRIDE = old
    assert storage.MIGRATION_PROBLEMS == {"conflicts": ["dictation_log.jsonl"],
                                          "failed": ["config.json"]}, storage.MIGRATION_PROBLEMS
    print("PASS  migration records files left in both places and files it couldn't move.")

def test_rewrite_lines_survives_undecodable_file():
    p = os.path.join(TMP, "bad.jsonl")
    with open(p, "wb") as f:
        f.write(b'{"text": "caf\xe9"}\n')
    assert storage.rewrite_lines(p, lambda lines: lines[:-1]) is False
    print("PASS  an undecodable log is left alone instead of crashing a rewrite.")

def test_read_jsonl_from_skips_unfinished_line():
    path = os.path.join(storage.DATA_DIR, "from.jsonl")
    with open(path, "wb") as f:
        f.write(b'\xef\xbb\xbf{"a": 1}\r\n{broken\r\n{"b": 2}\n{"c": 3')   # BOM, CRLF, bad, unfinished
    entries, offset = storage.read_jsonl_from(path, 0)
    assert entries == [{"a": 1}, {"b": 2}], entries
    with open(path, "ab") as f:
        f.write(b'}\n')                                               # the write completes
    more, offset2 = storage.read_jsonl_from(path, offset)
    assert more == [{"c": 3}] and offset2 == os.path.getsize(path), (more, offset2)
    assert storage.read_jsonl_from(os.path.join(storage.DATA_DIR, "nope.jsonl"), 0) == ([], 0)
    print("PASS  read_jsonl_from reads only complete new lines.")


def test_cloud_provider_is_validated():
    assert storage.clean_config_value("cloud_provider", "elevenlabs") == "elevenlabs"
    assert storage.clean_config_value("cloud_provider", "groq") == "groq"
    for bad in ("openai", "", None, 3):
        try:
            storage.clean_config_value("cloud_provider", bad)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"{bad!r} should be refused")
    assert storage.DEFAULT_CONFIG["cloud_provider"] == "groq"
    print("PASS  cloud_provider accepts groq / elevenlabs only; groq by default.")


def test_a_last_line_without_a_newline():
    path = os.path.join(storage.DATA_DIR, "hand-edited.jsonl")
    with open(path, "wb") as f:
        f.write(b'{"a": 1}\n{"b": 2}')                 # a hand edit: no final newline
    entries, offset = storage.read_jsonl_from(path, 0)
    assert entries == [{"a": 1}, {"b": 2}] and offset == os.path.getsize(path), (entries, offset)
    storage.append_jsonl(path, {"c": 3})                  # starts a new line first
    assert storage.read_jsonl(path) == [{"a": 1}, {"b": 2}, {"c": 3}]
    more, _ = storage.read_jsonl_from(path, offset)
    assert more == [{"c": 3}], more
    print("PASS  a complete last line without a newline is read; appends start a new line.")


def test_theme_is_validated():
    assert storage.DEFAULT_CONFIG["theme"] == "system"
    for ok in ("system", "light", "dark"):
        assert storage.clean_config_value("theme", ok) == ok
    for bad in ("blue", "", None, 1):
        try:
            storage.clean_config_value("theme", bad)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"{bad!r} should be refused")
    print("PASS  theme accepts system / light / dark only; system by default.")


def test_polish_style_is_validated():
    assert storage.DEFAULT_CONFIG["polish_style"] == "full"
    for ok in ("full", "light"):
        assert storage.clean_config_value("polish_style", ok) == ok
    for bad in ("heavy", "", None, True):
        try:
            storage.clean_config_value("polish_style", bad)
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"{bad!r} should be refused")
    print("PASS  polish_style accepts full / light only; full by default.")


def test_vocab_keeps_learned_words():
    raw = {"terms": ["Kalshee", "Webull"], "corrections": {"cal she": "Kalshee"},
           "learned": {"kalshee": {"from": "fix", "at": "2026-09-27T10:00:00", "wrong": "cal she"},
                       "Webull": {"from": "said", "at": "2026-09-27T11:00:00"},
                       "odd": {"from": "magic", "at": "x"},             # unknown source
                       "worse": "not a record",
                       "": {"from": "said", "at": "x"}}}
    vocab, skipped = storage.validate_vocab(raw)
    assert vocab["learned"] == {
        "kalshee": {"from": "fix", "at": "2026-09-27T10:00:00", "wrong": "cal she"},
        "webull": {"from": "said", "at": "2026-09-27T11:00:00"}}, vocab["learned"]
    assert skipped == 3, skipped
    assert storage.validate_vocab({"terms": []})[0]["learned"] == {}, "older files: none"
    # Saved and read back; the file carries "learned" only when there is some.
    storage.save_vocab(vocab)
    again, _ = storage.load_vocab()
    assert again["learned"] == vocab["learned"]
    storage.save_vocab(dict(vocab, learned={}))
    on_disk = json.load(open(storage.VOCAB_FILE, encoding="utf-8"))
    assert "learned" not in on_disk
    print("PASS  vocabulary.json keeps what Scribe learned (and why); bad entries are dropped.")


if __name__ == "__main__":
    test_data_dir_override()
    test_atomic_write_leaves_no_temp_files()
    test_atomic_write_retries_while_file_is_open()
    test_read_json_statuses()
    test_jsonl_append_read_rewrite()
    test_log_error_writes_traceback_and_caps_size()
    test_validate_config()
    test_validate_vocab()
    test_corrupt_config_restores_backup()
    test_corrupt_config_without_backup_resets()
    test_save_never_overwrites_corrupt_file()
    test_two_corruptions_in_one_second_both_kept()
    test_save_config_rejects_invalid_values()
    test_invalid_keys_notice()
    test_vocab_recovery_and_save()
    test_migrate_legacy_files()
    test_migrate_keeps_copy_when_original_is_locked()
    test_migration_disabled_under_override()
    test_unreadable_config_is_never_overwritten()
    test_invalid_settings_reported_once()
    test_migration_records_conflicts_and_failures()
    test_rewrite_lines_survives_undecodable_file()
    test_read_jsonl_from_skips_unfinished_line()
    test_cloud_provider_is_validated()
    test_a_last_line_without_a_newline()
    test_theme_is_validated()
    test_polish_style_is_validated()
    test_vocab_keeps_learned_words()
    print("\nAll storage tests passed.")
