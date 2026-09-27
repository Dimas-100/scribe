r"""
Standalone probe for dashboard.py's data handling (no window is opened).

Run from the project root:   venv\Scripts\python tests\dashboard_test.py
"""

import json
import os
import re
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="scribe-dash-test-")
os.environ["SCRIBE_DATA_DIR"] = TMP
for name in ("storage", "devices", "dashboard"):
    sys.modules.pop(name, None)
import storage    # noqa: E402
import dashboard  # noqa: E402


def _write(name, text):
    with open(os.path.join(TMP, name), "w", encoding="utf-8") as f:
        f.write(text)


def test_corrupt_config_never_loses_the_key():
    storage.save_config_changes({"groq_api_key": "gsk_precious"})
    _write("config.json", '{"groq_api_key": "gsk_precious",')          # damaged
    api = dashboard.JsApi()
    api.mark_milestones_seen(["count:10"])     # the auto-save that used to wipe it
    cfg, _ = storage.load_config()
    assert cfg["groq_api_key"] == "gsk_precious" and "count:10" in cfg["seen_milestones"]
    keys = [n["key"] for n in dashboard._take_notices()]
    assert "settings_recovered" in keys, keys
    print("PASS  a damaged config is recovered; the milestone save keeps the API key.")


def test_save_settings_errors_reach_the_page():
    api = dashboard.JsApi()
    try:
        api.save_settings({"hotkey": "Ctrl + Q"})
    except storage.StorageError as exc:
        assert "hotkey" in str(exc)
    else:
        raise AssertionError("invalid hotkey must raise")
    with mock.patch.object(storage, "atomic_write_json", side_effect=PermissionError("denied")):
        try:
            api.save_settings({"user_name": "Sam"})
        except storage.StorageError as exc:
            assert "Couldn't write" in str(exc)
        else:
            raise AssertionError("a write failure must raise, not report success")
    print("PASS  save_settings raises readable errors instead of a false 'Saved'.")


def test_vocab_save_failure_raises():
    api = dashboard.JsApi()
    with mock.patch.object(storage, "atomic_write_json", side_effect=PermissionError("denied")):
        try:
            api.add_vocab_term("Vercel")
        except storage.StorageError:
            pass
        else:
            raise AssertionError("vocab write failure must raise (page shows the error toast)")
    print("PASS  a failed vocabulary save raises (no more 'Added' on failure).")


def test_bad_log_lines_are_skipped():
    lines = [
        json.dumps({"timestamp": "2026-09-26T10:00:00", "text": "good", "words": 1, "duration": 1.0}),
        "null", "[1, 2]", "{broken",
        json.dumps({"timestamp": "2026-09-26T10:01:00", "text": 42}),
        json.dumps({"timestamp": "2026-09-26T14:02:00+00:00", "text": "tz aware", "words": "x"}),
        json.dumps({"timestamp": "not a date", "text": "bad ts"}),
    ]
    _write("dictation_log.jsonl", "\n".join(lines) + "\n")
    entries = dashboard.read_log()
    assert [e["text"] for e in entries] == ["good", "tz aware"], entries
    assert entries[1]["words"] == 2 and "+" not in entries[1]["timestamp"]
    _write("cloud_usage.jsonl", '{"timestamp": "2026-09-26T10:00:00", "audio_seconds": "abc"}\nnull\n')
    dashboard.summarize_cloud_usage()          # must not raise
    print("PASS  malformed history/usage lines are skipped; types are coerced.")


def test_initial_data_shape():
    with mock.patch.object(dashboard.devices, "list_input_devices", return_value=["Mic A"]), \
         mock.patch.object(dashboard, "build_app_icons", return_value={}):
        data = dashboard.JsApi().get_initial_data()
    assert data["choices"]["mics"] == ["(system default)", "Mic A"]
    assert data["choices"]["hotkeys"] == storage.HOTKEY_NAMES
    assert isinstance(data["notices"], list)
    print("PASS  get_initial_data uses the shared lists and carries notices.")


def test_webview2_detection_returns_bool():
    assert dashboard.webview2_installed() in (True, False)
    print(f"PASS  WebView2 detection works (installed={dashboard.webview2_installed()}).")


