# How Scribe talks to you

Scribe never fails silently: when something goes wrong it writes the details to
`error_log.txt` (`storage.log_error()`) and shows a short Windows notification with
`notify(key, title, message, cooldown)` — rate-limited per `key`, so one problem is
one notice, not one per dictation.

## The rules for the words

- **Title:** a few words saying what happened. No jargon, no error codes.
- **Message:** what Scribe did about it, or the one thing you can do. One or two
  sentences.
- Say where to go ("Settings", "tray → Open data folder"), never how the code works.
- A fallback is good news: say what Scribe used instead ("Scribe used the local model").
- Never blame the user, never show a stack trace, never ask them to run a command —
  `setup.bat` is the repair tool.

## Examples

| Key | Title | Message |
|---|---|---|
| `no_mic` | No microphone found | Plug one in — Scribe will pick it up automatically. |
| `mic_fallback` | Microphone unavailable | "{name}" isn't connected, so Scribe is using your default mic. |
| `mic_switched` | Microphone changed | Now listening with {name}. |
| `key_rejected` | Groq API key rejected | Check the key in Settings. {what Scribe uses instead} |
| `rate_limited` | Cloud limit reached | Groq asked Scribe to wait {time}. {what Scribe uses until then} |
| `cloud_down` | Cloud transcription unavailable | Couldn't reach Groq, so Scribe used the local model. |
| `cloud_broken` | Cloud transcription unavailable | Scribe's cloud component isn't installed correctly. Run setup.bat again to repair it. |
| `polish_down` | AI polish unavailable | Couldn't reach Groq, so your words were typed as spoken. |
| `polish_limited` | AI polish paused | Groq's free limit for polishing was reached. Scribe types your words as spoken for {time}. |
| `transcribe_failed` | Couldn't transcribe that | {reason} Nothing was typed. |
| `undo_failed` | Couldn't undo | The window you dictated into isn't available, so nothing was deleted. |
| `recording_limit` | Recording limit reached | Scribe transcribed the first 5 minutes. Release the hotkey and start a new dictation to keep going. |
| `history_failed` | Couldn't update your history | Your text was handled, but Scribe couldn't update its history file. Details are in the error log. |
| `settings_recovered` | Settings restored | Your settings file couldn't be read, so Scribe restored the last working copy. The damaged file was kept as {file}. |
| `update` | Scribe {version} is available | Open Scribe's Settings → About and click Update now. It takes about a minute. |
| `background_error` | Something went wrong | Scribe kept running. Details are in the error log (tray → Open data folder). |
