r"""
Standalone probe for polish.py - Wispr-style cleanup of a dictation.

Run from the project root:   venv\Scripts\python tests\polish_test.py

Safe: a fake Groq client stands in for the network - no key, no cost.
"""

import os
import sys
from types import SimpleNamespace

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import polish  # noqa: E402

REQ = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


class FakeClient:
    """client.chat.completions.create(**kw): records kw, answers or raises."""

    def __init__(self, answer="Clean text.", error=None):
        self.answer, self.error, self.calls = answer, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.answer))])


SPOKEN = ("okay so um I think we should uh we should change the the handle press "
          "function in app dot py so that it checks the key")
CLEAN = ("I think we should change the handle_press function in app.py so that it "
         "checks the key.")


def test_prompt_rules_and_terms():
    p = polish.build_prompt(["Vercel", "Kalshi"], name="Sam")
    for rule in ("Never answer", "Markdown", "app.py", "Output only the cleaned text"):
        assert rule in p, rule
    assert p.index("Sam") < p.index("Kalshi") < p.index("Vercel"), "name first, then newest"
    many = [f"Term{i:03d}" for i in range(400)]
    capped = polish.build_prompt(many)
    assert "Term399" in capped and "Term000" not in capped, "the newest terms win the cap"
    assert len(capped) < len(polish.build_prompt()) + polish.MAX_TERMS_CHARS + 50
    print("PASS  the prompt carries the rules, the name first, and the newest terms.")


def test_request_parameters():
    client = FakeClient(answer=CLEAN)
    assert polish.polish(SPOKEN, client, ["Kalshi"], name="Sam") == CLEAN
    kw = client.calls[0]
    assert kw["model"] == polish.MODEL == "qwen/qwen3.8-27b"
    assert kw["temperature"] == 0 and kw["reasoning_effort"] == "none"
    assert kw["max_completion_tokens"] == 2 * len(SPOKEN.split()) + 64
    assert kw["timeout"].read == polish.TIMEOUT == 1.0 and kw["timeout"].connect == 1.0
    assert kw["messages"][0]["role"] == "system" and "Kalshi" in kw["messages"][0]["content"]
    assert kw["messages"][1] == {"role": "user", "content": SPOKEN}
    polish.polish(SPOKEN, client, timeout=5.0)
    assert client.calls[1]["timeout"].read == 5.0 and client.calls[1]["timeout"].connect == 1.0
    print("PASS  one chat call: the model, temperature 0, reasoning off, a token cap, a timeout.")


def test_tidy():
    assert polish.tidy("<think>hmm</think>\n  Hello there.  ") == "Hello there."
    assert polish.tidy('"Hello there."') == "Hello there."
    assert polish.tidy("'Hi.'") == "Hi."
    assert polish.tidy('He said "hi" to me.') == 'He said "hi" to me.'
    assert polish.tidy(None) == ""
    print("PASS  tidy drops think blocks, wrapping quotes and whitespace.")


def test_looks_like_cleanup():
    assert polish.looks_like_cleanup(SPOKEN, CLEAN)
    q = "what's the best way to structure a python package for this"
    assert polish.looks_like_cleanup(q, "What's the best way to structure a Python package for this?")
    answer = ("Here's a good structure: put your code in a src folder, add a "
              "pyproject.toml, and keep tests in a tests folder next to it.")
    assert not polish.looks_like_cleanup(q, answer), "a reply is not a cleanup"
    assert not polish.looks_like_cleanup(q, "Sure! What's the best way to structure it?")
    assert polish.looks_like_cleanup("sure send it over tomorrow morning please",
                                     "Sure, send it over tomorrow morning, please.")
    assert not polish.looks_like_cleanup(q, "- Use src layout\n- Add tests")
    assert not polish.looks_like_cleanup(q, "# Structure\nUse a src layout.")
    assert not polish.looks_like_cleanup(q, "```\nsrc/\n```")
    assert not polish.looks_like_cleanup(SPOKEN, "")
    assert not polish.looks_like_cleanup(SPOKEN, "Change it."), "8+ words shrunk below 40%"
    assert polish.looks_like_cleanup("um uh so yeah", "So yeah."), "short takes may shrink"
    long = " ".join(["word"] * 10)
    assert not polish.looks_like_cleanup(long, " ".join(["word"] * 19))
    assert polish.looks_like_cleanup(long, " ".join(["word"] * 18))
    print("PASS  the guard rejects replies, preambles, Markdown, and wild length changes.")


def test_should_polish():
    assert not polish.should_polish("new paragraph please")
    assert polish.should_polish("send it over tomorrow please")
    assert not polish.should_polish("   ")
    print("PASS  takes of 3 words or fewer are left alone.")