def _log(lines, mode="w"):
    with open(os.path.join(TMP, "dictation_log.jsonl"), mode, encoding="utf-8") as f:
        for text in lines:
            f.write(json.dumps({"timestamp": "2026-09-26T10:00:00", "text": text,
                                "words": len(text.split()), "duration": 1.0}) + "\n")


def test_suggestions_keep_the_users_spelling():
    entries = [{"text": t} for t in ["I bet on Kalshi today.", "Kalshi odds moved.",
                                     "check kalshi again", "Is Kalshi up?"]]
    s = dashboard.suggest_vocabulary(entries, [], {}, [])
    assert [x["word"] for x in s] == ["Kalshi"], s
    print("PASS  a suggestion keeps its most common mid-sentence spelling.")


def test_unicode_words_are_one_word():
    entries = [{"text": "Call José now."}, {"text": "José said hi."}, {"text": "ask José"}]
    s = dashboard.suggest_vocabulary(entries, [], {}, [])
    assert "José" in [x["word"] for x in s], s
    print("PASS  accented words stay whole.")


def test_names_bypass_the_common_word_list():
    assert "david" in dashboard.COMMON_WORDS
    entries = [{"text": f"I met David {i} times."} for i in range(3)]
    entries += [{"text": "the apple was red"}] * 3
    words = [x["word"] for x in dashboard.suggest_vocabulary(entries, [], {}, [])]
    assert "David" in words and "apple" not in words, words
    print("PASS  mostly-capitalized common words (names) are suggested; everyday ones aren't.")


def test_history_cache_appends_incrementally():
    _log(["first entry"])
    cache = dashboard.HistoryCache(os.path.join(TMP, "dictation_log.jsonl"))
    assert cache.refresh()[0] == "reset" and len(cache.entries_copy()) == 1
    assert cache.refresh() == ("same", [])
    _log(["second entry"], mode="a")
    kind, new = cache.refresh()
    assert kind == "appended" and [e["text"] for e in new] == ["second entry"], (kind, new)
    assert [e["text"] for e in cache.entries_copy()] == ["first entry", "second entry"]
    print("PASS  new dictations are read without re-reading the whole history.")


def test_history_cache_detects_rewrite():
    _log(["one", "two"])
    cache = dashboard.HistoryCache(os.path.join(TMP, "dictation_log.jsonl"))
    cache.refresh()
    # An undo rewrites the file (atomic replace = new file) and a new dictation
    # lands before the next poll: the file is LONGER than before, but different.
    storage.atomic_write_text(os.path.join(TMP, "dictation_log.jsonl"), "")
    _log(["one", "three is a longer line than two was"], mode="a")
    kind, _ = cache.refresh()
    assert kind == "reset", kind
    assert [e["text"] for e in cache.entries_copy()] == ["one", "three is a longer line than two was"]
    # Same file, rewritten in place: the bytes before the offset changed.
    _log(["uno", "dos"])
    _log(["tres"], mode="a")
    assert cache.refresh()[0] == "reset"
    assert [e["text"] for e in cache.entries_copy()] == ["uno", "dos", "tres"]
    print("PASS  a rewritten history is re-read in full, never half-parsed.")


def test_history_cache_suggestions_follow_new_entries():
    _log([])
    cache = dashboard.HistoryCache(os.path.join(TMP, "dictation_log.jsonl"))
    cache.refresh()
    assert cache.suggestions([], {}, []) == []
    _log(["Ping Vercel now", "Vercel deploys", "on Vercel again"], mode="a")
    cache.refresh()
    assert [x["word"] for x in cache.suggestions([], {}, [])] == ["Vercel"]
    print("PASS  suggestions update from the incremental index.")


