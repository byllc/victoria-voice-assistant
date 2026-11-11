# Router model replacement

- Status: open
- Phase: staging
- Origin: Stage 14
- Created: 2026-11-06 (UTC)

## Done when

- [ ] On the Mac mini, a router model is chosen from candidates the dev machine has no memory for (architecture §6: around 30B, mixture of experts, 4-bit MLX), scored with `assistant tools test` on correct outcome and time to first token.
- [ ] The chosen model serves the router and Hermes under the one-copy rule (qwen3-coder-30b is already downloaded).
- [ ] The failures seen with gemma-4-12b on the M2 are re-checked with the chosen model: about 4 in 10 "set a timer" requests came back empty even with the retry, and most replies were padded with blank lines to the 200-token limit (13–16 s).

## Blocking decisions

- Which router models to try.

## Evidence and notes

Finding recorded 2026-10-30 (reasoning tokens stream before content; `reasoning_effort: low` ignored, `none` honoured).

Bill, 2026-11-11: the dev machine's model setup is sized for its small memory, so the model is judged on the Mac mini, in staging. This answers the earlier blocking decision (M2 or Mac mini). Rewording the empty-reply fallback is not performance work and moved to `20261111-145252_empty-reply-fallback-wording.md`.
