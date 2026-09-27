r"""
Standalone probe for the self-filling Dictionary inside app.py: a delivered
dictation is watched for fixes, learned words apply to the next dictation,
names said often are added, and the switch turns it all off.

Run from the project root:   venv\Scripts\python tests\learning_flow_test.py

Safe: tests/harness.py (temp data folder, mocked mic and model); the fix
watcher is a mock - no other app is read.
"""

import json
import os
import sys
import time
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import import_app_with_mocks  # noqa: E402


def _fresh(app, vocab=None):
    """An empty (or given) Dictionary, learning on, a quiet tray."""
    app.storage.save_vocab(vocab or {"terms": [], "corrections": {}})
    app.load_vocabulary()
    app._TRANSCRIBE_PROMPT_CACHE = None
    app.LEARN_WORDS = True
    app._learning_index = None
    app._notice_last.clear()
    app.tray_icon = mock.MagicMock()
    app._tray_ready = True


def _titles(app):
    return [c.args[1] for c in app.tray_icon.notify.call_args_list]


def test_a_learned_fix_applies_to_the_next_dictation(app):
    _fresh(app)
    assert app._learn_word("Kalshee", "cal she", "fix") is True
    saved = app.storage.load_vocab()[0]
    assert saved["corrections"] == {"cal she": "Kalshee"} and "Kalshee" in saved["terms"]
    assert saved["learned"]["kalshee"]["from"] == "fix"
    assert app.apply_vocabulary("ask cal she now") == "ask Kalshee now", "live, no restart"
    assert "Kalshee" in app.build_transcribe_prompt(), "the prompt cache was rebuilt"
    assert _titles(app) == ["Learned “Kalshee”"], _titles(app)
    assert app._learn_word("Kalshee", "cal she", "fix") is False, "nothing new: no second notice"
    assert _titles(app) == ["Learned “Kalshee”"]
    print("PASS  a fix is learned, saved, used from the next dictation, and announced once.")


def test_a_removed_word_is_never_learned(app):
    _fresh(app, {"terms": [], "corrections": {}, "dismissed": ["kalshee"]})
    assert app._learn_word("Kalshee", "cal she", "fix") is False
    assert app.apply_vocabulary("ask cal she now") == "ask cal she now" and not _titles(app)
    print("PASS  a word you removed is never learned again.")


def test_names_said_often_are_added_quietly(app):
    _fresh(app)
    for i in range(4):
        app._learn_from_dictation(f"I moved money to Webull today, round {i}.")
    assert "Webull" not in app.VOCAB_TERMS, "not yet: said 4 times"
    app._learn_from_dictation("Then I checked Webull again.")
    assert "Webull" in app.VOCAB_TERMS
    assert app.storage.load_vocab()[0]["learned"]["webull"]["from"] == "said"
    assert not _titles(app), "a name said often is added quietly"
    print("PASS  a name said often joins the Dictionary on the 5th time, quietly.")


def test_the_index_starts_from_the_history(app):
    _fresh(app)
    with open(app.LOG_FILE, "a", encoding="utf-8") as f:
        for i in range(4):
            f.write(json.dumps({"timestamp": "2026-09-27T10:00:00", "words": 5, "duration": 1.0,
                                "text": f"Buy more SCHD in the account, round {i}."}) + "\n")
    app._learning_index = None                 # built from the history on first use
    app._learn_from_dictation("And a little more SCHD today.")
    assert "SCHD" in app.VOCAB_TERMS, "4 in the history + this one = 5"
    print("PASS  the name counts start from your whole history.")


def test_a_delivery_is_watched(app):
    _fresh(app)
    app.fix_watcher = mock.MagicMock()
    with mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "deliver"), \
         mock.patch.object(app, "_any_modifier_down", return_value=False):
        app._deliver_job("Ask cal she. ", 321, "App", "app.exe", None, 1.0, log_text="Ask cal she.",
                         released_at=time.monotonic())
    app.fix_watcher.watch.assert_called_once_with(321, "Ask cal she. ")
    # Not watched: the clipboard fallback (nothing was typed into the box)...
    app.fix_watcher.reset_mock()
    with mock.patch.object(app, "restore_target_window", return_value=False), \
         mock.patch.object(app, "_copy_for_user", return_value=True):
        app._deliver_job("Ask. ", 321, "App", "app.exe", None, 1.0, log_text="Ask.")
    assert not app.fix_watcher.watch.called
    # ...and with learning switched off (nothing learned from the words either).
    app.LEARN_WORDS = False
    with mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "deliver"), \
         mock.patch.object(app, "_any_modifier_down", return_value=False), \
         mock.patch.object(app, "_learn_from_dictation") as learn:
        app._deliver_job("Ask. ", 321, "App", "app.exe", None, 1.0, log_text="Ask.")
    assert not app.fix_watcher.watch.called and not learn.called
    app.LEARN_WORDS = True
    app.fix_watcher = None
    print("PASS  a typed dictation is watched for fixes; clipboard fallbacks and 'off' aren't.")


def test_dictionary_fixes_are_counted_in_the_history(app):
    _fresh(app, {"terms": ["Kalshee"], "corrections": {"cal she": "Kalshee"}})
    with mock.patch.object(app, "_transcribe_take", return_value=("ask cal she and cal she", "local")), \
         mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "deliver"), \
         mock.patch.object(app, "_any_modifier_down", return_value=False):
        app.process_audio(np.zeros(16000, dtype=np.float32), None)
    last = json.loads(open(app.LOG_FILE, encoding="utf-8").read().splitlines()[-1])
    assert last["text"] == "Ask Kalshee and Kalshee" and last["fixed"] == 2, last
    with mock.patch.object(app, "_transcribe_take", return_value=("nothing to fix here", "local")), \
         mock.patch.object(app, "restore_target_window", return_value=True), \
         mock.patch.object(app, "deliver"), \
         mock.patch.object(app, "_any_modifier_down", return_value=False):
        app.process_audio(np.zeros(16000, dtype=np.float32), None)
    last = json.loads(open(app.LOG_FILE, encoding="utf-8").read().splitlines()[-1])
    assert "fixed" not in last, last
    print("PASS  the history counts the words the Dictionary fixed (for Insights).")


def test_your_own_words_come_first(app):
    _fresh(app, {"terms": ["Webull", "Sam", "Kalshee"], "corrections": {},
                 "learned": {"webull": {"from": "said", "at": "2026-09-27T10:00:00"},
                             "kalshee": {"from": "fix", "at": "2026-09-27T11:00:00"}}})
    assert app._ranked_terms() == ["Webull", "Kalshee", "Sam"], app._ranked_terms()
    many = {"terms": [f"Word{i}" for i in range(60)] + ["Sam"], "corrections": {},
            "learned": {f"word{i}": {"from": "said", "at": "2026-09-27T10:00:00"} for i in range(60)}}
    _fresh(app, many)
    keys = app.elevenlabs_stream.keyterms(app._ranked_terms(), "")
    assert "Sam" in keys and len(keys) == 50, "learned words never push yours out"
    print("PASS  your own words come first: ElevenLabs' 50 priority words, polish, Whisper.")


if __name__ == "__main__":
    app = import_app_with_mocks()
    test_a_learned_fix_applies_to_the_next_dictation(app)
    test_a_removed_word_is_never_learned(app)
    test_names_said_often_are_added_quietly(app)
    test_the_index_starts_from_the_history(app)
    test_a_delivery_is_watched(app)
    test_dictionary_fixes_are_counted_in_the_history(app)
    test_your_own_words_come_first(app)
    print("\nAll learning flow tests passed.")