def test_poll_sends_only_new_entries():
    _log(["alpha one"])
    dashboard.HISTORY = dashboard.HistoryCache(os.path.join(TMP, "dictation_log.jsonl"))
    api = dashboard.JsApi()
    with mock.patch.object(dashboard, "build_app_icons", return_value={}):
        first = api.poll_updates(None)
        assert first["changed"] and first["reset"] and len(first["entries"]) == 1
        gen, count = first["history_gen"], len(first["entries"])
        assert api.poll_updates(first["log_sig"], gen, count)["changed"] is False
        _log(["beta two"], mode="a")
        nxt = api.poll_updates(first["log_sig"], gen, count)
    assert nxt["changed"] and not nxt["reset"], nxt
    assert [e["text"] for e in nxt["appended"]] == ["beta two"]
    assert "entries" not in nxt and isinstance(nxt["suggestions"], list)
    print("PASS  a poll sends only the new dictations (and fresh suggestions).")


def test_add_term_reports_added_updated_exists():
    storage.save_vocab({"terms": ["kalshi"], "corrections": {}})
    api = dashboard.JsApi()
    with mock.patch.object(dashboard.instance, "send", return_value=True):
        assert api.add_vocab_term("Kalshi")["result"] == "updated"
        assert storage.load_vocab()[0]["terms"] == ["Kalshi"]
        assert api.add_vocab_term("kalshi")["result"] == "updated"      # back again
        assert api.add_vocab_term("kalshi")["result"] == "exists"
        assert api.add_vocab_term("Vercel")["result"] == "added"
    print("PASS  adding a term says whether it was added, re-cased or already there.")


def test_page_functions_cover_the_page_and_nothing_else():
    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    used = set(re.findall(r"pywebview\.api\.([a-z_]+)", html)) | set(re.findall(r"\ba\.([a-z_]+)\(", html))
    used = {u for u in used if hasattr(dashboard.JsApi, u)}
    assert used <= set(dashboard.PAGE_FUNCTIONS), used - set(dashboard.PAGE_FUNCTIONS)
    assert not [n for n in dashboard.PAGE_FUNCTIONS if n.startswith("_")]
    fns = dashboard.page_functions(dashboard.JsApi())
    assert [f.__name__ for f in fns] == list(dashboard.PAGE_FUNCTIONS)
    print("PASS  the page gets exactly the functions it calls - nothing private.")


def test_main_exposes_functions_without_a_js_api_object():
    window = mock.MagicMock()
    with mock.patch.object(dashboard, "webview2_installed", return_value=True), \
         mock.patch.object(dashboard.webview, "create_window", return_value=window) as create, \
         mock.patch.object(dashboard.webview, "start"):
        dashboard.main()
    assert "js_api" not in create.call_args.kwargs, "a js_api object lets dotted names walk into Python"
    exposed = [f.__name__ for f in window.expose.call_args.args]
    assert exposed == list(dashboard.PAGE_FUNCTIONS)
    html = create.call_args.kwargs["html"]
    assert "Content-Security-Policy" in html and "connect-src 'none'" in html
    print("PASS  the window exposes an explicit list, and the page has a strict CSP.")


def test_maximize_toggles_on_the_real_window_state():
    win = mock.MagicMock()
    with mock.patch.object(dashboard.webview, "windows", [win]), \
         mock.patch.object(dashboard, "_window_hwnd", return_value=1234), \
         mock.patch.object(dashboard.ctypes.windll.user32, "IsZoomed", return_value=1):
        dashboard.JsApi().toggle_maximize()
    win.restore.assert_called_once_with(); win.maximize.assert_not_called()
    win = mock.MagicMock()
    with mock.patch.object(dashboard.webview, "windows", [win]), \
         mock.patch.object(dashboard, "_window_hwnd", return_value=1234), \
         mock.patch.object(dashboard.ctypes.windll.user32, "IsZoomed", return_value=0):
        dashboard.JsApi().toggle_maximize()
    win.maximize.assert_called_once_with(); win.restore.assert_not_called()
    print("PASS  maximize/restore follows the window's real state.")


def test_icon_query_gives_up_on_a_hung_window():
    fake = mock.MagicMock()
    fake.SendMessageTimeoutW.return_value = 0          # timed out / hung
    with mock.patch.object(dashboard, "_u32", fake):
        assert dashboard._query_icon(99, 1) == 0
    args = fake.SendMessageTimeoutW.call_args.args
    assert args[4] & dashboard._SMTO_ABORTIFHUNG and args[5] <= 200
    print("PASS  asking a hung app for its icon times out instead of freezing.")


