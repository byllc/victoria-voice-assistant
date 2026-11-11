# Satellite voice checks

- Status: in progress
- Phase: current
- Origin: Stage 14
- Created: 2026-11-06 (UTC)

## Done when

- [ ] By voice on a satellite: search, agents, home control, and music work. Time, weather, and timers already work.
- [ ] Answering a clarifying question without the wake word works (the mic reopens; untested).

- [x] The office Satellite1 hears and answers a command at 1 foot, 4 feet, 8 feet and across the room, tested on its own. With `gain` 120 and a short pause after the wake word (2026-11-11).

## Blocking decisions

None.

## Evidence and notes

Not started.

2026-11-11 (started at Bill's request: "work on the satellite1 directly to make sure it's doing its job"):

- Earlier today I noted that the office Satellite1 "didn't hear a single command". That overstated it. Of its three `satellite_no_speech` turns, one was the wake word alone (15:06), one was from across the room (15:09, raw peak 95), and one was a lone "Hey Viki" (15:14, raw peak 84). The only command spoken near it (15:15, 12 inches) was answered by the Waveshare, so the office was never asked to transcribe it.
- Its history is normal for it: 41 turns between 2026-11-02 and 2026-11-05 ended with speech detected, with raw peaks mostly 400 to 1,700 (volume 0.8 to 0.9 after `gain` 24). Turns with raw peaks near 100 (volume 0.60 to 0.69) sit at speech detection's 0.6 gate: some were heard, some not.
- So the open question is range, not whether it works: how far away a command still clears the gate.
- To test it alone, the Waveshare has a temporary `arbitration_offset_db: -40` in Bill's `config/tools.yaml`, so it always stands down. To be removed after the tries.

Satellite1 on its own, 2026-11-11 (UTC):

| Time | Where | With the Waveshare? | Result | Levels |
|---|---|---|---|---|
| 15:19:49 | 1 foot | yes: both woke, office chosen (Waveshare held down by the temporary offset) | `satellite_no_speech` after 8 s: no answer | raw peak 1,142, volume 0.85 |
| 15:24:22 | 1 foot | no: the Waveshare was off the network (unplugged by Bill) | "What time is it?" -> "It's 10:24 AM." | raw peak 84, volume 0.69 |

- New setting `satellites.<name>.record_dir` (off unless set): saves a turn's microphone audio, after `gain`, as a WAV file for fault-finding. Test: `test_a_turn_is_recorded_only_when_a_folder_is_set`. `uv run pytest -q`: 378 passed, 3 skipped. Set for the office in Bill's `config/tools.yaml`, pointing at a temporary folder; to be removed with the recordings.
- The 15:24 recording (2.0 s): speech from the very start for about 1.0 s at -37 to -41 dBFS after `gain` 24, then a floor of -65 dBFS. So the Satellite1 streams from the start of the command (it does not lose the first words to its wake sound), but its speech is quiet even after the gain: about 15 dB under a normal speech level, and close to speech detection's gate.
- Not explained yet: the 15:19 turn, louder and still not detected. Correction: the 15:24 turn was held for arbitration too (the arbiter counts configured satellites, and two are configured), and it was heard, so the half-second hold is not the cause by itself. The only difference left is that a second satellite woke as well. To reproduce with both connected and recording on, when the Waveshare is back; Bill unplugged it to focus on the Satellite1 (the temporary Waveshare offset is removed).

Range, Satellite1 alone, `gain` 24 (2026-11-11, UTC; Bill's reports, with each recording's level after gain):

| Time | Distance | Answered? | Raw peak | Volume (gate 0.6) | Speech level |
|---|---|---|---|---|---|
| 15:24:22 | 1 foot | yes | 84 | 0.69 | -37 to -41 dBFS |
| 15:27:47 | 4 feet | yes | 42 | 0.64 | -43 to -50 dBFS |
| 15:28:02 | 8 feet | no (`satellite_no_speech`) | 31 | 0.52 | -46 to -57 dBFS |
| 15:28:18 | 20 feet | no (`satellite_no_speech`) | 123 | 0.60 | -46 to -56 dBFS |

- Cause: the loudness gate, not the microphone or Whisper. Replayed offline, Whisper read "What time is it?" from all four recordings as they were. Replayed at five times the gain (120), all four clear the gate (volume 0.83, 0.78, 0.73, 0.70) with nothing near full scale.
- Fix: the Satellite1's default `gain` is now 120 (limit raised from 64 to 512), and `raise_level` eases loud audio in under full scale instead of cutting it off flat, so a close or loud voice at this gain is compressed, not clipped. The Waveshare keeps its own `gain: 24`. Test: `test_quiet_microphone_audio_is_raised_without_wrapping`. `uv run pytest -q`: 378 passed, 3 skipped. Service restarted to load it. To be confirmed by voice at 1, 8 and 20 feet.
- Found in passing (tool calling, not the satellite): at 15:27 the reply to "What time is it?" was "It's 10:24 AM.", three minutes stale. The model repeated its earlier answer from the conversation instead of calling `clock` (no `tool_call` in the log for that turn).

With `gain` 120 (2026-11-11, UTC):

| Time | Distance | Answered? | Recording |
|---|---|---|---|
| 15:33:11 | 1 foot, command straight after the wake word | no (`satellite_no_speech`, volume 0.70) | 0.4 s of speech at -25 to -32 dBFS, then a floor of -48 dBFS; Whisper reads "That's it." |

- This one is a different fault from the range: the level was fine, but the recording holds only the end of the sentence ("...is it"). The Satellite1 starts streaming when its wake word is detected, with nothing kept from before, and it detects about 0.2 to 0.45 s later than the Waveshare did (the arbitration log earlier today). A command spoken straight after "Victoria" is partly over by then, and 0.4 s of speech isn't enough for speech detection. The unexplained 15:19 turn (loud, not detected) fits the same cause.
- I had asked Bill to speak with no pause, for the arbitration tries. A short pause after the wake word (until the ring lights or it chimes) is how this firmware is meant to be used. To be re-tried that way at 1, 8 and 20 feet.
- Related: `20261106-011047_satellite-wake-word-acceptance.md` (the satellite's own check often rejects "Victoria"), which may be part of why it wakes late.

Confirmed by voice, `gain` 120, a short pause after "Victoria" (2026-11-11, UTC; Bill: "it worked with a small pause from every position"):

| Time | Heard | Volume | Reply |
|---|---|---|---|
| 15:35:24 | "What time is it?" | 0.85 | "It's 10:35 AM." (`clock` called; the model took 26 s) |
| 15:36:06 | "What time is it?" | 0.82 | "It's 10:35 AM." (no `clock` call: the earlier answer repeated, a minute stale) |
| 15:36:19 | "What time is it?" | 0.74 | "It's 10:35 AM." (the same) |
| 15:37:34 | "Can you tell me a story?" | 0.74 | two empty model replies, then "Sorry, I didn't get that. Could you say it another way?" |

- The satellite side is doing its job: every turn was heard and transcribed correctly, from 1, 8 and 20 feet. The faults left in these turns are the model's (a stale time repeated from the conversation, empty replies), not the satellite's.
- Cleaned up: `record_dir` removed from Bill's `config/tools.yaml` and the test recordings deleted. The setting stays available for fault-finding.
- Still open here: search, agents, home control and music by voice; answering a clarifying question without the wake word. No-pause commands lose their first words (see above); not fixed.
