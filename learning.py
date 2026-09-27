"""
=============================================================================
 SCRIBE LEARNING - the Dictionary that fills itself.
=============================================================================

 Nobody wants to keep a word list. Scribe learns your words two ways:

   1. FROM YOUR FIXES. After Scribe types a dictation, fix_watch.py keeps an
      eye on that text box for a minute. find_fixes() compares what Scribe
      typed with what you left there and returns the words you corrected
      ("cal she" -> "Kalshi"): next time Scribe spells them right.

   2. FROM WHAT YOU SAY OFTEN. SuggestionIndex counts the words across your
      history; auto_terms() picks the names and jargon worth keeping
      (Webull, SCHD...) - with strict rules, so everyday words and
      mishearings ("Koushi" for Kalshi) stay out.

 learn() / forget() add or remove a learned word in a vocabulary dict (the
 shape of vocabulary.json). Everything here is a pure function: no files, no
 threads - app.py saves through storage.py, and the tests call these
 directly.
=============================================================================
"""

import difflib
import re

# Bundled common-English-word list: an everyday word is never learned as a
# name. Soft import: if the data module is somehow missing, learning still
# works, it just can't tell everyday words apart (noisier, but functional).
try:
    from common_words import COMMON_WORDS
except Exception:
    COMMON_WORDS = frozenset()


# =============================================================================
#  THE WORD STATISTICS  -  how often you say each word, and how you spell it.
# =============================================================================

# --- Vocabulary suggestion engine tuning ---------------------------------
MIN_SUGGESTION_COUNT = 3      # a word must be said at least this many times
MIN_SUGGESTION_LEN   = 3      # ...and be at least this many letters
MAX_SUGGESTIONS      = 15     # cap the list so it stays scannable
CAP_BOOST            = 1.6    # rank multiplier for ever-capitalized words

# Everyday words to keep out of suggestions, on top of COMMON_WORDS. Mirrors
# the dashboard's client-side STOP_WORDS (these carry apostrophes, which the
# common-word list does not).
SUGGEST_STOP_WORDS = frozenset((
    "the a an and or but of in on at to for with by from is am are was were be "
    "been being have has had do does did will would could should can may this that "
    "these those i you he she it we they my your his her its our their me him them "
    "us what which who how when where why as if so than then just like get got also "
    "really very more most some any all no not about into out now here there too only "
    "own same such i'm i've i'll i'd it's that's don't doesn't didn't won't can't "
    "you're we're they're isn't").split())


def _at_sentence_start(text, start):
    """True if the token at index `start` begins a sentence - i.e. nothing
    precedes it, or the previous non-space char ends a sentence. Used to ignore
    Whisper's automatic sentence-initial capitals when scoring proper nouns."""
    before = text[:start].rstrip()
    return (not before) or before[-1] in ".!?"


def _sample_around(text, start, end, width=46):
    """A short readable snippet around a word, for context in the suggestion
    card ('…deploy it to versel tonight and…')."""
    a = max(0, start - width)
    b = min(len(text), end + width)
    snippet = text[a:b].strip()
    if a > 0:
        snippet = "…" + snippet
    if b < len(text):
        snippet = snippet + "…"
    return snippet


# A word: letters of ANY language ("José", "naïve"), with inner apostrophes
# ("don't"). Digits and underscores are not letters.
WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")

# A common word that is capitalized in at least this share of its
# mid-sentence uses (and at least NAME_MIN_MID times) is treated as a name -
# "David", "Nvidia" - and may be suggested despite COMMON_WORDS.
NAME_CAP_SHARE = 0.6
NAME_MIN_MID = 2

# Always Capitalized, but not the names Scribe is looking for - Whisper
# spells them right, so suggesting them is just noise.
NOT_NAMES = frozenset((
    "monday tuesday wednesday thursday friday saturday sunday "
    "january february march april may june july august september october "
    "november december "
    "english spanish french german italian portuguese chinese japanese korean "
    "russian arabic hindi dutch greek american british canadian mexican "
    "european african asian christmas easter").split())


