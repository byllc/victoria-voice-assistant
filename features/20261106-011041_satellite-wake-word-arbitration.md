# Satellite wake-word arbitration

- Status: open
- Phase: staging
- Origin: Stage 15 (from Stage 13)
- Created: 2026-11-06 (UTC)
- Started: 2026-11-11, branch `feature-satellite-wake-word-arbitration`

## Done when

Bill, 2026-11-11: the position testing moves to staging, with the satellites in their final positions; he will work on it over time. The scoring changes below are built; the two open criteria wait for staging.

- [ ] Words heard by several satellites are answered once, by the one nearest the speaker, in at least 9 of 10 tries from mixed positions. Each decision and its scores are logged.
- [ ] Tune `arbitration_offset_db` for the two boards' different microphones (the office answered when Bill stood beside the Waveshare, so it is not tuned yet).
- [x] Settle the arbitration window (`satellite_arbitration_s`, currently 0.5 s). Kept at 0.5 s (decision 1).

## Decisions (Bill, 2026-11-11: "lets go with your advice")

1. **Arbitration window:** stays at 0.5 s.
2. **Offset approach:** per device, with each satellite's own `arbitration_offset_db` in `config/tools.yaml` (the setting already exists).

3. **Score by loudness** (2026-11-11, "Yes, build both"): the satellite that hears the speech loudest answers (the 90th percentile of its 30 ms frame levels, plus its offset), instead of speech against its own quiet frames. Architecture §4 updated (v2.12).
4. **Equal audio** (same answer): each satellite is scored on its own first 0.5 s after it woke, and the decision comes 0.5 s after the last one woke. The window for joining stays 0.5 s. Accepted cost: when a second satellite wakes late, the turn starts up to about 0.45 s later.

## Evidence and notes

Two live turns so far: the nearer satellite won once (9.3 dB vs 5.6 dB) and lost once (9.8 dB vs 16.4 dB). Code: `satellite/arbiter.py`, `tests/test_arbiter.py`.

2026-11-11:

- The service log holds nine two-satellite decisions, all from 2026-11-05: the Waveshare answered five, the office four. Where Bill stood is known for only the two above, so they don't show how to set the offsets.
- The decision log (`satellite_arbitration`) now says what each score was made of: when each satellite woke relative to the first, its loud and quiet levels (dB below full scale), how much audio was scored, and its offset. Example: `kitchen 20.0 dB (answers) [woke +0.00 s; loud -24.0, quiet -55.1 dBFS, 0.48 s, offset +2.0]`. Test: `test_the_decision_log_says_what_each_score_was_made_of`. `uv run pytest -q`: 374 passed, 3 skipped.
- Not yet running: the installed service still has the old code until it is restarted.
- Blocked on hardware: the office Satellite1 is off the network (no reply to ping at 15:02 UTC, and not connected since the service started on 2026-11-09). The Waveshare is connected.

- Service restarted with Bill's go-ahead (15:04 UTC); the office Satellite1 connected at 15:05:44 once powered.

Calibration tries (2026-11-11, UTC):

| # | Time | Where Bill stood | Said | Waveshare | Office | Answered | Right? |
|---|---|---|---|---|---|---|---|
| 1 | 15:05:58 | 12 inches from the Satellite1 | wake word only | 1.8 dB (woke first; loud -70.9, quiet -72.8 dBFS) | 18.0 dB (woke +0.28 s; loud -67.7, quiet -85.6 dBFS) | office | yes |
| 2 | 15:08:13 | right beside the Waveshare | wake word only | 10.1 dB (woke first; loud -63.0, quiet -73.0; 0.51 s scored) | 7.4 dB (woke +0.33 s; loud -55.3, quiet -62.7; 0.18 s scored) | waveshare | yes |
| 3 | 15:08:43 | right beside the Waveshare | "what time is it" | 12.5 dB (woke first; loud -51.1, quiet -63.7; 0.54 s) | 8.7 dB (woke +0.44 s; loud -65.2, quiet -73.8; 0.09 s) | waveshare ("It's 10:08 AM.") | yes |
| 4 | 15:09:03 | right between them | "what time is it" | 13.3 dB (woke first; loud -45.9, quiet -59.2; 0.54 s) | 10.4 dB (woke +0.32 s; loud -59.9, quiet -70.3; 0.21 s) | waveshare ("It's 10:08 AM.") | no clear nearest; answered |
| 5 | 15:09:25 | across the room | a command | 11.1 dB (woke first; loud -61.8, quiet -72.9; 0.48 s) | 17.7 dB (woke +0.35 s; loud -64.8, quiet -82.5; 0.15 s) | office, then `satellite_no_speech` after 8 s (peak 95) | **failed: nobody answered** |

