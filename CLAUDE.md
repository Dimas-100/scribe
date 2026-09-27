# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scribe is a voice dictation app for Windows. Hold a global hotkey to record,
release to transcribe and auto-type the text at the cursor. Transcription runs
on-device by default (faster-whisper, on CPU); an optional cloud mode uses the
user's own API key: Groq (the recording is sent on release) or ElevenLabs Scribe
v2 Realtime (streamed WHILE the hotkey is held; Groq is then the backup). Any
cloud failure falls back to the next backend - Groq, then the local model. User-facing docs are in `README.md`; the license is PolyForm
Noncommercial (`LICENSE.md`). The repo is PUBLIC (github.com/Dimas-100/scribe)
and meant to work for anyone who downloads it, so never commit personal data:
no real dictations, names, machines, account details or key fragments - in
code, tests, comments or commit messages. Design notes, plans and reviews
(`docs/superpowers/`, `docs/reviews/`) are gitignored and stay on the
developer's PC for exactly that reason; public docs go in `docs/` (e.g.
`docs/notifications.md`, `docs/images/` - screenshots made from demo data only).

## Running

```
python app.py --show    # console with status messages; opens the dashboard
pythonw app.py          # NO console, tray only (how it starts at sign-in)
```

`--show` = "the user asked to see Scribe" (the Start-menu and pinned
shortcuts pass it); without it Scribe starts quietly in the tray. A second
launch exits at once (with `--show` it first asks the running copy to show
its window). `setup.bat` is the one-step install/update: it refuses to run
from inside a ZIP, warns inside OneDrive, rebuilds a venv that no longer
runs, uses a 64-bit x64 Python 3.11-3.13 via the `py` launcher - or, when
there is none, downloads a pinned, SHA-256-checked `uv` into `.tools\` and
lets it fetch a private x64 Python 3.11 there (gitignored; nothing installed
system-wide) - then requirements, a Visual C++ runtime check (`import
ctranslate2, onnxruntime`; if it fails AND Windows can't load
msvcp140.dll, Microsoft's vc_redist.x64.exe is downloaded and run - UAC
asks; if the runtime loads, the real import error is shown instead - no
pointless UAC prompt), `install_shortcut.py` (skipped for a
portable copy: `SCRIBE_DATA_DIR` set) and start. app.py wraps its heavy
imports: a library that won't load ends in a message box, not silence. The venv lives in
`venv/` (or `.venv/`) inside the project folder. Dependencies are in
`requirements.txt`.

**Distribution.** Friends download `Scribe.zip` from the latest GitHub
release (the README's link), extract it and double-click `setup.bat`.
`update.bat` / Settings -> About -> "Update now" run `updates.py --install`
(download the release ZIP and unpack it into a temp folder FIRST - a broken
download never touches Scribe - then ask Scribe to quit over the pipe,
`{"cmd": "quit"}`, repeated; `git pull --ff-only` for a clone, else
`install_files()` swaps the files in ALL OR NONE - each old file moved to
`.update-old`, the new one renamed in, everything put back if any file is
locked; then the NEW `setup.bat` via `cmd /c call`; any failure after the
quit restarts Scribe, and only an un-undoable mix (`PartialUpdate`) says
"run update.bat again"); the running app checks GitHub once a day (`_update_watcher`,
setting `check_updates`, one notice per version via `update_notified`).
`uninstall.bat` -> `uninstall.py` removes the Run entry and this copy's
shortcuts and asks before deleting data, keys or models. The daily check
starts OFF for "On this PC" users (the welcome promises offline). There is
no Windows Sandbox on the dev PC: a truly clean first install (no Python,
no VC++ runtime, SmartScreen) has only been simulated (PATH without Python;
the runtime branch with a stubbed installer). **Releasing:** bump
`version.py`, commit, tag `v<version>`, then
`git archive --prefix=Scribe/ -o Scribe.zip v<version>` (`.gitattributes`
`export-ignore` keeps tests/docs/CLAUDE.md out) and
`gh release create v<version> Scribe.zip` - the asset MUST be named
`Scribe.zip` (updates.py and the README download it by that name).

There is no build step or linter, and no pytest suite. The `tests/` folder
holds standalone `*_test.py` scripts, run directly from the project root
(`venv\Scripts\python tests\storage_test.py`); each ends with an
"All ... passed." line.

- **Safe anytime** (temp `SCRIBE_DATA_DIR`, model/mic/network/message boxes
  mocked): `storage_test`, `devices_test`, `startup_test`, `notify_test`,
  `mic_handling_test`, `cloud_test`, `dashboard_test`, `input_test`,
  `delivery_test`, `clipboard_test` (uses the real clipboard but restores
  it), `overlay_test`, `keystore_test` (one real Credential Manager
  round-trip on a throwaway name), `instance_test`, `autostart_test`
  (throwaway registry keys), `model_manager_test`, `model_flow_test`,
  `first_run_test`, `welcome_test` (its one real-vault test points EVERY service at
  `Scribe-test/...` names, and a guard refuses any other read, write or delete),
  `elevenlabs_stream_test` (a fake ElevenLabs server on 127.0.0.1),
  `streaming_flow_test`, `polish_test`, `polish_flow_test` (fake Groq
  client), `updates_test` (fake GitHub answers, a local HTTP server, temp
  folders), `uninstall_test` (temp shortcuts; registry and vault untouched
  under an override folder). The harness's config has `polish: false`, and
  it blocks httpx and `urllib.request.urlopen`, so no test can reach the
  real Groq or GitHub by accident. Any test that imports `app.py` MUST go
  through `tests/harness.py` (`import_app_with_mocks`) — it stubs the model
  (never a download) and joins the loader thread; an unmocked import on a
  fresh checkout waits for the welcome and would load the real model. The
  mutex/pipe names derive from the data folder, so a test (temp folder)
  can never reach the running Scribe.
- **Interactive / network** probes: `mic_test.py`, `hotkey_test.py`,
  `typing_test.py` (types into the focused window), `whisper_test.py` use
  the real mic or keyboard; `download_probe.py` really downloads `tiny.en`
  into a throwaway cache to check download progress; `elevenlabs_probe.py`
  streams a Windows-voice sentence to the REAL ElevenLabs service (key from
  Credential Manager) and prints release->text time (`--usage` measures credits
  per hour; needs the key's User -> Read permission); `polish_probe.py` times
  the real AI polish on synthetic dictations (Groq key from Credential
  Manager).

Troubleshooting: start Scribe with `SCRIBE_SAVE_TAKES=1` and every take is
kept as `debug-takes\<time>.wav` + `.json` (captured vs held seconds, input
overflows, level) in the data folder - to check a bad transcript against
what the mic really delivered (e.g. run it through local Whisper). Voice
recordings: delete them when done.

`requirements.txt` pins every dependency with `~=`; bump versions on purpose
after re-running the tests.

## Architecture

The tray app is one file: `app.py` (hotkey, recording, transcription,
delivery). The dashboard is a separate process — `dashboard.py` (a pywebview
window plus the Python API its page calls) and `dashboard.html` — launched
by `app.py`; the two share the JSON files on disk, and the dashboard talks
to the app over its named pipe (`instance.py`: `reload-config` after saving
settings, `setup-done`, `retry-model`, `status` for live progress; a second
launch sends `show`). On a first run the dashboard opens as the **welcome**
(`dashboard.py --welcome`, a full-window view inside `dashboard.html`).
The page can call only `dashboard.PAGE_FUNCTIONS` (handed over with
`window.expose`, never as a `js_api` object, so no dotted name can walk into
Python) and runs under a strict CSP (no network). The page's look is one set
of CSS tokens per theme (`:root` light, `:root[data-theme="dark"]`); the
`theme` setting (system / light / dark) is saved the moment it's picked, and
`dashboard.window_background()` gives the window the same colour before the
page paints. Pages: **Home** (a live card, today vs your usual day, 30 days,
recent history), **Insights** (pace, speed, polish, hours, apps, calendar,
top words - all computed in the page from the history entries),
**Dictionary**, **Settings** (incl. cloud **Usage** and **About**: version, updates). Live data is polled,
only while the window is visible: `get_setup_status` every 800 ms (the app's
`status` reply carries `activity` - recording / transcribing / idle - and
`provider`; it feeds the sidebar, Home's live card and Settings' model line)
and `poll_updates` every 1 s (every 5 s while hidden). `dashboard_test`
checks that every id the script looks up exists in the markup and, when
Node is installed, that the page's scripts parse. History is read through
`dashboard.HISTORY`, an incremental cache (new lines only; a rewritten file -
new id, shrunk, or changed tail bytes - is re-read in full) that also keeps
the suggestion index. `common_words.py` is the word list behind the
dashboard's vocabulary suggestions (a word Capitalized in most of its
mid-sentence uses counts as a name and bypasses it). Small shared modules sit under both processes:

- `storage.py` — where user data lives (`%APPDATA%\Scribe`, or
  `SCRIBE_DATA_DIR` if set) and how it is saved: atomic writes (temp file +
  `os.replace`, retrying while Windows holds the file open), validated
  settings/vocabulary with last-good `.bak` recovery (a damaged file is kept
  as `*.corrupt-<stamp>.json`, never overwritten), JSON Lines helpers, the
  shared error log, one-time migration out of the code folder, and the
  single source of truth for `DEFAULT_CONFIG`, `HOTKEY_NAMES`,
  `MODEL_CHOICES`.
- `devices.py` — usable microphones (one host API, 16 kHz-capable, each
  once), resolving saved mic names (incl. old truncated MME names), PortAudio
  refresh, and cheap change signals (Core Audio default-mic id via ctypes COM,
  waveIn device count). `raw_twin()` / `raw_settings()`: `app.make_stream()`
  opens the chosen mic through its WASAPI entry in RAW mode first - a
  laptop's own noise suppression (Dolby, on a Lenovo) chopped speech so
  badly ElevenLabs heard "Microbox, near to everything" for a whole sentence
  it got word for word in raw - and the usual way when raw isn't offered.
  Raw audio can be very quiet, so long-take pauses are judged per take
  (`elevenlabs_stream.quiet_threshold`, in dB between the take's own quiet
  and loud levels), never by a fixed level.
- `keystore.py` — API keys in Windows Credential Manager (ctypes advapi32),
  one per service (`SERVICES`: `Scribe/groq`, `Scribe/elevenlabs`; every
  function takes `service="groq"`); `resolve_key(cfg, service)` = vault, else
  config.json (fallback when `SCRIBE_DATA_DIR` is set or the vault refuses);
  one-time verified migration out of config.json.
- `elevenlabs_stream.py` — ElevenLabs Scribe v2 Realtime over a websocket
  (`websockets` sync client): `Session(key, terms)` with `start()` (connects
  on its own thread), `feed(chunk)` (a queue put - safe from the mic
  callback), `finish(timeout)` (send the rest + commit, return the text) and
  `cancel()`; failures raise `StreamError(kind)` (auth, quota, busy, terms,
  network, timeout, server, broken). Long takes are committed in pieces
  (after 20 s at a 0.3 s pause, at 30 s regardless): ElevenLabs' own commit
  at ~36 s returns text with most of the take missing (seen live), and
  `finish()` waits until every commit sent has been answered; a last tail
  under 0.3 s is padded with silence (ElevenLabs refuses a smaller commit
  and hangs up). Also `keyterms()` (Dictionary terms,
  ElevenLabs' 50 x 20-char limits), `check_key()` (free) and `get_usage()`
  (the account's credits; needs User -> Read). No app state.
- `polish.py` — AI polish (Wispr-style cleanup) with Groq `qwen/qwen3.8-27b`,
  reasoning off, temperature 0, a time budget by length (`budget()`: 1 s
  everyday, up to 4 s for a 5-minute take), in a style (`polish_style`
  setting: "full" may drop false starts and "okay so"s; "light" only fixes
  punctuation, capitals and filler sounds): `polish(text, client, terms,
  name, timeout, style)` returns the cleaned text or raises `PolishError(kind)`
  (auth = 401 key rejected, forbidden = 403 model not allowed -> polish-only
  pause, rate_limited, timeout, network, server, suspicious);
  `looks_like_cleanup()` refuses answers where under half the words are the
  speaker's, reply openers the speaker didn't say early, Markdown or "Note:"
  lines, cut-off answers, wild length changes, INVENTED words (a real word
  the speaker never said - joining words, numbers, Dictionary terms, what a
  spoken shortcut stands for ("dunno" -> "don't know") and other forms of a
  said word ("loaded"/"loading", not "contact"/"contract") don't count; seen
  live: "extremely" -> "extremely slowly"), a changed yes/no (negations are
  COUNTED: none may be added - "approved" -> "not approved", "can" ->
  "can't" - light may drop none, full may not drop them all; the "no" of
  "no wait" doesn't count), a lost number (in full style a number said
  before "no wait" / "I mean" / "scratch that" may go - never after, and
  "actually"/"sorry" are not corrections) or a lost Dictionary term (only
  when said as that term: "Will" not "will", "New York" not "new"), and -
  light style - more than 1 + 10% of the speaker's words dropped (on the
  developer's real history ~10% of full-style polishes are refused, all long
  rewrites that add words); `should_polish()` skips
  takes of 3 words or fewer. No app state. In app.py, `_warm_polish()`
  opens Groq in the background at start and after a settings reload (the
  first polish shouldn't pay for `import groq` + a TLS handshake); a retired
  model (404/400), a 403 or a broken install pauses polish until the Groq
  key or the polish switch changes.
- `instance.py` — the per-user single-instance mutex and the JSON named-pipe
  control channel (names = hash of the data folder; bytes only, never pickle).
- `autostart.py` — "Start Scribe when I sign in" via the HKCU `Run` value
  (honors Task Manager's StartupApproved switch; the registry is the source
  of truth).
- `version.py` — `VERSION`, shown in Settings -> About and compared with
  the latest GitHub release.
- `updates.py` — `check()` (GitHub's latest release vs `VERSION`; never
  raises: available / current / offline / error) and `install()` (the
  updater - see Distribution above; `apply_zip()` writes only `Scribe/...`
  files, never into venv/.tools/.git or outside the folder).
- `model_manager.py` (app only) — the local Whisper model on a background
  thread: find in cache (no network) / download with byte progress (plain
  HTTPS, not Xet) / load / swap; states waiting, loading, downloading, ready,
  failed, off.

The important thing to understand in `app.py` is its **threading model**,
because almost every bug class here is a cross-thread one.

The main threads:

- **Main thread** — owns Tkinter. The hidden `root` window, the status
  overlay AND the tray icon's picture/tooltip may *only* be touched here
  (`update_status()` just queues; `_apply_status()` runs here).
  `root.mainloop()` blocks the main thread until shutdown.
- **Keyboard listener thread** (`pynput`) — runs inside Windows' low-level
  keyboard hook. `on_press` / `on_release` do nothing but drop REAL key
  events into `key_events` (events flagged `injected` — Scribe's own Ctrl+V,
  Backspace, Shift+Enter — are ignored). Never do work here: a slow hook is
  silently removed by Windows and the hotkey dies.
- **Input controller thread** (`_input_controller`) — the ONLY writer of
  `pressed`, `recording`, `recording_started_at`, `_hold_consumed`,
  `_blocked_notice` and `_next_job_seq`. `_handle_press` / `_handle_release` / `_handle_check`
  implement the hotkey: stale modifiers are dropped using the physical key
  state (`_key_is_down`, GetAsyncKeyState); hotkey + any other key cancels
  (it's a shortcut, not dictation); a used-up hold (`_hold_consumed`) can't
  restart until a hotkey key is released; a missed key-up or the 5-minute
  limit finishes the recording normally. A press that can't record
  (`_dictation_blocker()`: setup pending, model downloading/failed) is
  explained on RELEASE - and not at all if another key joined in (a
  Windows shortcut).
- **Tray icon thread** (`pystray`) — runs the system-tray icon and its menu.
- **Model loader thread** (`model_manager`) — finds/downloads/loads the
  model. Its `on_change` (`_on_model_change`) runs on whichever thread
  changed the state (the loader; the main thread on a settings reload; the
  pipe thread on `retry-model`), so it only calls thread-safe `notify` and
  `_refresh_status()`. A dictation waits only for a *loading* model
  (`wait_loaded`), never a download. Read the model through
  `_local_model()` — the manager swaps it when the size changes.
- **Control pipe thread** (`instance.serve` → `_handle_control`) — answers
  `status` directly and routes everything else through `ui_queue` (incl.
  the updater's `quit`). `instance.is_running()` asks whether a Scribe runs
  with `OpenMutexW` - it never creates the mutex, so asking can't race a
  starting Scribe (setup.bat / the updater wait on it).
- **Update watcher** (`_update_watcher`) — 90 s after start, then daily:
  `_check_for_update()` notifies once per new version.
- **ElevenLabs session threads** (one per streamed dictation, in
  `elevenlabs_stream`) — `start_recording()` opens one when ElevenLabs is the
  usable service (`_eleven_configured()` + not paused, or the only backend);
  `_stream_session` is written only by the controller (start / finish /
  cancel) and only READ by `audio_callback`, which feeds it each chunk. The
  mic is stopped before `_finish_recording()` hands the session to the
  worker, so the stream always holds the whole take.
- **Per-dictation worker threads** — `_finish_recording()` spawns
  `process_audio(audio, hwnd, app_name, app_exe, seq)` with everything
  captured at release. Jobs deliver in spoken order (`_wait_turn` /
  `_finish_turn` on `seq`; skipped jobs still finish their turn), after the
  modifiers are physically released, into a window `restore_target_window()`
  has verified is in front — otherwise the text goes to the clipboard with a
  notice and undo is not armed. `_in_flight` + `_refresh_status()` decide
  what the status shows (recording > transcribing > idle).

Background daemons also run: the watchdog (`_recording_watchdog`, which only
posts `("check", None)` to the controller every second) and the **device
watcher** (`_device_watcher`, every 2 s), which reopens the mic after a
plug/unplug or Windows default change — only while idle, and only under
`mic_lock`, which guards every open/close/start/stop of `stream` (sound cues
and device-name lookups too).

**Crossing threads safely:** background threads never touch Tkinter directly.
They drop a command into the `ui_queue` (a `queue.Queue`). `poll_ui_queue()`
runs on the main thread every 100 ms, drains the queue, and does the real UI
work. The tray menu callbacks (`on_open_dashboard`, `on_quit`) and
`update_status()` follow this pattern. If you add UI work triggered from a
background thread, route it through `ui_queue` — do not call Tkinter directly.

**Dictation flow:** hotkey down → (controller) `_ready_to_dictate()` (setup
pending, or no cloud and no local model yet → a notice, nothing recorded; a
model still *loading* is fine — `transcribe_audio()` waits for it) →
`start_recording()` (captures
the focused window handle and app, starts the mic stream) → audio chunks accumulate in `frames`
via `audio_callback` → hotkey up → `stop_recording()` → worker thread runs
`process_audio()` → Whisper transcribe (cloud path: `trim_silence()` first
cuts out non-speech, because Whisper invents words like "Thank you for
watching!" in silence; a take with no speech never reaches Groq) →
**post-processing pipeline** →
(ElevenLabs as the service: `_transcribe_take()` finishes the stream instead -
text in ~0.2 s - and on any `StreamError` notifies once via
`_handle_eleven_error()` and sends the same audio down the Groq/local path) →
`restore_target_window()` brings the original window back to focus → text
delivered via `deliver()` (paste or type, per `PASTE_MODE`) →
`log_dictation()` appends to the log (with `engine` and `latency` - release to
delivery - for measuring speed). Exception: if the whole dictation is a
voice command (`whole_utterance_command()`), the pipeline is skipped and the
line break is delivered directly.

**Post-processing pipeline:** raw transcription text first goes through AI
polish (`_polish_take()` → `polish.polish()`: only with `POLISH` on, the
cloud on, Groq usable and polish not paused; on any failure the text stays
as spoken, `_handle_polish_error()` notifies once per kind and pauses ONLY
polish, never Groq transcription; the history keeps `raw` + `polished`),
then, in this exact order, `remove_fillers()` → `clean_text()` →
`apply_vocabulary()` → `apply_voice_commands()` - so Dictionary corrections
always win over the model. Whole-utterance commands/actions are matched
before polish and never sent to it. "Fix that" (`ai_fix_last_output()`) is
polish on demand (5 s budget, any length, says when it can't). The order matters: filler removal leaves gaps that
`clean_text()` tidies; `apply_vocabulary()` runs after `clean_text()` so its
exact casing isn't re-touched by the capitalization rules; and
`apply_voice_commands()` runs last because `clean_text()` would otherwise
collapse the newlines it inserts. Each stage is a pure string function —
test them in isolation. New post-processing belongs in this chain.

## Key conventions

- **Settings precedence:** defaults live in `storage.DEFAULT_CONFIG`; the
  SETTINGS constants in `app.py` mirror them (`startup_test.py` fails if they
  drift). `load_config()` runs at import time and applies the validated
  `config.json` (a bad value resets only itself). The dashboard's Settings
  tab saves through `storage.save_config_changes()` and pings
  `reload-config`. Everything applies live — a new mic at the next idle
  moment, a new model size once it has loaded in the background
  (`_apply_model_settings()`). `local_model` false = cloud-only (no local
  model); turning the cloud off always turns it back on.
- **API keys never go in config.json** (except the keystore's fallbacks)
  and never reach `dashboard.html`: the page gets `groq_key_hint` /
  `elevenlabs_key_hint` ("ends in 1a2b") from `dashboard.page_config()`; new
  keys are checked with their service (`check_groq_key`,
  `elevenlabs_stream.check_key`) before they are saved. `cloud_provider`
  ("groq" | "elevenlabs") picks the main service; Groq's speculative
  pipeline runs only when Groq is the main service.
- **`cloud_usage.jsonl`** holds Groq calls (kept 7 days, for the daily quota)
  and ElevenLabs streams tagged `provider: "elevenlabs"` (kept 40 days, for
  the Settings usage meter). Groq's quota math must skip the ElevenLabs entries.
- **Never write a data file directly** — go through `storage` (atomic,
  validated, recoverable). Never add a path under the code folder for user
  data.
- **Errors are never silent:** log with `storage.log_error()` (or
  `_write_error_log`) and tell the user with `notify(key, title, message,
  cooldown)` — rate-limited per key, queued until the tray exists, safe from
  any thread. Copy follows `docs/notifications.md`: short title, message
  says what Scribe did or what the user can do.
  `sys.excepthook` / `threading.excepthook` / Tk `report_callback_exception`
  catch the rest. Cloud failures go through `_handle_cloud_error()`
  (401 → stop using that key, 429 → pause for retry-after, network → pause
  30 s); ElevenLabs failures through `_handle_eleven_error()` (auth → stop
  using that key, quota/terms → pause 1 h, busy → 60 s, network/timeout →
  30 s, server → none); `transcribe_audio()` raises `TranscriptionFailed`
  rather than returning "" when nothing can transcribe.
- **Custom vocabulary:** `vocabulary.json` (optional, personal, gitignored —
  `vocabulary.example.json` is the committed template) holds `terms` (fed to
  Whisper's prompt via `build_transcribe_prompt()`) and `corrections` (the
  find-and-replace map for `apply_vocabulary()`). Loaded at startup by
  `load_vocabulary()` and again by `reload_config()` when the dashboard
  saves, so Dictionary-page edits apply on the next dictation.
- **Undo:** `_deliver_job()` saves the delivered text in `last_output` with
  the window it went to (`last_output_hwnd`), whether it was logged
  (`last_output_logged`) and its app (`last_output_app`), all under
  `undo_lock`. The `Ctrl+Win+Z` hotkey (detected in `_handle_press`) and the
  "scratch that" voice command run `undo_last()` on a worker thread: it waits
  for the chord's modifiers to come up, restores AND verifies that window
  under `deliver_lock` (if it's gone, nothing is deleted and the user is
  told), backspaces that many characters, and drops the history entry only
  if one was written.
- **Deliveries** go through `_deliver_job()` (turn, modifiers, verified focus,
  clipboard fallback, history, undo state). Pastes borrow the clipboard via
  `_clipboard_set_text(..., private=True)` — marked so it never enters Win+V
  history or cloud sync — and restore the user's clipboard (also marked
  private) after `CLIPBOARD_RESTORE_DELAY`; a busy clipboard means typing.
- **Tests drive the hotkey** through `_handle_press` / `_handle_release` /
  `_handle_check` (synchronous) with `_key_is_down` stubbed — never through
  `on_press`, which only queues.
- **Microphone & first run:** `MIC_DEVICE` (config) names the input device;
  `open_mic()` resolves it via `devices.resolve_device()` and falls back to
  the default, or to no stream at all (`stream is None` — Scribe still runs
  and says "No microphone found"). `start_recording()` returns False when no
  mic can start, and the start cue plays only after `stream.start()`.
  `FIRST_RUN` (no config.json) sets `setup_pending`: nothing is saved or
  downloaded, the model manager waits, and main() opens the welcome. Its
  "Finish setup" saves the choices and sends `setup-done` →
  `complete_setup()`. If the welcome can't open (no WebView2, exit code 2)
  `_check_dashboard_started()` → `finish_setup_with_defaults()`.
- **State lives in module-level globals** (`recording`, `frames`, `pressed`,
  the widget references, etc.), declared near the top under SHARED STATE.
- **Single-instance guard:** `instance.acquire()` (a named mutex, per user
  and data folder) runs at the very top of app.py — before the heavy
  imports and before any file is touched; a second copy exits immediately.
- **`dictation_log.jsonl`** is JSON Lines — one JSON object per dictation,
  append-only via `storage.append_jsonl`. The dashboard reads it via
  `read_log()`, which skips malformed lines and coerces types.
- **Windows-specifics:** focus capture/restore uses `ctypes` + `user32`;
  `SetCurrentProcessExplicitAppUserModelID` gives the app its own taskbar
  identity. These are wrapped in `try`/`except` so the code degrades quietly
  off-Windows.

## Style

The codebase is written to be read by a learner: heavy explanatory comments,
section banners, plain language. Match that density and tone when editing —
explain *why*, not just *what*. Build features one small, testable piece at a
time. Future feature ideas and their priority are in `ROADMAP.md`.
