# Scribe — Feature Roadmap

Ideas for future versions, to close the gap with Wispr Flow.
Each item has a rough **difficulty** (Easy / Medium / Hard) and *why it
matters*. Build them the way we built the app: one small, testable piece at
a time.

> **The honest trade-off:** Wispr Flow runs on cloud GPUs, so it's fast.
> Scribe runs locally for free and offline, so on a CPU it's slower. The two
> real paths to matching that speed are **GPU acceleration** and **streaming
> transcription** (both below). Everything else is about *quality* and
> *features*, where being local costs us nothing.

---

## 1. Speed

- **Paste mode for long text** — ✅ *Done.* Copies the text to the clipboard
  and sends one Ctrl+V instead of simulating every keystroke — near-instant
  for long dictations. Saves and restores the clipboard so nothing is lost.
  Toggle it in the dashboard's Settings tab.
- **Distilled models** — *Easy.* Models like `distil-small.en` give nearly
  the accuracy of the full model at higher speed. Mostly a model-name change.
- **GPU acceleration (CUDA)** — *Easy code / needs hardware.* On a PC with an
  NVIDIA GPU, faster-whisper can run with `device="cuda"` — several times
  faster. Needs the GPU plus CUDA libraries installed.
- **Streaming transcription** — ✅ *Done (cloud, ElevenLabs).* With
  ElevenLabs Scribe v2 Realtime as the cloud service, audio streams while you
  hold the hotkey and the text lands ~0.2 s after release (Groq and the local
  model are the fallbacks). Streaming the *local* model is still open.

## 2. Accuracy & punctuation

- **Auto-capitalize / cleanup pass** — ✅ *Done.* `clean_text()` runs after
  transcription: collapses whitespace, removes spaces before punctuation,
  capitalizes sentence starts and standalone "i". Deterministic and safe —
  no proper-noun guessing.
- **Custom vocabulary** — ✅ *Done.* Two layers: terms from `vocabulary.json`
  are appended to Whisper's `initial_prompt` (prevention), and a
  `corrections` find-and-replace pass (`apply_vocabulary()`) fixes stubborn
  mishearings and casing (correction). Edit `vocabulary.json` and restart.
- **VAD tuning** — *Medium.* Adjust the silence detector's sensitivity for
  your mic and room.

## 3. Smart text processing (the "AI" part of Wispr Flow)

- **Filler-word removal** — ✅ *Done.* `remove_fillers()` strips non-word
  filler sounds (um, uh, er, hmm...) as whole words, before the cleanup
  pass. Conservative by design — only true non-words, configurable via the
  `FILLER_WORDS` list.
- **AI polish** — ✅ *Done (cloud).* Groq's `qwen3.8-27b` cleans each
  dictation up like Wispr Flow (grammar, punctuation, false starts, rambling)
  in ~0.2 s, with a guard that falls back to your own words (it refuses an
  answer that invents a word you never said); "fix that" re-polishes on
  demand. Light or Full style in Settings. A *local* model for this is still
  open.
- **Tone / format modes** — *Hard.* Rewrite the same dictation as a casual
  message vs. a formal email. Builds on the LLM cleanup above.

## 4. Features & UX

- **Multi-language support** — *Easy–Medium.* Drop the English-only lock and
  let Whisper auto-detect the spoken language.
- **History search & export** — *Easy.* Search past dictations in the
  dashboard; export the log to a text or CSV file.
- **Undo last dictation** — ✅ *Done.* Ctrl+Win+Z sends one Backspace per
  delivered character and drops the entry from the log. Best-effort —
  deletes backward from the current cursor.
- **Voice commands** — ✅ *Done (line breaks).* Spoken "new line" / "new
  paragraph" become real line breaks — the one thing Whisper can't produce
  itself. Two paths: `whole_utterance_command()` (reliable — say the command
  on its own) and inline `apply_voice_commands()` (best-effort mid-sentence).
  Scoped to line breaks on purpose; extend via the `VOICE_COMMANDS` dict.
- **First-run setup wizard** — ✅ *Done.* `run_setup_wizard()` shows once
  (when there's no config.json): a single window for microphone, hotkey, and
  model, run before the model loads so the choices apply on the first launch.
  Also added microphone selection as a real setting (config + Settings tab).

---

## 5. Distribution

- **Download, extract, double-click** — ✅ *Done (1.0).* `Scribe.zip` from
  the latest release + `setup.bat` (fetches its own Python when needed),
  a welcome that connects a voice model in three steps, a daily update check
  with "Update now", and `uninstall.bat`.
- **A real installer (.exe)** — *Medium.* PyInstaller + Inno Setup: no
  console window during setup, an "Apps & features" entry. Worth it once
  more people than friends use Scribe.
- **Code signing** — *Easy code / costs money.* Without it Windows shows
  "Windows protected your PC" on first run; a signed build avoids that (and
  antivirus false alarms on a keyboard-hook app).
- **winget / Scoop package** — *Easy.* `winget install Scribe`, once there
  is an installer.

## Suggested order

A reasonable next batch, by value-for-effort:
1. ~~**Paste mode** — instant speed win for long dictations.~~ ✅ Done.
2. ~~**Auto-capitalize cleanup** — quick, visible quality bump.~~ ✅ Done.
3. ~~**Custom vocabulary** — fixes the specific words *you* dictate often.~~ ✅ Done.
4. ~~**Filler-word removal** — makes output read cleaner with little effort.~~ ✅ Done.

Bigger projects for later: **streaming transcription** and **local LLM
cleanup** — save these for when the quick wins are done.
