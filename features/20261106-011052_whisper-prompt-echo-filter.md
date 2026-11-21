# Drop Whisper prompt echoes

- Status: ready for review
- Phase: current
- Origin: Stage 14
- Created: 2026-11-06 (UTC)
- Started: 2026-11-12, branch `feature-upgrade-feature-tool` (Bill: "ask all the qeustions you need right now and then get to work")

## Done when

- [x] On near-silence Whisper can return its own prompt repeated ("Ask Claude, ask Claude, …"). Transcripts that are only the prompt are dropped, on every device.

## Blocking decisions

None.

## Evidence and notes

Seen 2026-11-05 on the Waveshare at too low a gain.

Built 2026-11-12:

- `is_prompt_echo` (`voice/models.py`): a transcript is dropped when it is nothing but the phrases of Whisper's prompt ("Victoria", "Ask Hermes", "Ask Claude") with one of them repeated, or three or more of them. It is logged as `stt_prompt_echo_dropped`.
- One phrase alone ("Ask Claude.") or "Victoria, ask Claude." is kept: someone may have said it. Anything with other words in it is kept.
- The check is in the speech-to-text step every device shares (browser voice and satellites both build it with `SpeechModels.stt`), so it applies on every device.
- Tests: `test_prompt_echo_is_recognised`, and `test_whisper_gets_the_prompt` now also checks that an echoed transcript produces no transcription (`tests/test_voice.py`).

Not seen live: it needs Whisper to echo its prompt on near-silence, which can't be made to happen on demand. After a day of use, `stt_prompt_echo_dropped` in the log shows whether it fires.
