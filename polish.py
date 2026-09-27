"""
=============================================================================
 SCRIBE - AI POLISH  (Wispr-style cleanup of each dictation)
=============================================================================

 Speech is messy: "okay so um I think we should uh we should change the the
 handle press function in app dot py". A transcriber writes down exactly
 that. A quick language model turns it into what you meant to type:

     I think we should change the handle_press function in app.py.

 That's all this module does: one short chat request to Groq, with rules
 that keep your meaning, tone and details, never add anything, and never
 ANSWER what you dictated (a dictated question is text to tidy, not a
 question for the model). Scribe calls it right after transcription.

   polish(text, client, terms, name)  -> the cleaned text, or PolishError
   should_polish(text)                -> is this take worth polishing?

 Speed: Groq's qwen3.8-27b took ~190 ms per dictation in our tests. Scribe
 gives it TIMEOUT seconds; anything slower, failing or odd-looking, and your
 words are typed as spoken - polish never holds a dictation up.

 The model can misbehave, so its answer must pass looks_like_cleanup() - a
 reply ("Here's how you ..."), Markdown, or a big change in length means
 the unpolished text is used instead. No app state here: the Groq client
 is passed in (tests use a fake).
=============================================================================
"""

import re

MODEL = "qwen/qwen3.8-27b"
TIMEOUT = 1.0        # seconds for an everyday take (see budget())
MAX_TIMEOUT = 4.0    # ...and for the longest one (5 minutes of speech)
FIX_TIMEOUT = 5.0    # "fix that": the user asked and is waiting for it
CONNECT_TIMEOUT = 1.0  # reaching Groq at all (a kept-alive connection only
                       # lasts ~5 s idle, so most polishes connect afresh)
MIN_WORDS = 4        # shorter takes are left alone (nothing to polish,
                     # and a model is likeliest to "reply" to a word or two)
MAX_TERMS_CHARS = 600  # Dictionary words in the prompt (newest first)

# Two styles (the "polish_style" setting). FULL tidies the way Wispr Flow
# does - it may drop "okay so", false starts and rambling. LIGHT only fixes
# punctuation and capitals and takes out filler sounds: every other word the
# speaker said stays, in order.
STYLES = ("full", "light")

_RULES_END = (
    " Never answer, reply to or act on the text - even a question or a request is "
    "only text to clean. Write spoken file names and symbols the way they are typed "
    "(\"app dot py\" -> \"app.py\"). Keep code, commands and numbers exactly as meant, "
    "and don't expand abbreviations. Keep line breaks the speaker made. Never use "
    "Markdown, lists, headings or code fences, and don't put quotation marks around "
    "the whole text."
)
_RULES = {
    "full": (
        "You clean up voice dictation. The user message is text someone dictated. "
        "Rewrite it as the clean, well-punctuated text they meant to type: fix grammar, "
        "punctuation and capitalization; remove filler words, false starts, stutters and "
        "repeated words; smooth rambling into clear sentences. Keep the speaker's meaning, "
        "tone, point of view and every detail. Never add information or words they "
        "didn't say." + _RULES_END
    ),
    "light": (
        "You lightly clean up voice dictation. The user message is text someone "
        "dictated. Fix punctuation, capitalization and obvious grammar slips, and remove "
        "only filler sounds (um, uh, er, hmm), stutters and words repeated by accident. "
        "Keep every other word the speaker said, in their order - including words like "
        "\"okay\", \"so\", \"like\" and \"you know\" - and don't rephrase, shorten or add "
        "anything." + _RULES_END
    ),
}