class _Page:
    """What the dashboard page keeps: its entries, history generation and
    log signature - updated from poll replies exactly as dashboard.html does."""

    def __init__(self, api):
        self.api = api
        data = api.get_initial_data()
        self.entries = list(data["entries"])
        self.gen = data["history_gen"]
        self.sig = data["log_sig"]

    def poll(self):
        r = self.api.poll_updates(self.sig, self.gen, len(self.entries))
        self.sig = r["log_sig"]
        if r["changed"]:
            if r["reset"]:
                self.entries = list(r["entries"])
            else:
                self.entries += r["appended"]
            self.gen = r["history_gen"]
        return [e["text"] for e in self.entries]


def _page(api):
    with mock.patch.object(dashboard, "build_app_icons", return_value={}), \
         mock.patch.object(dashboard.devices, "list_input_devices", return_value=[]):
        return _Page(api)


def test_other_calls_cannot_swallow_a_new_dictation():
    _log(["one", "two"])
    dashboard.HISTORY = dashboard.HistoryCache(os.path.join(TMP, "dictation_log.jsonl"))
    api = dashboard.JsApi()
    page = _page(api)
    _log(["three"], mode="a")                      # a dictation lands...
    with mock.patch.object(dashboard.instance, "send", return_value=True):
        api.add_vocab_term("Vercel")               # ...and a vocab call reads the log first
    with mock.patch.object(dashboard, "build_app_icons", return_value={}):
        assert page.poll() == ["one", "two", "three"]
    # An undo rewrites the file; a dismiss reads it before the next poll.
    storage.atomic_write_text(os.path.join(TMP, "dictation_log.jsonl"), "")
    _log(["one"], mode="a")
    api.dismiss_suggestion("whatever")
    with mock.patch.object(dashboard, "build_app_icons", return_value={}):
        assert page.poll() == ["one"]
    print("PASS  the page always ends up with exactly the file's dictations.")


def test_a_failed_read_changes_nothing_and_is_retried():
    _log(["one", "two"])
    dashboard.HISTORY = dashboard.HistoryCache(os.path.join(TMP, "dictation_log.jsonl"))
    api = dashboard.JsApi()
    page = _page(api)
    _log(["three"], mode="a")
    with mock.patch.object(dashboard.storage, "read_jsonl_from", return_value=None), \
         mock.patch.object(dashboard, "build_app_icons", return_value={}):
        assert page.poll() == ["one", "two"]          # couldn't read: nothing changes
    _log(["four"], mode="a")
    with mock.patch.object(dashboard, "build_app_icons", return_value={}):
        assert page.poll() == ["one", "two", "three", "four"], page.entries
    assert [e["text"] for e in dashboard.HISTORY.entries_copy()] == ["one", "two", "three", "four"]
    # A failed read on the very first load is retried too.
    _log(["solo"])
    dashboard.HISTORY = dashboard.HistoryCache(os.path.join(TMP, "dictation_log.jsonl"))
    with mock.patch.object(dashboard.storage, "read_jsonl_from", return_value=None):
        page = _page(dashboard.JsApi())
    assert page.entries == [] and page.sig is None
    with mock.patch.object(dashboard, "build_app_icons", return_value={}):
        assert page.poll() == ["solo"]
    assert storage.read_jsonl_from(os.path.join(TMP, "missing.jsonl"), 0) == ([], 0)
    print("PASS  a failed read leaves everything as it was and is tried again.")


def test_calendar_and_language_words_are_not_names():
    entries = [{"text": "See you on Monday at noon."}, {"text": "It ships next Friday."},
               {"text": "We met on Monday again."}, {"text": "Friday works for me."},
               {"text": "back in January then."}, {"text": "since January now."},
               {"text": "my English teacher said"}, {"text": "in English please"},
               {"text": "I met David 1 time."}, {"text": "I met David 2 times."}]
    words = [x["word"] for x in dashboard.suggest_vocabulary(entries * 2, [], {}, [])]
    assert not {"Monday", "Friday", "January", "English"} & set(words), words
    assert "David" in words, words
    print("PASS  weekdays, months and languages aren't suggested as names; people are.")


