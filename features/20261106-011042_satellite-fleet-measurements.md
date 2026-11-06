# Fleet load measurements

- Status: open
- Origin: Stage 15 (from Stage 13)
- Created: 2026-11-06 (UTC)

## Done when

- [ ] With every satellite connected, measure memory, event-loop lag, and queue waits for simultaneous turns on the Mac mini.
- [ ] A turn in one room is unaffected by the idle satellites.
- [ ] Results accepted by Bill.

## Blocking decisions

- Measurements must run on the Mac mini, not the M2.

## Evidence and notes

Shared turn-detection models already cut per-satellite memory by about 19 MB (measured on the M2).