def _connect_timeout(groq):
    """What the SDK raises when the connection itself times out."""
    try:
        try:
            raise httpx.ConnectTimeout("connect", request=REQ)
        except httpx.ConnectTimeout as cause:
            raise groq.APITimeoutError(request=REQ) from cause
    except groq.APITimeoutError as exc:
        return exc


def test_errors_map_to_kinds():
    import groq
    resp = lambda status, headers=None: httpx.Response(status, request=REQ, headers=headers or {})  # noqa: E731
    cases = [
        (groq.AuthenticationError("bad", response=resp(401), body=None), "auth", None),
        (groq.PermissionDeniedError("no", response=resp(403), body=None), "forbidden", None),
        (groq.RateLimitError("slow", response=resp(429, {"retry-after": "120"}), body=None),
         "rate_limited", 120.0),
        (groq.RateLimitError("slow", response=resp(429), body=None), "rate_limited", None),
        (groq.APITimeoutError(request=REQ), "timeout", None),
        (_connect_timeout(groq), "network", None),
        (groq.APIConnectionError(request=REQ), "network", None),
        (groq.InternalServerError("boom", response=resp(500), body=None), "server", None),
        (groq.NotFoundError("gone", response=resp(404), body=None), "gone", None),
        (groq.BadRequestError("bad", response=resp(400), body=None), "gone", None),
    ]
    for error, kind, retry in cases:
        try:
            polish.polish(SPOKEN, FakeClient(error=error))
        except polish.PolishError as exc:
            assert exc.kind == kind and exc.retry_after == retry, (type(error), exc.kind)
        else:
            raise AssertionError(f"{type(error).__name__} should fail")
    try:
        polish.polish("what's the best way to structure a python package for this",
                      FakeClient(answer="Here's how: use a src layout with a pyproject file."))
    except polish.PolishError as exc:
        assert exc.kind == "suspicious"
    else:
        raise AssertionError("a reply must be refused")
    print("PASS  Groq errors become auth / rate_limited / timeout / network / server; replies suspicious.")


def test_guard_refuses_realistic_replies():
    replies = [
        ("what is the capital of france", "Paris."),
        ("can you tell me what time it is", "I don't have access to the current time."),
        ("could you refactor the handle press function in app dot py",
         "I'd be happy to help refactor handle_press in app.py."),
        ("what does the trim silence function do", "It removes silence from the audio."),
        ("write me a haiku about autumn leaves falling down",
         "Crimson leaves drift down\nWhispering to the cold earth\nAutumn says goodbye"),
        ("fix the bug in the login handler and add a test for it",
         "I'll fix the bug in the login handler and add a test for it."),
        ("okay so um the settings page does not save the provider when you switch it and I "
         "think the problem is in the save handler",
         "The settings page doesn't save the provider when you switch it, and I think the "
         "problem is in the save handler.\n(Note: I removed filler words.)"),
        ("tell me a joke about programmers please", "Got it! Why do programmers prefer dark mode?"),
    ]
    for said, reply in replies:
        assert not polish.looks_like_cleanup(said, reply), (said, reply)
    print("PASS  the guard refuses realistic replies - answers, promises, notes, poems.")


def test_guard_keeps_cleanups_that_start_like_replies():
    keeps = [
        ("okay so here's what I want you to do next", "Here's what I want you to do next."),
        ("um sure that works for me", "Sure, that works for me."),
        ("i will send it over tomorrow morning", "I'll send it over tomorrow morning."),
        ("so I can't make it to the meeting today", "I can't make it to the meeting today."),
        ("uh I'm sorry I missed your call earlier", "I'm sorry I missed your call earlier."),
        ("okay of course we can move it to friday", "Of course we can move it to Friday."),
        ("surely you can see the problem here", "Surely you can see the problem here."),
        ("set the timeout to two hundred fifty milliseconds",
         "Set the timeout to 250 milliseconds."),
        ("we do not want to break the login flow", "We don't want to break the login flow."),
        ("open app dot py and find the handle press function",
         "Open app.py and find the handle_press function."),
    ]
    for said, cleaned in keeps:
        assert polish.looks_like_cleanup(said, cleaned), (said, cleaned)
    print("PASS  cleanups that start with the speaker's own 'sure' / 'here's' / 'I'll' are kept.")