def test_cycle_start():
    from datetime import datetime
    oct14 = datetime(2026, 10, 14, 9, 30).timestamp()
    assert dashboard.cycle_start(oct14) == datetime(2026, 9, 14, 9, 30)
    mar31 = datetime(2027, 3, 31, 12, 0).timestamp()
    assert dashboard.cycle_start(mar31) == datetime(2027, 2, 28, 12, 0)
    now = datetime(2026, 9, 26, 15, 0)
    assert dashboard.cycle_start(None, now) == datetime(2026, 9, 1)
    print("PASS  the billing cycle starts one month before the reset (clamped).")


def test_usage_meter_numbers():
    from datetime import datetime, timedelta
    path = dashboard.CLOUD_USAGE_FILE
    now = datetime.now()
    rows = [(now - timedelta(days=2), 1800, "elevenlabs"),
            (now - timedelta(days=40), 999, "elevenlabs"),
            (now - timedelta(hours=1), 60, None)]
    with open(path, "w", encoding="utf-8") as f:
        for ts, sec, prov in rows:
            e = {"timestamp": ts.isoformat(timespec="seconds"), "audio_seconds": sec, "model": "m"}
            if prov:
                e["provider"] = prov
            f.write(json.dumps(e) + "\n")
    assert dashboard.summarize_cloud_usage()["total_calls"] == 1, "Groq rows only"
    storage.save_config_changes({"elevenlabs_api_key": ""})
    assert dashboard.JsApi().get_elevenlabs_usage() == {"status": "none"}
    storage.save_config_changes({"elevenlabs_api_key": "xi_meter_1234"})
    reset = (now + timedelta(days=10)).timestamp()
    acct = {"status": "ok", "used": 6000, "limit": 30000, "resets_at": int(reset), "tier": "starter"}
    with mock.patch.object(dashboard.elevenlabs_stream, "get_usage", return_value=acct), \
         mock.patch.object(dashboard.elevenlabs_stream, "CREDITS_PER_REALTIME_HOUR", 12000):
        u = dashboard.JsApi().get_elevenlabs_usage()
    assert u["status"] == "ok" and u["dictated_seconds"] == 1800, u
    assert abs(u["hours_left"] - 2.0) < 1e-9
    with mock.patch.object(dashboard.elevenlabs_stream, "get_usage",
                           return_value={"status": "no_permission"}):
        u = dashboard.JsApi().get_elevenlabs_usage()
    assert u["status"] == "no_permission" and u["dictated_seconds"] == 1800, u
    assert "hours_left" not in u
    storage.save_config_changes({"elevenlabs_api_key": ""})
    print("PASS  usage meter: account numbers + this cycle's streamed time; Groq rows apart.")

def test_page_uses_the_elevenlabs_functions():
    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    for name in ("check_elevenlabs_key", "open_elevenlabs_keys_page", "get_elevenlabs_usage"):
        assert f"api.{name}(" in html, name
    for id_ in ('id="set-provider"', 'id="set-ekey"', 'data-mode="elevenlabs"',
                'data-mode="groq"', 'id="eleven-usage"'):
        assert id_ in html, id_
    print("PASS  the page offers the service choice, the ElevenLabs key and the meter.")


def test_page_has_the_polish_switch():
    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    assert 'id="set-polish"' in html and "polish: toggleValue('set-polish')" in html
    assert storage.DEFAULT_CONFIG["polish"] is True
    print("PASS  Settings has the AI polish switch, and it is saved.")


def test_a_spelling_tie_goes_to_the_capitalized_form():
    entries = [{"text": t} for t in ["we met Zeno there", "we met zeno again",
                                     "ask Zeno now", "ask zeno later"]]
    s = dashboard.suggest_vocabulary(entries * 2, [], {}, [])
    assert [x["word"] for x in s] == ["Zeno"], s
    print("PASS  a spelling tie goes to the Capitalized form.")