class SuggestionIndex:
    """
    Running word statistics over the dictation history, for the Dictionary
    page's suggestions. add() takes new entries only, so a new dictation
    costs a few microseconds instead of a rescan of the whole history.

    A word is a candidate when it is said often, isn't an everyday English
    word (COMMON_WORDS / stop-words - unless it is almost always Capitalized
    mid-sentence, i.e. a name), isn't already a term or a correction's
    wrong/right side, and hasn't been dismissed. Ranked by frequency, with a
    boost for words seen Capitalized mid-sentence (a proper-noun tell).
    """

    def __init__(self):
        self.counts = {}         # word (lowercase) -> times said
        self.samples = {}        # word -> a snippet of its first use
        self.mid_total = {}      # word -> uses not at a sentence start
        self.mid_capped = {}     # word -> ...of which Capitalized
        self.spellings = {}      # word -> {spelling: count} mid-sentence
        self.any_spelling = {}   # word -> {spelling: count} anywhere

    def add(self, entries):
        for e in entries:
            text = e.get("text", "") or ""
            for m in WORD_RE.finditer(text):
                tok = m.group()
                w = tok.lower()
                self.counts[w] = self.counts.get(w, 0) + 1
                if w not in self.samples:
                    self.samples[w] = _sample_around(text, m.start(), m.end())
                anyd = self.any_spelling.setdefault(w, {})
                anyd[tok] = anyd.get(tok, 0) + 1
                if not _at_sentence_start(text, m.start()):
                    self.mid_total[w] = self.mid_total.get(w, 0) + 1
                    if tok[0].isupper():
                        self.mid_capped[w] = self.mid_capped.get(w, 0) + 1
                    mid = self.spellings.setdefault(w, {})
                    mid[tok] = mid.get(tok, 0) + 1

    def _display(self, w):
        """The spelling to show: the most common one mid-sentence (sentence-
        initial capitals say nothing), else the most common anywhere - and on
        a tie, the Capitalized one (a name typed both ways is still a name)."""
        pool = self.spellings.get(w) or self.any_spelling.get(w) or {w: 1}
        return max(pool.items(), key=lambda kv: (kv[1], kv[0][:1].isupper(), kv[0]))[0]

    def _looks_like_a_name(self, w):
        if w in NOT_NAMES:
            return False
        mid = self.mid_total.get(w, 0)
        return mid >= NAME_MIN_MID and self.mid_capped.get(w, 0) / mid >= NAME_CAP_SHARE

    def suggest(self, terms, corrections, dismissed):
        """Up to MAX_SUGGESTIONS {word, count, sample}, best first."""
        known = {str(t).strip().lower() for t in terms}
        for wrong, right in corrections.items():
            known.add(str(wrong).strip().lower())
            known.add(str(right).strip().lower())
        dismissed_set = {str(d).strip().lower() for d in dismissed}
        candidates = []
        for w, c in self.counts.items():
            if c < MIN_SUGGESTION_COUNT or len(w) < MIN_SUGGESTION_LEN:
                continue
            if w in known or w in dismissed_set:
                continue
            bare = w.replace("'", "")
            if w in SUGGEST_STOP_WORDS or bare in SUGGEST_STOP_WORDS:
                continue
            if (w in COMMON_WORDS or bare in COMMON_WORDS) and not self._looks_like_a_name(w):
                continue
            score = c * (CAP_BOOST if self.mid_capped.get(w) else 1.0)
            candidates.append((score, c, w))
        candidates.sort(reverse=True)
        return [{"word": self._display(w), "count": c, "sample": self.samples.get(w, "")}
                for _score, c, w in candidates[:MAX_SUGGESTIONS]]


def suggest_vocabulary(entries, terms, corrections, dismissed):
    """Suggestions for a list of entries (builds a fresh index). The live
    dashboard uses HISTORY's running index instead."""
    index = SuggestionIndex()
    index.add(entries)
    return index.suggest(terms, corrections, dismissed)


# =============================================================================
#  PART 2  -  the names you say often.
# =============================================================================

# A name is kept only when ALL of these hold - better to miss a word (it can
# still be learned from a fix) than to lock in an everyday word or a
# mishearing, which would push the voice model the wrong way.
AUTO_MIN_COUNT = 5         # said at least this often
AUTO_SPELLING_SHARE = 0.8  # ...nearly always spelled the same way
VARIANT_RATIO = 0.5        # this alike to a known word = probably a mishearing of it
# An acronym: 2-6 capitals or digits, starting with a letter ("SCHD", "MCP").
_ACRONYM = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")


def is_variant(word, known, ratio=VARIANT_RATIO):
    """Does `word` look like a misspelling of one of `known` ("Koushi" for
    Kalshi, "Weibo" for Webull)? Compared by letters, ignoring case - a word
    isn't a variant of itself."""
    w = (word or "").lower()
    for k in known or ():
        k = str(k).lower()
        if k and k != w and difflib.SequenceMatcher(None, w, k).ratio() >= ratio:
            return True
    return False


def _known_words(vocab):
    """Every word the Dictionary already has - terms, both sides of each
    correction, and the ones you removed - lowercased."""
    known = {str(t).strip().lower() for t in vocab.get("terms", ())}
    for wrong, right in (vocab.get("corrections") or {}).items():
        known.add(str(wrong).strip().lower())
        known.add(str(right).strip().lower())
    known.update(str(d).strip().lower() for d in vocab.get("dismissed", ()))
    return known


def auto_terms(index, vocab):
    """
    The names in `index` (a SuggestionIndex over your history) worth adding
    to the Dictionary, most said first. A word qualifies only when it is:
      - said at least AUTO_MIN_COUNT times;
      - a name (Capitalized in most of its mid-sentence uses) or an acronym;
      - not an everyday word, a day, month or language;
      - spelled one way at least AUTO_SPELLING_SHARE of the time;
      - not already known (a term, a correction, or a word you removed);
      - not a variant of a known term, or of another candidate said at least
        twice as often - that's the same name misheard.
    """
    known = _known_words(vocab)
    candidates = []
    for w, count in index.counts.items():
        if count < AUTO_MIN_COUNT or len(w) < 3 or w in known:
            continue
        bare = w.replace("'", "")
        if (w in COMMON_WORDS or bare in COMMON_WORDS or w in NOT_NAMES
                or w in SUGGEST_STOP_WORDS):
            continue
        spelling = index._display(w)
        if not (index._looks_like_a_name(w) or _ACRONYM.match(spelling)):
            continue
        uses = index.any_spelling.get(w, {})
        if sum(uses.values()) and uses.get(spelling, 0) / sum(uses.values()) < AUTO_SPELLING_SHARE:
            continue
        candidates.append((count, spelling))
    candidates.sort(key=lambda c: (-c[0], c[1]))
    # Everything the Dictionary knows counts - its terms, what its
    # corrections turn words into, and the words you removed: "Weibo" is
    # still Webull misheard, even after you told Scribe to drop Webull.
    out = []
    for count, spelling in candidates:
        louder = [s for c, s in candidates if c >= 2 * count]
        if is_variant(spelling, list(known) + louder):
            continue
        out.append(spelling)
    return out
