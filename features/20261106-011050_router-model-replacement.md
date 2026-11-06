# Router model replacement

- Status: open
- Origin: Stage 14
- Created: 2026-11-06 (UTC)

## Done when

- [ ] The router (gemma-4-12b on the M2) fails tool calls: about 4 in 10 "set a timer" requests come back empty even with the retry, and most replies are padded with blank lines to the 200-token limit (13–16 s).
- [ ] Try a replacement for the router and Hermes under the one-copy rule (qwen3-coder-30b is already downloaded).
- [ ] Reword the empty-reply fallback.

## Blocking decisions

- Which router model to try.
- Whether the M2 or the Mac mini is the place to judge it.

## Evidence and notes

Finding recorded 2026-10-30 (reasoning tokens stream before content; `reasoning_effort: low` ignored, `none` honoured).
