# Claude agent live check

- Status: in progress
- Phase: current
- Origin: Stage 7 (carried over)
- Created: 2026-11-11 (UTC)

## Done when

- [ ] A real "Ask Claude …" call works by voice and text: the decider's choice, the reply, and the tokens and cost in the spend log.
- [ ] "Ask Claude Opus …" overrides the decider on a real call.
- [ ] "How much have we spent on Claude this month?" is answered by voice from the spend log (not built).

## Blocking decisions

- A secure setup for the Anthropic key (Bill: no API keys until then; Claude stays a stub on purpose).

## Evidence and notes

Not started. The pool, decider, overrides, budgets and spend log are built and unit-tested; no real Claude call has been made.

2026-11-12, the third criterion's tool, built without a key (Bill: "lets finish the current phase"):

- A `spend` tool (`service: spend`, in both `tools.yaml` files) answers from the spend log: one agent or all of them, this month or last. "We've spent $4.50 on Claude this month, of a $30.00 budget."; "We spent $2.00 on Claude last month."; "Nothing spent on Claude this month." Amounts under a dollar are said in cents.
- It works while Claude is still a stub, because it only reads the log.
- Code: `SpendLog.month`, `SpendLog.by_agent`, `spend_answer`, `speak_usd` (`agents/spend.py`); the builtin in `tools/registry.py`. Tests: two in `tests/test_pools.py`. `uv run pytest -q`: 569 passed, 3 skipped.
- Live, typed chat, 15:08 UTC: "How much have we spent on Claude this month?" → "Nothing spent on Claude this month."; "What did Claude cost last month?" → "Nothing spent on Claude last month."; "How much have the agents cost us this month?" → "Nothing spent on agents this month." Each with a `spend` tool call in the log.

Still open: everything that needs a real Claude call (the first two criteria), and hearing the spend answer by voice with a real amount. The blocking decision (a secure key setup) is unchanged.