def test_a_correction_updates_a_terms_spelling():
    storage.save_vocab({"terms": ["kalshi"], "corrections": {}})
    dashboard.JsApi().add_vocab_correction("cal she", "Kalshi")
    vocab = storage.load_vocab()[0]
    assert vocab["terms"] == ["Kalshi"], vocab["terms"]
    assert vocab["corrections"] == {"cal she": "Kalshi"}
    print("PASS  a correction updates the matching term's spelling.")


def test_polls_send_only_new_app_icons():
    _log(["alpha one"])
    with open(os.path.join(TMP, "dictation_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": "2026-09-26T10:00:01", "text": "in app a",
                            "app_exe": "C:/a.exe"}) + "\n")
    dashboard.HISTORY = dashboard.HistoryCache(os.path.join(TMP, "dictation_log.jsonl"))
    api = dashboard.JsApi()
    with mock.patch.object(dashboard, "extract_icon_data_uri", side_effect=lambda p: "data:" + p), \
         mock.patch.dict(dashboard._icon_cache, clear=True), \
         mock.patch.object(dashboard.devices, "list_input_devices", return_value=[]):
        first = api.get_initial_data()
        assert first["app_icons"] == {"C:/a.exe": "data:C:/a.exe"}
        with open(os.path.join(TMP, "dictation_log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-09-26T10:00:02", "text": "in app b",
                                "app_exe": "C:/b.exe"}) + "\n")
        nxt = api.poll_updates(first["log_sig"], first["history_gen"], len(first["entries"]))
    assert nxt["app_icons"] == {"C:/b.exe": "data:C:/b.exe"}, nxt["app_icons"]
    page = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    assert "Object.assign({}, state.appIcons, update.app_icons" in page, "the page merges them"
    print("PASS  a poll sends only the icons the page doesn't have yet.")


def test_a_same_size_edit_is_noticed():
    path = os.path.join(TMP, "dictation_log.jsonl")
    _log(["first entry here", "second entry"])
    cache = dashboard.HistoryCache(path)
    assert cache.refresh()[0] == "reset"
    data = open(path, "rb").read().replace(b"first entry here", b"FIRST ENTRY HERE")
    st = os.stat(path)
    with open(path, "r+b") as f:          # in place, same size (like Notepad)
        f.write(data)
    os.utime(path, (st.st_atime + 5, st.st_mtime + 5))
    what, entries = cache.refresh()
    assert what == "reset" and entries[0]["text"] == "FIRST ENTRY HERE", (what, entries)
    print("PASS  an in-place edit of the same size is noticed.")


def test_page_leftovers():
    page = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    assert "function butClause(" in page, "D4: 'Saved - but your...'"
    assert "state.pendingSuggestions = null; renderDictionary()" in page, "D5"
    assert "restoreHistoryFocus(" in page, "D6"
    assert "$('#set-ekey-field').hidden" in page.split("for (const [service, row] of Object.entries(KEY_ROWS))")[2], "D7"
    print("PASS  the page's leftovers are in (checked live in Chrome too).")


def test_the_window_opens_in_the_theme_colour():
    light, dark = dashboard.WINDOW_BACKGROUND["light"], dashboard.WINDOW_BACKGROUND["dark"]
    assert dashboard.window_background({"theme": "light"}) == light
    assert dashboard.window_background({"theme": "dark"}) == dark
    with mock.patch.object(dashboard, "_windows_uses_light_theme", return_value=True):
        assert dashboard.window_background({"theme": "system"}) == light
    with mock.patch.object(dashboard, "_windows_uses_light_theme", return_value=False):
        assert dashboard.window_background({}) == dark
    print("PASS  the window's first paint matches the theme (no flash).")


def _theme_tokens(page):
    """{"light": {...}, "dark": {...}} - the page's CSS custom properties."""
    def block(selector):
        body = page.split(selector + " {", 1)[1].split("}", 1)[0]
        return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", body))
    light = block(":root")
    dark = dict(light, **block(':root[data-theme="dark"]'))
    return {"light": light, "dark": dark}