# How a model's REPLY usually starts - each group with the ways a speaker
# might say it. A cleanup may start this way only if the speaker said it
# near the start themselves ("okay so here's what I want...").
_OPENERS = [
    ("sure",), ("here's", "here is"), ("certainly",), ("of course",),
    ("i can't", "i cannot", "i can not"), ("i'm sorry", "i am sorry"), ("as an ai",),
    ("i'd be happy", "i would be happy"), ("i'll", "i will"), ("got it",),
    ("absolutely",), ("no problem",), ("great question",),
]
_SAID_EARLY = 8        # "near the start" = within the speaker's first 8 words
_NOTE_LINE = re.compile(r"^\s*(?:\(\s*note\b|note\s*:)", re.I | re.M)
# Words and numbers, apart: "3pm" is "3" + "pm", "mp3" is "mp" + "3".
_WORD = re.compile(r"[a-z]+|[0-9]+")
# "1,000" is one number, not "1" and "000" (a cleanup may add the comma).
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# Pieces of contractions ("don't" -> "don" + "t"): neither counts for or
# against the speaker, so "do not" -> "don't" isn't a new word.
_CONTRACTION_STEMS = frozenset(
    "don doesn didn won isn aren wasn weren can couldn shouldn wouldn hasn haven hadn".split())
_MIN_SHARED = 0.5      # at least half the answer's words must be the speaker's
_MARKDOWN_LINE = re.compile(r"^\s*(?:[-*•]\s|#{1,6}\s|\d+[.)]\s|```)", re.M)

# Words a cleanup may bring in that the speaker didn't say: joining words
# ("did", "they", "including" - how "this transcription pick this up?" becomes
# a sentence), the halves of spoken shortcuts ("gonna" -> "going to"), and
# numbers written out. Anything else new - a real word like "slowly" - was
# made up, and the answer is refused (seen live: the transcriber missed a
# word and the model invented one to fill the gap).
_JOINING_WORDS = frozenset("""
    the and but for yet you your yours she her hers him his its our ours
    they them their theirs this that these those who whom whose which what when
    where why how been being have has had having does did doing done will would
    shall should can could may might must with from into onto upon about above below over
    under after before between through during without within along across around
    against toward towards than then because since although though while unless
    until whether either both each every all any some such same other
    another own only also just even still already very more most less least much
    many few here there now again once however therefore instead including rather
    otherwise meanwhile plus going want kind sort let gotta got
    zero one two three four five six seven eight nine ten eleven twelve thirteen
    fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty
    sixty seventy eighty ninety hundred thousand million billion first second
    third percent
""".split())
# A negation changes what a sentence says: "the deal is approved" must never
# quietly become "...is not approved", or the other way round. So they are
# COUNTED - a cleanup may not add one, light may not drop one, and full may
# not drop them all ("do not" -> "don't" keeps the count: "t" is the end of
# every "n't", so "can" -> "can't" is caught too).
_NEGATIONS = frozenset("not no never nothing nobody none neither nor cannot t".split())
# Spoken shortcuts, and the words a cleanup writes for them.
_INFORMAL = {
    "dunno": "don t know", "gimme": "give me", "lemme": "let me",
    "gonna": "going to", "wanna": "want to", "gotta": "got to have",
    "kinda": "kind of", "sorta": "sort of", "alright": "all right",
    "ok": "okay", "okay": "ok", "cuz": "because", "cause": "because",
    "ya": "you", "yeah": "yes", "yep": "yes", "yup": "yes", "nope": "no",
    "tryna": "trying to", "outta": "out of", "lotta": "lot of",
}
# "3, no wait, 4": the speaker took a number back - FULL may drop what came
# BEFORE the correction (that's the point of tidying a false start). Only
# phrases that always mean "I take that back": "actually" and "sorry" are
# everyday words.
_CORRECTION = re.compile(r"\b(?:no,? wait|wait,? no|i mean|scratch that|no,? no)\b", re.I)
_SUFFIXES = ("ing", "ed", "es", "er", "ly", "s", "e", "y")
_FILLER_SOUNDS = frozenset("um umm uh uhh er erm hmm mm mhm ah".split())
_LIGHT_MAX_DROPPED = 0.1   # light: at most 1 + 10% of the speaker's words may go


