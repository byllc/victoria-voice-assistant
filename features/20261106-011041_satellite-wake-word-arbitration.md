# Satellite wake-word arbitration

- Status: open
- Origin: Stage 15 (from Stage 13)
- Created: 2026-11-06 (UTC)

## Done when

- [ ] Words heard by several satellites are answered once, by the one nearest the speaker, in at least 9 of 10 tries from mixed positions. Each decision and its scores are logged.
- [ ] Tune `arbitration_offset_db` for the two boards' different microphones (the office answered when Bill stood beside the Waveshare, so it is not tuned yet).
- [ ] Settle the arbitration window (`satellite_arbitration_s`, currently 0.5 s).

## Blocking decisions

- Arbitration window value.
- Arbitration offset approach (per-board or per-device).

## Evidence and notes

Two live turns so far: the nearer satellite won once (9.3 dB vs 5.6 dB) and lost once (9.8 dB vs 16.4 dB). Code: `satellite/arbiter.py`, `tests/test_arbiter.py`.
