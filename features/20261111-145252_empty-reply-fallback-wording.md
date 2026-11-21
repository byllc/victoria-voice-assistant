# Empty-reply fallback wording

- Status: ready for review
- Phase: current
- Origin: Stage 14 (split from router model replacement, 2026-11-11)
- Created: 2026-11-11 (UTC)
- Started: 2026-11-12, branch `feature-upgrade-feature-tool` (Bill: "ask all the qeustions you need right now and then get to work")

## Done when

- [x] The fallback spoken when the model returns nothing ("Sorry, I didn't get that. Could you say it another way?", `chat/turns.py`) is reworded so it can't be taken for a hearing problem.

## Blocking decisions

None open. The wording: "Sorry, I came up empty on that one. Try asking another way." (Bill, 2026-11-12, chose "I came up empty".)

## Evidence and notes

Built 2026-11-12: `EMPTY_REPLY` in `chat/turns.py` is now "Sorry, I came up empty on that one. Try asking another way." The tests use the constant, so they follow it. Not seen live: it needs the model to return nothing.
