# End-to-end voice and text benchmark

- Status: open
- Phase: staging
- Origin: Stage 14 (moved from Stage 1 deferral)
- Created: 2026-11-06 (UTC)

## Done when

- [ ] Benchmark voice and text interactions on the Mac mini, with overall success and per-stage latency, against the 1.5 s target (architecture §5).
- [ ] The latency report for 20 spoken commands, broken down by stage (`assistant voice bench --log <service log>`; carried from Stage 5).
- [ ] Short commands ("stop", "pause") end their turn promptly, and Smart Turn doesn't cut a sentence off mid-utterance (carried from Stages 1 and 5).
- [ ] Two voice sessions at once without audio stutter (`assistant voice bench --sessions 2`; carried from Stage 5).
- [ ] Web search timings for a snippet answer and a page-read answer, compared with the architecture's estimates of 1–2 s and 2–4 s (carried from Stage 6).
- [ ] A new browser's wait to be listed by Music Assistant (about 16 s on the M2; carried from Stage 10).
- [ ] Results accepted by Bill.

## Blocking decisions

- Latency target is 1.5 s to first reply (architecture §5).

## Evidence and notes

Not started. The harness is built (`voice/bench.py`).

Bill, 2026-11-11: all performance testing happens in staging, on the Mac mini. The carried-over timing checks from Stages 5, 6 and 10 are listed here so they are tracked in one place.
