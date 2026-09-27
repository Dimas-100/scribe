r"""
Standalone probe for learning.py - the Dictionary that fills itself: the
word statistics over your history, the names worth keeping (auto_terms),
spotting a mishearing of a known word (is_variant), the words you fixed
(find_fixes / find_region), and adding or removing a learned word.

Run from the project root:   venv\Scripts\python tests\learning_test.py

Safe: pure functions on made-up words - no files, no windows, no network.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import learning  # noqa: E402


def _vocab(**kw):
    v = {"terms": [], "corrections": {}, "dismissed": [], "learned": {}}
    v.update(kw)
    return v


# --- The word statistics (moved here from dashboard.py) ----------------------

def test_suggestions_keep_the_users_spelling():
    entries = [{"text": t} for t in ["I bet on Kalshee today.", "Kalshee odds moved.",
                                     "check kalshee again", "Is Kalshee up?"]]
    s = learning.suggest_vocabulary(entries, [], {}, [])
    assert [x["word"] for x in s] == ["Kalshee"], s
    print("PASS  a suggestion keeps its most common mid-sentence spelling.")


def test_unicode_words_are_one_word():
    entries = [{"text": "Call José now."}, {"text": "José said hi."}, {"text": "ask José"}]
    s = learning.suggest_vocabulary(entries, [], {}, [])
    assert "José" in [x["word"] for x in s], s
    print("PASS  accented words stay whole.")


def test_names_bypass_the_common_word_list():
    assert "david" in learning.COMMON_WORDS
    entries = [{"text": f"I met David {i} times."} for i in range(3)]
    entries += [{"text": "the apple was red"}] * 3
    words = [x["word"] for x in learning.suggest_vocabulary(entries, [], {}, [])]
    assert "David" in words and "apple" not in words, words
    print("PASS  mostly-capitalized common words (names) count as names; everyday ones don't.")


def test_calendar_and_language_words_are_not_names():
    entries = [{"text": "See you on Monday at noon."}, {"text": "It ships next Friday."},
               {"text": "We met on Monday again."}, {"text": "Friday works for me."},
               {"text": "back in January then."}, {"text": "since January now."},
               {"text": "my English teacher said"}, {"text": "in English please"},
               {"text": "I met David 1 time."}, {"text": "I met David 2 times."}]
    words = [x["word"] for x in learning.suggest_vocabulary(entries * 2, [], {}, [])]
    assert not {"Monday", "Friday", "January", "English"} & set(words), words
    assert "David" in words, words
    print("PASS  weekdays, months and languages aren't names; people are.")


def test_a_spelling_tie_goes_to_the_capitalized_form():
    entries = [{"text": t} for t in ["we met Zeno there", "we met zeno again",
                                     "ask Zeno now", "ask zeno later"]]
    s = learning.suggest_vocabulary(entries * 2, [], {}, [])
    assert [x["word"] for x in s] == ["Zeno"], s
    print("PASS  a spelling tie goes to the Capitalized form.")


# --- Part 2: names you say often ---------------------------------------------

def _index(lines):
    idx = learning.SuggestionIndex()
    idx.add([{"text": t} for t in lines])
    return idx


SAID = ([f"I moved money to Webull today, round {i}." for i in range(10)] +   # 2x Weibo
        [f"Buy more SCHD in the account, round {i}." for i in range(6)] +
        [f"The dashboard looks better now, round {i}." for i in range(8)] +   # everyday word
        [f"Ask Weibo about it, round {i}." for i in range(5)] +                # Webull, misheard
        [f"Check Kalshee odds, round {i}." for i in range(3)])                 # said too rarely


def test_auto_terms_keep_names_and_skip_the_rest():
    idx = _index(SAID)
    assert sorted(learning.auto_terms(idx, _vocab())) == ["SCHD", "Webull"]
    print("PASS  auto terms: names and acronyms said often - not everyday words, "
          "mishearings or rare words.")


def test_auto_terms_respect_the_dictionary():
    idx = _index(SAID)
    assert learning.auto_terms(idx, _vocab(dismissed=["webull"])) == ["SCHD"], \
        "a word you removed never comes back"
    assert learning.auto_terms(idx, _vocab(terms=["SCHD", "Webull"])) == [], "already known"
    assert learning.auto_terms(idx, _vocab(corrections={"web bull": "Webull"})) == ["SCHD"]
    # A mishearing of a word you already have isn't learned as a new word.
    idx2 = _index([f"Open Kalshi now, round {i}." for i in range(6)])
    assert learning.auto_terms(idx2, _vocab(terms=["Kalshee"])) == []
    print("PASS  auto terms skip removed, known, and misheard-known words.")


def test_auto_terms_need_one_spelling():
    lines = ([f"Ping Zorblax now, round {i}." for i in range(3)] +
             [f"Ping ZorbLax now, round {i}." for i in range(3)])
    assert learning.auto_terms(_index(lines), _vocab()) == [], "no spelling dominates"
    print("PASS  a word spelled two ways isn't learned until one spelling wins.")


def test_is_variant():
    assert learning.is_variant("Koushi", ["Kalshi"])
    assert learning.is_variant("Weibo", ["Webull"])
    assert not learning.is_variant("Vercel", ["Webull", "Kalshi"])
    assert not learning.is_variant("Webull", ["Webull"]), "a word isn't a variant of itself"
    print("PASS  is_variant spots a mishearing of a known word.")


# --- Part 1: the words you fixed ---------------------------------------------

def test_find_fixes_learns_real_fixes():
    ff = learning.find_fixes
    assert ff("ask cal she about the odds", "ask Kalshee about the odds") == [("cal she", "Kalshee")]
    assert ff("the kalshee market", "the Kalshee market") == [("kalshee", "Kalshee")], \
        "a rare word's capitals"
    assert ff("the text two speech demo", "the text-to-speech demo") == \
        [("text two speech", "text-to-speech")], "the user's hyphens are kept"
    assert ff("Ask cal she, then web bull.", "Ask Kalshee, then Webull.") == \
        [("cal she", "Kalshee"), ("web bull", "Webull")], "two fixes in one take"
    print("PASS  find_fixes learns the words you corrected, as you wrote them.")


def test_find_fixes_ignores_everything_else():
    ff = learning.find_fixes
    assert ff("buy an apple today", "buy an Apple today") == [], "an everyday word's capitals"
    assert ff("put it there please", "put it their please") == [], "grammar: both everyday"
    assert ff("let's have a meeting", "let's have a call") == [], "a different word, not a fix"
    assert ff("send 25 dollars", "send 35 dollars") == [], "numbers"
    assert ff("send it to cal she", "send it to cal she and more words") == [], "appended"
    assert ff("send it to cal she now", "send it now") == [], "deleted"
    assert ff("one two three four five six seven eight",
              "uno dos tres cuatro cinco seis siete ocho") == [], "a rewrite"
    assert ff("we met on the first floor of the big blue building downtown",
              "we met in a small cafe near the station yesterday") == [], "rewritten"
    assert ff("", "anything") == [] and ff("words here", "") == []
    assert ff("ask cal she about it", "ask Kalshee Kalshee Kalshee Kalshee about it") == [], \
        "more than 3 words for 2"
    print("PASS  find_fixes ignores rewrites, grammar, numbers, additions and deletions.")


def test_find_fixes_ignores_grammar_with_curly_apostrophes():
    # Word and Outlook turn a typed ' into ’ - still grammar, not a name.
    ff = learning.find_fixes
    assert ff("I think your right", "I think you’re right") == []
    assert ff("were late again", "we’re late again") == []
    assert ff("its fine now", "it’s fine now") == []
    assert ff("lets go now", "let’s go now") == []
    print("PASS  your -> you're, its -> it's with curly apostrophes are grammar, not fixes.")


def test_find_fixes_ignores_word_endings():
    # Possessives, plurals and verb forms are edits, not mishearings: learning
    # "API" -> "APIs" would type "APIs" for every "API" after.
    ff = learning.find_fixes
    assert ff("check the Webull app", "check the Webull's app") == []
    assert ff("read the API docs", "read the APIs docs") == []
    assert ff("the repo is big", "the repos is big") == []
    assert ff("please summarize it", "please summarized it") == []
    assert ff("we deploy now", "we deploying now") == []
    assert ff("we make it", "we making it") == []
    print("PASS  possessives, plurals and verb endings are never learned.")


def test_find_fixes_ignores_capitals_that_start_a_sentence():
    ff = learning.find_fixes
    assert ff("done and deploying now", "done. Deploying now") == [], "a new sentence"
    assert ff("Done. Deploying now", "Done, deploying now") == [], "two sentences joined"
    assert ff("the kalshee market", "the Kalshee market") == [("kalshee", "Kalshee")], \
        "mid-sentence it is still learned"
    print("PASS  a capital that only starts (or stops starting) a sentence isn't learned.")


def test_find_fixes_ignores_joined_everyday_words():
    ff = learning.find_fixes
    assert ff("let me set up the call", "let me setup the call") == [], "setup is a word"
    assert ff("it may be late", "it maybe late") == []
    assert ff("we train every day", "we train everyday") == []
    assert ff("I cannot go", "I can not go") == [], "split into everyday words"
    assert ff("that is alright", "that is all right") == []
    assert ff("this and or that", "this and/or that") == [], "only little words"
    assert ff("my face book page", "my facebook page") == [("face book", "facebook")]
    assert ff("the text two speech demo", "the text-to-speech demo") == \
        [("text two speech", "text-to-speech")]
    print("PASS  joining or splitting everyday words is grammar, unless it makes a new word.")


def test_find_region():
    fr = learning.find_region
    assert fr("Hi ", " Bye", "Hi fixed words Bye") == "fixed words"
    assert fr("Hi ", "", "Hi fixed words and more") == "fixed words and more"
    assert fr("", " Bye", "fixed words Bye") == "fixed words"
    assert fr("", "", "whole box") == "whole box"
    assert fr("Hi ", " Bye", "Something else entirely") is None, "the anchors are gone"
    assert fr("Hi ", " Bye", "Bye first, then Hi words") is None, "after-anchor must follow"
    print("PASS  find_region finds Scribe's text between its anchors, or says it's gone.")


def test_find_region_at_the_edges_of_the_box():
    # Scribe adds a space after each dictation: when its text ends the box,
    # the after-anchor is just that space - it must match at the END of the
    # box, not at the first space inside Scribe's own words.
    fr = learning.find_region
    assert fr("Hi ", " ", "Hi fixed words ") == "fixed words"
    assert fr("", " ", "fixed words ") == "fixed words"
    assert fr("Hi ", " ", "Hi fixed words and more") == "fixed words and more", \
        "typed on after it: followed to the end of the box"
    assert fr("Hi ", " Bye", "Hi fixed words Bye") == "fixed words"
    assert fr(" ", "", "   fixed words") == "  fixed words", "a leading space matches at the start"
    print("PASS  an anchor that ran to the edge of the box is matched at that edge.")


# --- Adding and removing a learned word --------------------------------------

def test_everyday_words_are_never_replaced():
    # A fix whose wrong side is everyday English teaches the name as a word
    # to listen for, but never becomes a find-and-replace: "their" -> "Theo"
    # would rewrite every later "their".
    for wrong, right in [("their", "Theo"), ("so we", "Zoe"), ("me a", "Mia"),
                         ("and a", "Anna"), ("the rest", "Theresa"), ("shown", "Shawn"),
                         ("cal she", "Kalshee"), ("you’re", "Yuri")]:
        v = _vocab()
        assert learning.learn(v, right, wrong, "fix", now="t"), wrong
        assert v["terms"] == [right] and v["corrections"] == {}, (wrong, v)
        assert v["learned"][right.lower()]["wrong"] == wrong, "the page still says what you fixed"
    v = _vocab()
    assert learning.learn(v, "Kalshee", "Koushi", "fix", now="t")
    assert v["corrections"] == {"koushi": "Kalshee"}, "a mishearing that isn't a word is replaced"
    print("PASS  a fix of everyday words teaches the name, never a find-and-replace.")


def test_learn_and_forget():
    v = _vocab(terms=["Sam"])
    assert learning.learn(v, "Kalshee", "Koushi", "fix", now="2026-09-27T10:00:00")
    assert v["terms"][-1] == "Kalshee" and v["corrections"]["koushi"] == "Kalshee"
    assert v["learned"]["kalshee"] == {"from": "fix", "at": "2026-09-27T10:00:00", "wrong": "Koushi"}
    assert not learning.learn(v, "Kalshee", "Koushi", "fix"), "nothing new"
    assert learning.learn(v, "Webull", source="said", now="2026-09-27T11:00:00")
    assert v["learned"]["webull"] == {"from": "said", "at": "2026-09-27T11:00:00"}
    assert learning.forget(v, "Kalshee")
    assert "Kalshee" not in v["terms"] and "koushi" not in v["corrections"]
    assert "kalshee" in v["dismissed"] and "kalshee" not in v["learned"]
    assert v["terms"] == ["Sam", "Webull"], "other words stay"
    assert not learning.learn(v, "Kalshee", "Koushi"), "a removed word never comes back"
    assert not learning.forget(v, "never-there")
    print("PASS  learn adds a word (and its fix); forget removes it for good.")


def test_learn_keeps_a_manual_term_and_updates_capitals():
    v = _vocab(terms=["kalshee"])
    assert learning.learn(v, "Kalshee", "kalshee", "fix", now="t")
    assert v["terms"] == ["Kalshee"], "the fixed spelling replaces the old one"
    assert v["corrections"] == {"kalshee": "Kalshee"}
    print("PASS  a capitals fix updates the term's spelling.")


if __name__ == "__main__":
    test_suggestions_keep_the_users_spelling()
    test_unicode_words_are_one_word()
    test_names_bypass_the_common_word_list()
    test_calendar_and_language_words_are_not_names()
    test_a_spelling_tie_goes_to_the_capitalized_form()
    test_auto_terms_keep_names_and_skip_the_rest()
    test_auto_terms_respect_the_dictionary()
    test_auto_terms_need_one_spelling()
    test_is_variant()
    test_find_fixes_learns_real_fixes()
    test_find_fixes_ignores_everything_else()
    test_find_fixes_ignores_grammar_with_curly_apostrophes()
    test_find_fixes_ignores_word_endings()
    test_find_fixes_ignores_capitals_that_start_a_sentence()
    test_find_fixes_ignores_joined_everyday_words()
    test_find_region()
    test_find_region_at_the_edges_of_the_box()
    test_everyday_words_are_never_replaced()
    test_learn_and_forget()
    test_learn_keeps_a_manual_term_and_updates_capitals()
    print("\nAll learning tests passed.")
