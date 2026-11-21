# Timer reply wording

- Status: ready for review
- Phase: current
- Origin: Stage 14
- Created: 2026-11-06 (UTC)
- Started: 2026-11-12, branch `feature-upgrade-feature-tool` (Bill: "ask all the qeustions you need right now and then get to work")

## Done when

- [x] The timer reply "1-minute timer set for 1 minute." no longer repeats itself.

## Blocking decisions

None.

## Evidence and notes

Built 2026-11-12:

- An unnamed timer is stored under its length ("1-minute"), and the reply said that name and then the length again. The reply now leaves the name out when none was given: "Timer set for 1 minute."; a second one is "Second timer set for 1 minute." (`tools/timers.py`).
- A name that is only the timer's own length ("1-minute timer", "one minute"), which the model sometimes passes, counts as no name.
- Named timers are unchanged: "Pasta timer set for 10 minutes."
- Test: `test_unnamed_timer_reply_does_not_repeat_its_length` (`tests/test_tools.py`). `uv run pytest -q`: 567 passed, 3 skipped.
- Live, typed chat on the service, 13:57 UTC: "Set a timer for one minute." → "Timer set for 1 minute."; "Cancel the timer." → "1-minute timer cancelled."

Not heard by voice.