def _stem(word):
    """The word without one ending (-ing, -ed, -s, -e...), if 3+ letters stay
    - and without the doubled letter such an ending brings ("running" ->
    "run", "stopped" -> "stop")."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            stem = word[:-len(suffix)]
            if len(stem) >= 4 and stem[-1] == stem[-2] and stem[-1] not in "aeiouy":
                stem = stem[:-1]
            return stem
    return word


def _same_word(a, b):
    """One word in another form: "app"/"apps", "slow"/"slowly" (the shorter
    starts the longer, 4 letters over at most - 2 for a 3-letter word, so
    "car" isn't "career") or "loaded"/"loading", "take"/"taking" (the same
    stem). Not "contact"/"contract" or "expect"/"expert" - that's a
    different word, however alike it looks."""
    short, long_ = sorted((a, b), key=len)
    extra = len(long_) - len(short)
    if len(short) >= 3 and long_.startswith(short) and extra <= (2 if len(short) == 3 else 4):
        return True
    stem = _stem(a)
    return len(stem) >= 3 and stem == _stem(b)


def _term_words(terms):
    """The words of the Dictionary terms (and the user's name) - two letters
    or more, so the "m" of "I'm" isn't one."""
    return {w for term in terms or () if isinstance(term, str)
            for w in _words(term) if len(w) >= 2}


def _has_run(words, run):
    """Does `run` appear in `words`, in order and side by side?"""
    n = len(run)
    return any(words[i:i + n] == run for i in range(len(words) - n + 1))


def _said_terms(original, said, terms):
    """The words of the Dictionary terms the speaker really said: the whole
    term ("New York", not just "new"), and - for a term with capitals - in
    those capitals ("Will" the name, not "will" the verb)."""
    out = set()
    for term in terms or ():
        if not isinstance(term, str) or not term.strip():
            continue
        words = [w for w in _words(term) if len(w) >= 2]
        if not words or not _has_run(said, words):
            continue
        if term != term.lower() and not re.search(
                r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", original):
            continue
        out.update(words)
    return out


def _invented(said, answer, terms):
    """The answer's words that are new: not the speaker's, not what a spoken
    shortcut stands for ("dunno" -> "don't know"), not a joining word, a
    negation (counted apart - _negations_changed), a number, a Dictionary
    word or a form of a word they said."""
    spoken = set(said)
    allowed = _term_words(terms)
    for w in spoken:
        allowed.update(_INFORMAL.get(w, "").split())
    return [w for w in answer
            if w not in spoken and w not in allowed and w not in _NEGATIONS
            and len(w) > 3 and w not in _JOINING_WORDS and w not in _CONTRACTION_STEMS
            and not w.isdigit() and not any(_same_word(w, s) for s in spoken)]


def _negation_count(words):
    """How many negations: "not", "no", "never"... and each "n't" (its "t") -
    plus the ones inside spoken shortcuts ("dunno" = "don't know")."""
    return sum(1 for w in words if w in _NEGATIONS) + sum(
        1 for w in words for part in _INFORMAL.get(w, "").split() if part in _NEGATIONS)


def _negations_changed(original, polished, style):
    """True when the answer says something else about yes and no: it has a
    negation the speaker didn't say; or (light) it lost one; or (full) the
    speaker negated something and the answer negates nothing. The "no" of
    "no wait" takes a word back - it doesn't negate anything, on either side."""
    before = _negation_count(_words(_CORRECTION.sub(" ", original)))
    after = _negation_count(_words(_CORRECTION.sub(" ", polished)))
    if after > before:
        return True
    return after < before if style == "light" else (before > 0 and after == 0)


def _taken_back(original):
    """Where a number was taken back: the position of the last number just
    before each correction ("at 3, no wait, 4" -> the 3). Positions are in
    the lowercased text with thousands commas removed, like _words()."""
    text = _THOUSANDS.sub("", original.lower())
    numbers = [(m.start(), m.group()) for m in _WORD.finditer(text) if m.group().isdigit()]
    out, since = set(), 0
    for cue in _CORRECTION.finditer(text):
        before = [pos for pos, _n in numbers if since <= pos < cue.start()]
        if before:
            out.add(before[-1])
        since = cue.end()
    return out, numbers


def _lost(original, said, answer, terms, style="full"):
    """Numbers and Dictionary words the speaker said that the answer lost -
    a cleanup never drops those (a wrong number reads fine and is wrong).
    One exception: in the full style, the number the speaker took back ("at
    3, no wait, 4" - the 3 only) may go; every other number stays protected."""
    kept = set(answer)
    if style == "full":
        taken_back, numbers = _taken_back(original)
        protected = {n for pos, n in numbers if pos not in taken_back}
    else:
        protected = {w for w in said if w.isdigit()}
    precious = protected | _said_terms(original, said, terms)
    return sorted(w for w in precious if w not in kept)


class PolishError(Exception):
    """
    The polish didn't happen. `kind`: "auth" (401: the key was rejected),
    "forbidden" (403: the key may not use this model), "rate_limited"
    (with `retry_after` seconds when Groq said), "timeout", "network" (incl.
    not connecting within CONNECT_TIMEOUT), "gone" (404/400: the model was
    retired, or the request is refused as a whole), "server" (anything else
    Groq refused) or "suspicious" (the answer didn't look like a cleanup).
    """

    def __init__(self, kind, detail="", retry_after=None):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.retry_after = retry_after


def build_prompt(terms=(), name="", style="full"):
    """The rules for `style` ("full" or "light"), plus the words to keep
    exactly as spelled: the user's name first, then Dictionary terms newest
    first, up to MAX_TERMS_CHARS."""
    keep, used = [], 0
    name = (name or "").strip()
    if name:
        keep.append(name)
        used = len(name)
    for term in reversed(list(terms or ())):
        term = term.strip() if isinstance(term, str) else ""
        if not term or term in keep:
            continue
        if used + len(term) + 2 > MAX_TERMS_CHARS:
            break
        keep.append(term)
        used += len(term) + 2
    prompt = _RULES.get(style, _RULES["full"])
    if keep:
        prompt += " Keep these words exactly as spelled: " + ", ".join(keep) + "."
    return prompt + " Output only the cleaned text."


def tidy(answer):
    """The model's answer without a <think> block, surrounding whitespace, or
    one pair of quotes wrapped around the whole thing."""
    if not isinstance(answer, str):
        return ""
    text = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip()
    closing = {"“": "”"}
    if (len(text) >= 2 and text[0] in "\"'“"
            and text[-1] == closing.get(text[0], text[0])
            and text[0] not in text[1:-1]):
        text = text[1:-1].strip()
    return text


def _words(text):
    """Lowercase words and numbers, split at anything else ("app.py" ->
    app, py; "handle_press" -> handle, press; "1,000" -> 1000)."""
    return _WORD.findall(_THOUSANDS.sub("", text.lower()))


def _starts_with(words, phrase):
    return words[:len(phrase)] == phrase


def _said_early(words, phrase):
    n = len(phrase)
    return any(words[i:i + n] == phrase for i in range(min(_SAID_EARLY, len(words))))


def looks_like_cleanup(original, polished, terms=(), style="full"):
    """
    Does `polished` look like a cleanup of `original` - not a reply, not a
    note, not a list, not a rewrite of a different size? `terms` are the
    Dictionary words (and the user's name). The checks, in turn:
      - it starts like a reply ("Sure", "Here's", "I'll"...) the speaker
        didn't say near the start themselves;
      - it has Markdown, or a "Note:" line;
      - it has a real word the speaker never said (_invented), lost a
        number or Dictionary word they did say (_lost), or says something
        else about yes and no (_negations_changed);
      - light style only: it dropped more than a few of the speaker's words;
      - fewer than half of its words are the speaker's ("Paris." for "what's
        the capital of France", a poem for "write me a poem");
      - it is much longer - or, for takes of 8+ words, much shorter (filler-
        heavy speech shrinks a lot, but not to less than 40%).
    """
    if not polished:
        return False
    said, answer = _words(original), _words(polished)
    if (_invented(said, answer, terms) or _lost(original, said, answer, terms, style)
            or _negations_changed(original, polished, style)):
        return False
    if style == "light":
        mine = {w for w in said if len(w) > 2 and w not in _FILLER_SOUNDS}
        dropped = mine - set(answer)
        if len(dropped) > 1 + _LIGHT_MAX_DROPPED * len(mine):
            return False
    for group in _OPENERS:
        phrases = [_words(p) for p in group]
        if (any(_starts_with(answer, p) for p in phrases)
                and not any(_said_early(said, p) for p in phrases)):
            return False
    if _MARKDOWN_LINE.search(polished) or _NOTE_LINE.search(polished):
        return False
    counted = [w for w in answer if len(w) > 2 and w not in _CONTRACTION_STEMS]
    spoken = set(said)
    if counted and sum(w in spoken for w in counted) < _MIN_SHARED * len(counted):
        return False
    before, after = len(original.split()), len(polished.split())
    if after > 1.5 * before + 3:
        return False
    if before >= 8 and after < 0.4 * before:
        return False
    return True


def budget(words):
    """
    Seconds to give a polish of `words` words. Writing the answer takes time
    in proportion to its length (measured on Groq: 150 words ~0.47 s, 400
    words ~1.17 s), so a flat budget would leave every long take unpolished
    after a wasted second. About twice the measured time, at least TIMEOUT,
    at most MAX_TIMEOUT.
    """
    return min(MAX_TIMEOUT, max(TIMEOUT, 0.5 + 0.004 * words))


def should_polish(text):
    """Worth polishing: at least MIN_WORDS words."""
    return len((text or "").split()) >= MIN_WORDS


def _retry_after(exc):
    """Seconds a 429 asked us to wait (its retry-after header), or None."""
    try:
        value = float(exc.response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return None
    return max(1.0, min(value, 3600.0))


def polish(text, client, terms=(), name="", timeout=None, style="full"):
    """
    Polish `text` with one Groq chat request (`client` is a groq.Groq),
    within `timeout` seconds (default: budget() for its length), in `style`
    ("full" or "light"). Returns the cleaned text; raises PolishError if it
    can't, or if the answer doesn't look like a cleanup of `text`.
    """
    import groq          # lazy, like the rest of the cloud code
    import httpx
    words = len(text.split())
    if timeout is None:
        timeout = budget(words)
    try:
        response = client.chat.completions.create(
            model=MODEL,
            temperature=0,               # the same words in, the same text out
            reasoning_effort="none",     # no "thinking" - it only costs time
            max_completion_tokens=2 * words + 64,
            # Reaching Groq at all gets its own limit, and a connect timeout
            # counts as "network" (a 30 s pause), so a network that silently
            # drops connections doesn't cost every take its whole budget.
            timeout=httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT, timeout)),
            messages=[{"role": "system", "content": build_prompt(terms, name, style)},
                      {"role": "user", "content": text}],
        )
        choice = response.choices[0]
        answer = choice.message.content
        cut_off = getattr(choice, "finish_reason", None) == "length"
    # Order matters: the specific errors are subclasses of the general ones.
    except groq.AuthenticationError:
        raise PolishError("auth", "401") from None
    except groq.PermissionDeniedError:
        # The key works (it may just have transcribed this take) but isn't
        # allowed to use the polish model - a model problem, not a bad key.
        raise PolishError("forbidden", "403") from None
    except groq.RateLimitError as exc:
        raise PolishError("rate_limited", "429", _retry_after(exc)) from None
    except groq.APITimeoutError as exc:
        if isinstance(exc.__cause__, httpx.ConnectTimeout):
            raise PolishError("network", "couldn't connect in time") from None
        raise PolishError("timeout", f"over {timeout:.1f} s") from None
    except groq.APIConnectionError as exc:
        raise PolishError("network", type(exc).__name__) from None
    except (groq.NotFoundError, groq.BadRequestError) as exc:
        # The model is gone (retired) or the request is refused as a whole:
        # every polish would fail the same way until Scribe is updated.
        message = str(getattr(exc, "message", "") or exc)[:120]
        raise PolishError("gone", f"{exc.status_code}: {message}") from None
    except groq.APIStatusError as exc:
        message = str(getattr(exc, "message", "") or exc)[:120]
        raise PolishError("server", f"{exc.status_code}: {message}") from None
    except Exception as exc:
        raise PolishError("server", f"{type(exc).__name__}: {exc}"[:160]) from None
    if cut_off:
        # It ran out of room mid-sentence: typing it would lose your words.
        raise PolishError("suspicious", "cut off at the token cap")
    cleaned = tidy(answer)
    if not looks_like_cleanup(text, cleaned, list(terms or ()) + [name], style):
        raise PolishError("suspicious", f"{len(cleaned.split())} words back for {words}")
    return cleaned
