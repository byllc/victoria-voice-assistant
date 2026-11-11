# Satellite wake-word acceptance

- Status: open
- Phase: current
- Origin: Stage 14
- Created: 2026-11-06 (UTC)

## Done when

- [ ] The satellite's own wake-word check stops rejecting "Victoria" (log: "model predicts 'victoria', but VAD model doesn't").

- [ ] The wake word stops waking the satellite when nobody said it (Bill, 2026-11-11: "the wake word is way too sensitive, it wakes up all the time"; to be done before staging). False wakes are counted over a set time, before and after.

## Blocking decisions

None.

## Evidence and notes

Seen on the Waveshare test board and the Satellite1.

2026-11-11: the service log shows each wake as `satellite_turn` with the wake word heard, and a false wake usually ends in `satellite_no_speech`, so they can be counted from the log. The two wake words have their own thresholds in the firmware (`services/satellite1/wake_words/`: victoria 0.80, hey_viki 0.85). Not started.

2026-11-11, Bill: it wakes to speech. Retraining the wake word moves to a later phase; technical fixes are done here.

Proposed technical fixes (firmware settings, uploaded over Wi-Fi; waiting for Bill's go-ahead on the values):

1. "Victoria": `probability_cutoff` 0.80 -> 0.90 and `sliding_window_size` 5 -> 8 in `services/satellite1/wake_words/victoria.json`, so the model must be surer, for longer, before it wakes.
2. "Hey Viki": switched off unless Bill uses it. Two wake words are two chances to wake by mistake. In the log the office woke 63 times on "victoria" and 19 on "hey_viki".
3. Count before and after from the log: wakes per hour with speech in the room and nobody saying the wake word.

Cost: stricter settings also reject more real "Victoria"s, and the satellite's own check already rejects some (the first criterion above). If both can't be met by settings, that is the case for retraining.

Done 2026-11-11 with Bill's go-ahead ("Yes, flash it"):

- `services/satellite1/wake_words/victoria.json`: `probability_cutoff` 0.90 (was 0.80), `sliding_window_size` 8 (was 5). `services/satellite1/satellite1.yaml`: "Hey Viki" switched off at startup (still in the firmware).
- `assistant satellites update --only office`: built, uploaded over Wi-Fi ("OTA successful", 3.4 MB), and the satellite reports the new build (built 2026-11-11 10:59:37 -0500, ESPHome 2026.7.3). It was off the assistant for 22 s (16:00:40 to 16:01:02 UTC) and reconnected by itself.
- The Waveshare shares `victoria.json`, so it gets the stricter setting at its next build; it is unplugged and was not flashed. Its "Hey Viki" is still on.
- `docs/satellites.md` and the Satellite1 README say the new settings, and that a short pause follows the wake word.
- Not yet measured: false wakes with speech in the room, and whether a real "Victoria" still wakes it reliably. Retraining the model is the fallback, in a later phase, if settings can't give both.
- 2026-11-11, Bill after the update: "it woke to victoria". He is leaving it running to see whether it still wakes to ordinary speech; false wakes to be counted from the log next session (wakes since 16:01 UTC that end in `satellite_no_speech`).