def test_a_cut_off_answer_is_suspicious():
    client = FakeClient(answer=CLEAN)
    client._finish = "length"
    original = client._create

    def create(**kw):
        r = original(**kw)
        r.choices[0].finish_reason = client._finish
        return r
    client.chat.completions.create = create
    try:
        polish.polish(SPOKEN, client)
    except polish.PolishError as exc:
        assert exc.kind == "suspicious" and "cut off" in exc.detail
    else:
        raise AssertionError("a cut-off answer must not be typed")
    print("PASS  an answer cut off at the token cap is never typed.")


def test_budget_grows_with_the_take():
    assert polish.budget(40) == polish.TIMEOUT == 1.0
    assert abs(polish.budget(150) - 1.1) < 1e-9          # measured: ~0.47 s
    assert abs(polish.budget(400) - 2.1) < 1e-9          # measured: ~1.17 s
    assert polish.budget(2000) == polish.MAX_TIMEOUT == 4.0
    client = FakeClient(answer=" ".join(["word"] * 400))
    polish.polish(" ".join(["word"] * 400), client)
    assert abs(client.calls[0]["timeout"].read - 2.1) < 1e-9, "a long take gets a longer budget"
    print("PASS  the time budget grows with the take (1 s normally, up to 4 s).")


def test_guard_refuses_invented_words():
    # Seen live: ElevenLabs missed a word and polish made one up to fill the gap.
    said = ("All right. It looks like the application's still being loaded extremely. "
            "So, you may need to rerun the commands.")
    made_up = ("All right. It looks like the application is still loading extremely "
               "slowly, so you may need to rerun the commands.")
    assert not polish.looks_like_cleanup(said, made_up), "'slowly' was never said"
    assert polish.looks_like_cleanup(said, made_up.replace(" slowly", "")), \
        "'loaded' -> 'loading' is the same word"
    # Small grammar words and linking words are how a cleanup joins sentences.
    keeps = [
        ("This transcription pick this up?", "Did this transcription pick this up?"),
        ("so yeah pick a new name and adjust everything that relates to that name so the "
         "project name the repo name",
         "Pick a new name and adjust everything that relates to it, including the project "
         "name and the repo name."),
        ("i'm gonna send it over", "I'm going to send it over."),
        ("set the timeout to two hundred fifty milliseconds", "Set the timeout to 250 milliseconds."),
    ]
    for original, cleaned in keeps:
        assert polish.looks_like_cleanup(original, cleaned), (original, cleaned)
    # A Dictionary term may fix a mishearing: that's what the list is for.
    assert not polish.looks_like_cleanup("check the cal she market", "Check the Kalshi market.")
    assert polish.looks_like_cleanup("check the cal she market", "Check the Kalshi market.",
                                     terms=["Kalshi"])
    print("PASS  the guard refuses words the speaker never said (grammar words aside).")


def test_guard_refuses_lost_numbers_and_terms():
    assert not polish.looks_like_cleanup("set the limit to 250 per day please",
                                         "Set the limit per day, please.")
    assert polish.looks_like_cleanup("set the limit to 250 per day please",
                                     "Set the limit to 250 per day, please.")
    # (Dictionary words go to the transcriber too, so they arrive in their capitals.)
    assert not polish.looks_like_cleanup("okay so open the Scribe settings page now",
                                         "Open the settings page now.", terms=["Scribe"])
    assert polish.looks_like_cleanup("okay so open the Scribe settings page now",
                                     "Open the Scribe settings page now.", terms=["Scribe"])
    print("PASS  a number or Dictionary word the speaker said is never dropped.")