def _contrast(a, b):
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_both_themes_are_readable():
    page = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    for theme, t in _theme_tokens(page).items():
        for fg in ("text", "text-2", "text-3", "err", "warn", "ok", "accent"):
            for bg in ("panel", "bg", "sidebar"):
                assert _contrast(t[fg], t[bg]) >= 4.5, (theme, fg, bg, round(_contrast(t[fg], t[bg]), 2))
        # Text on coloured fills: the primary button and the warning toast.
        assert _contrast(t["on-accent"], t["accent"]) >= 4.5, theme
        assert _contrast(t["on-warn"], t["warn"]) >= 4.5, (theme, round(_contrast(t["on-warn"], t["warn"]), 2))
        # Controls (WCAG 3:1 for UI parts): an OFF switch's outline.
        assert _contrast(t["control-off"], t["panel"]) >= 3, (theme, round(_contrast(t["control-off"], t["panel"]), 2))
    assert ".toast.warn { background: var(--warn); color: var(--on-warn); }" in page
    print("PASS  both themes: text, fills and switches meet their contrast minimums.")


def test_log_entries_new_fields_are_typed():
    good = dashboard._clean_log_entry({"timestamp": "2026-09-26T10:00:00", "text": "hi there",
                                       "engine": "groq", "latency": 0.61, "raw": "um hi there",
                                       "polished": True})
    assert (good["engine"], good["latency"], good["raw"], good["polished"]) == ("groq", 0.61, "um hi there", True)
    bad = dashboard._clean_log_entry({"timestamp": "2026-09-26T10:00:00", "text": "hi there",
                                      "engine": 5, "latency": "fast", "raw": 7, "polished": "yes"})
    for key in ("engine", "latency", "raw", "polished"):
        assert key not in bad, (key, bad)
    odd = dashboard._clean_log_entry({"timestamp": "2026-09-26T10:00:00", "text": "hi",
                                      "latency": float("nan"), "polished": True})
    assert "latency" not in odd and "polished" not in odd, odd   # polished needs the words said
    print("PASS  a hand-edited log can't break the page with wrong-typed new fields.")


def test_the_page_paints_in_the_saved_theme():
    for theme in ("light", "dark", "system"):
        html = dashboard.page_html({"theme": theme})
        assert f'<html lang="en" data-saved-theme="{theme}">' in html, theme
    assert 'data-saved-theme="system"' in dashboard.page_html({"theme": "<script>"})
    page = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    head = page.split("</head>", 1)[0]
    assert "dataset.savedTheme" in head, "the pre-paint script reads the saved theme"
    print("PASS  the page's first paint uses the saved theme (no light-dark-light flash).")


def test_minimizing_tells_the_page():
    class Hook(list):
        def __iadd__(self, fn):
            self.append(fn)
            return self
    class Events:
        minimized, restored, maximized = Hook(), Hook(), Hook()
    class FakeWindow:
        events = Events()
        def __init__(self):
            self.js = []
        def evaluate_js(self, code):
            self.js.append(code)
    win = FakeWindow()
    dashboard._watch_minimize(win)
    for fn in win.events.minimized:
        fn()
    for fn in win.events.restored:
        fn()
    assert win.js == ["window.scribeMinimized = true", "window.scribeMinimized = false"], win.js
    page = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    assert "function pageHidden()" in page and "window.scribeMinimized" in page
    print("PASS  minimizing the window pauses the page's polling (not only document.hidden).")


def test_page_scripts_parse():
    """One stray quote stops the whole page from booting, and nothing else
    here runs the page's JavaScript - so parse it with Node when it's
    installed (skipped, and said so, when it isn't)."""
    import shutil, subprocess, tempfile
    node = shutil.which("node")
    if not node:
        print("SKIP  the page's scripts weren't parsed (Node isn't installed).")
        return
    page = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts
    for i, source in enumerate(scripts):
        path = os.path.join(tempfile.gettempdir(), f"scribe_dashboard_script_{os.getpid()}_{i}.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        try:
            result = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=30)
        finally:
            os.remove(path)
        assert result.returncode == 0, result.stderr
    print(f"PASS  the page's {len(scripts)} scripts parse.")


