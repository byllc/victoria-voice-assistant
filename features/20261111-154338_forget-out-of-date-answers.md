# Out-of-date answers repeated from the conversation

- Status: in progress
- Phase: current
- Origin: Bill, 2026-11-11 ("we probably need a way to tell it not to do that for some tools, like time and weather")
- Created: 2026-11-11 (UTC)

## Done when

- [x] A tool can be marked in `tools.yaml` so its answers don't stay in the conversation: `forget_after_s`.
- [x] Set for the clock, timers and music (0: gone by the next request) and the weather (600 s) in the example config and Bill's.
- [ ] By voice: "what time is it?" asked twice, a minute or more apart, gives the right time both times, with a `clock` call in the log for each.

## Blocking decisions

None. Settled 2026-11-11:

- Timers and music get it too: `forget_after_s: 0` (Bill: "Yes, both").
- The tools hold the state, not the model (Bill: "the MCP should handle remembering the timers, their duration, and status, not the model").
- A bare "cancel it" isn't meant to work; a command names its tool ("cancel timer"), and with more than one timer the tool asks which (Bill: "'cancel it' is not a command that should work on it's own, it needs more context"). So no live "active now" note (proposed earlier, withdrawn).

## Evidence and notes

Found 2026-11-11 on the office satellite: "What time is it?" was answered "It's 10:24 AM." at 10:27, and "It's 10:35 AM." twice at 10:36. The model had its own earlier answer in the conversation and repeated it instead of calling `clock` (no `tool_call` logged for those turns).

Built:

- `forget_after_s` on a tool (`config/schema.py`, carried on the registry entry). When such a tool is called, the call is noted with the time its answer goes out of date (`chat/tools.py`, `ToolRuntime.forget`).
- At the start of each request, exchanges whose answers are out of date are taken out of the conversation whole, from the user's request up to the next one (`forget_stale`, called from `TurnPreprocessor._handled`), so the conversation still alternates and nothing is left to copy. Logged as `conversation_forgot`. It applies to chat, browser voice and satellites, which share the preprocessor.
- The weather keeps its exchange for 10 minutes so a follow-up ("and tomorrow?") still has its context.
- Architecture §8 lists the option.
- Tests (`tests/test_tools.py`): an out-of-date exchange leaves and the rest stays, in order; the setting comes from the tool's config and can't be negative. `uv run pytest -q`: 380 passed, 3 skipped. `assistant config check` passes on Bill's config.

Not covered: a yes/no answered without the model, or an agent's reply, since neither is a tool call in the conversation.

2026-11-11, on the running service (restarted 15:43 UTC with this code; the clock and weather load without errors). Before the restart, the service had reloaded the edited `tools.yaml` while still running the old code, and both tools briefly became automatic stubs (`forget_after_s: Extra inputs are not permitted`, 15:42 UTC). The restart cleared that. A service that reloads config live has to be restarted before it picks up a new `tools.yaml` field.

By typed chat, one conversation on the service's own `/ws`, "What time is it?" 70 s apart:

- 16:07:17 UTC: "It's 11:07 AM." with a `clock` `tool_call` (16:07:19).
- 16:08:29 UTC: `conversation_forgot` (count 1), then a `clock` `tool_call` (16:08:30): "It's 11:08 AM."

The voice check is still open. It needs someone speaking to a satellite or the browser.

Timers and music with `forget_after_s: 0`, by typed chat (16:12-16:14 UTC). Asked twice 20 s apart, "How long is left?" gave 14:56 and then 14:35, each from its own `timer` `status` call. "What's playing?" asked twice made two `now_playing` calls. "Add 5 minutes to it." still worked, because the timer service picks the only running timer. But "Cancel it." went to the `tasks` tool (`cancel`) and the reply was "Nothing is running.", while the pasta timer kept going: the exchange that set the timer had already left the conversation, so "it" pointed nowhere. Without the setting on timers, "Set a pasta timer for 10 minutes." then "Cancel it." gave "Pasta timer cancelled." Music would lose follow-ups the same way ("play their latest album"). Both settings were taken back out of both configs. `uv run pytest -q`: 380 passed, 3 skipped.

After Bill's ruling ("cancel timer", not "cancel it"), both settings were put back. Typed chat, 16:17 UTC: with one timer, "Cancel timer." cancelled it. With two, "Cancel timer." asked "Which timer: pasta or laundry?", but "The laundry one." gave the laundry timer's time left and didn't cancel it. The exchange holding the question had left the conversation before the answer came in (`conversation_forgot` at 16:17:44).

Fix: nothing is forgotten while a tool's question is still open (`TurnPreprocessor._handled`). The exchange leaves on the request after the answer. Test: `test_nothing_is_forgotten_while_a_tool_question_is_open`. `uv run pytest -q`: 381 passed, 3 skipped. Service restarted.

Typed chat again, 16:19 UTC: pasta and laundry set; "Cancel timer." → "Which timer: pasta or laundry?"; "The laundry one." → "Laundry timer cancelled."; "What timers do I have?" → "You have one timer: pasta, 9 minutes 52 seconds."; "Cancel timer." → "Pasta timer cancelled."; "How long is left?" → "You don't have any timers."