Try 5 is the failure that matters: the office was chosen on 0.15 s of audio and its low noise floor, then could not make out the speech from across the room (raw peak 95), so the Waveshare had been stood down and nothing answered (Bill: "they both woke but neither responded"). The Waveshare's loud level was 3 dB higher than the office's on that try, so comparing loudness (the first proposal above) would have picked the Waveshare.

Findings from tries 2 to 5:

- The office wakes 0.3 to 0.45 s after the Waveshare every time (five of five). The decision is made 0.5 s after the first wake, so the office is scored on only 0.09 to 0.21 s of audio (3 to 7 frames) against the Waveshare's half second, and on a different part of the sentence. Its score is close to chance.
- The margins beside the Waveshare are small (2.7 to 3.8 dB), against a 1.5 dB tie band, and ties go to the first to wake, which is always the Waveshare.
- Try 2: Bill reported "both responded". The log shows one answer: the office woke, was told to stand down within 0.3 s, and only the Waveshare went on. Both boards show and sound their own wake before the assistant can choose, so both visibly wake. To confirm with Bill.
- The boards are about 4 feet apart (Bill), so most positions are nearly equidistant: fine for setting offsets, not for the 9-of-10 check, which needs realistic placement.

**Proposal 2 (built, decision 4):** score every satellite on the same amount of audio. Keep the 0.5 s window for joining, but decide once the last satellite to wake has its own half second, so at most about 0.45 s later than now when a second satellite wakes late. The audio is held meanwhile and the speaker is usually still talking, but a one-word command ("stop") heard by two satellites would start up to that much later.

Notes from try 1: with the wake word alone, the scored half second holds almost no speech on either board (both loud levels are near -70 dBFS), so the result came from the office's much lower noise floor (-85.6 against -72.8 dBFS), not from Bill's voice. That floor difference favours the office wherever Bill stands. Later tries need a command after the wake word. The office then logged `satellite_no_speech` after 8 s, as expected with no command. Nine seconds earlier the Waveshare alone woke on "Victoria" (the office had connected 5 s before) and answered "Hello. How can I help you?".

Built after try 5 (decisions 3 and 4): `satellite/arbiter.py` (`speech_level_db`; each join moves the decision to one window after it), `satellite/esphome.py` (`_scored_audio`: the turn's first window only). Tests: a quiet noise floor doesn't beat louder speech, a late waker gets its full window before the decision, a satellite is scored on its first window only. `uv run pytest -q`: 377 passed, 3 skipped. `docs/satellites.md` and the architecture (§4) say the new rule. Service restarted to load it. By the loud levels logged in tries 1 to 5 the new rule would have chosen office, office, Waveshare, Waveshare, Waveshare: try 2 (wake word only, beside the Waveshare) would have gone to the office on its 0.18 s sliver, which the equal-audio change is meant to cure; to be re-run.

With the new rule (2026-11-11, UTC; scores are loud levels in dB below full scale, each on 0.48 s):

| # | Time | Where Bill stood | Waveshare | Office | Answered | Right? |
|---|---|---|---|---|---|---|
| 6 | 15:15:22 | 12 inches from the Satellite1 | -52.3 (woke first) | -61.2 (woke +0.29 s) | waveshare ("It's 10:15 AM.") | **no** |

Try 6: equal audio now (0.48 s each), and the office's microphone reads about 9 dB quieter than the Waveshare's even with Bill a foot from it and four feet from the Waveshare. Allowing roughly 12 dB for the distance, the office reads something like 15 to 20 dB low. This is what `arbitration_offset_db` is for. Provisional `arbitration_offset_db: 14` set on the office in Bill's `config/tools.yaml` (reloaded live at 15:16:48, revision 2, no restart); to be settled from equidistant tries.

Also seen: the office has not answered a command today. Its three turns ended in `satellite_no_speech` with raw peaks of 84 to 566 and a volume of 0.60 to 0.86 after `gain` 24, against speech detection's 0.6 gate, so it is at the edge of hearing. Its `gain` may need raising, separately from arbitration. To check with a command spoken a foot from it once it wins.

Plan for the live tries, once both satellites are up: ten wake-ups from positions Bill names (beside each board, between them, across the room), Bill saying where he stood for each. Offsets are set from the first few, then the 9-of-10 run.

Paused 2026-11-11 (Bill): moved to the staging phase. State left behind:

- Built and running on this Mac: the fuller decision log, scoring by loudness, and equal audio (decisions 3 and 4). `uv run pytest -q`: 377 passed, 3 skipped. On branch `feature-satellite-wake-word-arbitration`, not committed.
- The provisional `arbitration_offset_db: 14` on the office satellite was never tried, so it was taken out of Bill's `config/tools.yaml` again; both satellites have no offset.
- For staging: set each satellite's offset from tries at equal distance, with the satellites in their final rooms, then the 9-of-10 run. The desk tests here had the two boards about 4 feet apart, where most positions have no clear nearest.
- The office Satellite1 not hearing commands is not a position problem; it is tracked in `20261106-011048_satellite-voice-checks.md`.