def test_a_theme_click_saves_only_the_theme():
    storage.save_config_changes({"user_name": "Sam", "hotkey": "Ctrl + Alt", "theme": "system"})
    with mock.patch.object(dashboard, "signal_reload"):
        cfg = dashboard.JsApi().save_settings({"theme": "dark"})
    assert cfg["theme"] == "dark"
    saved = dashboard.read_config()
    assert saved["theme"] == "dark" and saved["user_name"] == "Sam" and saved["hotkey"] == "Ctrl + Alt", saved
    page = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    assert "save_settings({ theme: state.themePref })" in page
    storage.save_config_changes({"theme": "system"})
    print("PASS  picking a theme saves just the theme; everything else is untouched.")


def test_page_redesign():
    page = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    # The theme control is there, and its choice is saved with the rest.
    assert 'id="set-theme"' in page and "payload.theme = state.themePref" in page
    for theme in ("system", "light", "dark"):
        assert f'data-theme="{theme}"' in page, theme
    # Nothing left of the old look.
    lowered = page.lower()
    for gone in ("confetti", "voice profile", "medal", "--glow", "milestone"):
        assert gone not in lowered, gone
    # The CSP is exactly what it was: no network, nothing external.
    assert ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; font-src data:; connect-src 'none'; base-uri 'none'; "
            "form-action 'none'") in page
    # Every element the script looks up by id exists in the markup (a renamed
    # element would otherwise just make a handler throw).
    script = page.split("<script>")[-1]
    markup = page.split("<script>")[1].split("</script>", 1)[1].split("<script>")[0]
    wanted = set(re.findall(r"\$\$?\('#([a-z0-9-]+)", script))
    wanted |= set(re.findall(r"(?:setToggle|toggleValue|getElementById)\('([a-z0-9-]+)'", script))
    have = set(re.findall(r'id="([a-z0-9-]+)"', markup))
    assert wanted <= have, sorted(wanted - have)
    for key in ("set-key", "set-ekey"):      # the key rows' ids are built from KEY_ROWS
        for suffix in ("", "-saved", "-replace", "-remove"):
            assert key + suffix in have, key + suffix
    # The mic is still starting (a Bluetooth headset): the page says so.
    assert "act === 'connecting'" in page and "st.activity === 'connecting'" in page
    print("PASS  the redesigned page: theme saved, old look gone, CSP kept, every id there.")


if __name__ == "__main__":
    test_page_scripts_parse()
    test_both_themes_are_readable()
    test_log_entries_new_fields_are_typed()
    test_the_page_paints_in_the_saved_theme()
    test_minimizing_tells_the_page()
    test_page_redesign()
    test_a_theme_click_saves_only_the_theme()
    test_corrupt_config_never_loses_the_key()
    test_save_settings_errors_reach_the_page()
    test_vocab_save_failure_raises()
    test_bad_log_lines_are_skipped()
    test_initial_data_shape()
    test_webview2_detection_returns_bool()
    test_suggestions_keep_the_users_spelling()
    test_unicode_words_are_one_word()
    test_names_bypass_the_common_word_list()
    test_history_cache_appends_incrementally()
    test_history_cache_detects_rewrite()
    test_history_cache_suggestions_follow_new_entries()
    test_poll_sends_only_new_entries()
    test_add_term_reports_added_updated_exists()
    test_page_functions_cover_the_page_and_nothing_else()
    test_main_exposes_functions_without_a_js_api_object()
    test_maximize_toggles_on_the_real_window_state()
    test_icon_query_gives_up_on_a_hung_window()
    test_other_calls_cannot_swallow_a_new_dictation()
    test_a_failed_read_changes_nothing_and_is_retried()
    test_calendar_and_language_words_are_not_names()
    test_cycle_start()
    test_usage_meter_numbers()
    test_page_uses_the_elevenlabs_functions()
    test_page_has_the_polish_switch()
    test_a_spelling_tie_goes_to_the_capitalized_form()
    test_a_correction_updates_a_terms_spelling()
    test_polls_send_only_new_app_icons()
    test_a_same_size_edit_is_noticed()
    test_page_leftovers()
    test_the_window_opens_in_the_theme_colour()
    print("\nAll dashboard tests passed.")