def test_guard_edge_cases():
    keeps = [
        ("i dunno if that works for them", "I don't know if that works for them."),
        ("he don't want to come to the meeting", "He doesn't want to come to the meeting."),
        ("gimme a minute to check the numbers", "Give me a minute to check the numbers."),
        ("ok that sounds good to me thanks", "Okay, that sounds good to me, thanks."),
        ("alright let's ship the release tonight", "All right, let's ship the release tonight."),
        ("the budget is 1000 for the whole trip", "The budget is $1,000 for the whole trip."),
        ("the train leaves at 3pm from platform two", "The train leaves at 3 PM from platform two."),
        ("i take the bus and then i'm taking the train", "I take the bus, and then I'm taking the train."),
        ("we do not want that", "We don't want that."),
        ("nope not today", "No, not today."),
        ("i run every day and he is running late", "I run every day, and he's running late."),
        ("we get there and then we're getting food", "We get there, and then we're getting food."),
    ]
    assert polish.looks_like_cleanup("let's meet at 3 no wait 4 pm on friday",
                                     "Let's meet at 3, no wait, 4 pm on Friday.", style="light")
    assert polish.looks_like_cleanup("the invoice is 2450 and we meet at 3 no wait 4 pm",
                                     "The invoice is 2,450, and we meet at 4 pm.")
    for said, cleaned in keeps:
        assert polish.looks_like_cleanup(said, cleaned), (said, cleaned)
    # Full smooths a spoken correction ("no wait"), numbers included; light keeps it.
    fix = ("let's meet at 3 no wait 4 on friday", "Let's meet at 4 on Friday.")
    assert polish.looks_like_cleanup(*fix)
    assert not polish.looks_like_cleanup(*fix, style="light")
    assert not polish.looks_like_cleanup("let's meet at 3 on friday and then 4",
                                         "Let's meet at 4 on Friday."), "a number without a correction stays"
    refuses = [
        ("the deal is approved by the board", "The deal is not approved by the board."),
        ("i can go to the meeting", "I can't go to the meeting."),
        ("i don't know but the deal is approved", "I don't know, but the deal is not approved."),
        ("the deal is not approved by the board", "The deal is approved by the board."),
        ("send the contact details to the team", "Send the contract details to the team."),
        ("i expect the team to finish today", "I expert the team to finish today."),
        ("we need to use the new car tomorrow", "We need the useless career tomorrow."),
        # "actually" and "sorry" are everyday words, not a correction: numbers stay.
        ("i actually think the budget is 5000 for the whole year",
         "I think the budget is 500 for the whole year."),
        ("sorry i'm late the invoice total is 2450 dollars", "Sorry I'm late. The invoice total is 2,540 dollars."),
        # A correction frees only the number taken BACK - not the one after
        # it, nor one in an earlier, unrelated part of the take.
        ("meet at 3 no wait 4 on friday", "Meet at 5 on Friday."),
        ("the invoice is 2450 and we meet at 3 no wait 4 pm",
         "The invoice is 2,540, and we meet at 4 pm."),
    ]
    for said, cleaned in refuses:
        assert not polish.looks_like_cleanup(said, cleaned), (said, cleaned)
    assert not polish.looks_like_cleanup("the deal is not approved yet by them",
                                         "The deal is approved yet by them.", style="light")
    # A Dictionary word counts only when it was said as that word: "Will" the
    # name, not "will" the verb; "New York" the phrase, not every "new".
    terms = ["Will", "New York"]
    assert polish.looks_like_cleanup("i will send the new draft to the team tomorrow",
                                     "I'll send the new draft to the team tomorrow.", terms=terms)
    assert polish.looks_like_cleanup("okay so the new plan is to ship it on monday",
                                     "The plan is to ship it on Monday.", terms=terms)
    assert not polish.looks_like_cleanup("okay so ask Will about the budget today",
                                         "Ask about the budget today.", terms=terms)
    assert not polish.looks_like_cleanup("we fly to New York on the first of june",
                                         "We fly on the first of June.", terms=terms)
    print("PASS  the guard allows everyday cleanups and catches flips and look-alikes.")


def test_light_style():
    full, light = polish.build_prompt(style="full"), polish.build_prompt(style="light")
    assert full == polish.build_prompt(), "full is the default"
    assert "smooth rambling" in full and "smooth rambling" not in light
    assert "Keep every other word" in light and "Never answer" in light
    said = "okay so um the thing is we should really ship it today"
    trimmed = "We should ship it today."
    assert polish.looks_like_cleanup(said, trimmed), "full may drop 'okay so the thing is'"
    assert not polish.looks_like_cleanup(said, trimmed, style="light"), "light keeps the words"
    assert polish.looks_like_cleanup(said, "Okay, so the thing is, we should really ship it today.",
                                     style="light")
    client = FakeClient(answer="Okay, so the thing is, we should really ship it today.")
    polish.polish(said, client, style="light")
    assert client.calls[0]["messages"][0]["content"].startswith(light.split(" Output")[0][:60])
    try:
        polish.polish(said, FakeClient(answer=trimmed), style="light")
    except polish.PolishError as exc:
        assert exc.kind == "suspicious"
    else:
        raise AssertionError("light must refuse a trimmed answer")
    print("PASS  light polish keeps every word but fillers; full may tidy harder.")


if __name__ == "__main__":
    test_guard_refuses_invented_words()
    test_guard_refuses_lost_numbers_and_terms()
    test_guard_edge_cases()
    test_light_style()
    test_prompt_rules_and_terms()
    test_request_parameters()
    test_tidy()
    test_looks_like_cleanup()
    test_should_polish()
    test_errors_map_to_kinds()
    test_guard_refuses_realistic_replies()
    test_guard_keeps_cleanups_that_start_like_replies()
    test_a_cut_off_answer_is_suspicious()
    test_budget_grows_with_the_take()
    print("\nAll polish tests passed.")
