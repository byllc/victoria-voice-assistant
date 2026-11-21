# Apple Reminders tool

- Status: in progress
- Phase: current
- Origin: Bill, 2026-11-11 ("can we add a tool that connects to a local mac reminders app?")
- Created: 2026-11-11 (UTC)
- Started: 2026-11-11, branch `feature-upgrade-feature-tool` (Bill: "Yes, accept all six")

## Done when

- [x] A `reminders` tool, configured in `tools.yaml`, adds a reminder ("remind me to call the plumber tomorrow at 9"), lists what's on a list or due ("what's on the shopping list?", "what are my reminders today?"), and marks one done ("I bought the milk").
- [x] It works on the reminders of the Mac running the assistant, which syncs through iCloud to Bill's phone.
- [x] Without access to Reminders, the tool is an automatic stub with the setup steps.
- [ ] Tests for the tool's actions, with Reminders itself replaced by a fake. Live check by voice and chat on a test list. (Tests and chat done; voice open.)

## Blocking decisions

None. Settled 2026-11-11: all six as proposed (Bill: "Yes, accept all six"). Decision 5 widened the same day: a list named in `tools.yaml` but missing from Reminders is created through EventKit (Bill: "lets use event kit, this will run on a mac mini"). Nothing is ever deleted or renamed. Decision 3 widened the same day: `reminders.lists: all` reaches every list in the signed-in account, and the tool can name them (Bill chose "Reach every list" over "Only name them"). Bill's dev config uses `all`, with `default_list: Victoria Test`; on the Mac mini, with the Victoria account, `all` means the lists shared with it.

1. **How to reach Reminders.** Proposal: Apple's EventKit framework, called in the assistant's own process through PyObjC (`pyobjc-framework-EventKit`, pinned in the lock file), as a `builtin` tool. It is fast (milliseconds), works offline, and needs no other app running. The alternatives are slower or add a moving part: AppleScript through `osascript` (a second or more per call, and it opens the Reminders app), the Shortcuts app, or a separate Reminders MCP server.
2. **Permission.** macOS asks once whether the program may use Reminders. Under launchd that program is the service's Python, and the question has to be answered at the Mac, on this Mac now and again on the Mac mini at setup. `assistant reminders grant` would ask for it from a Terminal, so the answer is on record before the service needs it. The runbook would cover it.
3. **Whose reminders, and which lists.** The Mac's signed-in iCloud account: Bill's now, and whichever account the Mac mini uses. Proposal: the tool reaches only the lists named in `tools.yaml` (`reminders.lists`, e.g. Shopping, Household), with a default list for "remind me to …". Lists not named stay private.
4. **Who can use it.** Proposal: any household device, like timers. Anyone in a room with a satellite could then hear the named lists read out, which is one reason to name only shared lists in decision 3.
5. **What it can do.** Proposal: add (title, optional due date and time, optional list), list (by list, or due today, overdue, or this week), and mark done. No deleting or editing: a misheard "delete" can't be undone, and that's easier from the phone.
6. **Testing on this Mac.** The dev Mac is signed in to Bill's account, so live tests would touch his real reminders. Proposal: a `Victoria Test` list, named in the dev config only, which the checks add to, list and complete.

## Notes

- Due times like "tomorrow at 9" are spoken. The model would pass them as written, and the tool would parse them with the same approach the timers use for durations, in local time.
- A due reminder alerts on Bill's devices through Reminders itself. The assistant doesn't announce it, unless a later feature adds that.
- No satellites are involved, so this is current-phase work.

## Evidence

Built 2026-11-11:

- `tools/reminders.py`: `RemindersService` (add, list, complete; never delete or edit) over a `ReminderBackend`, with `EventKitBackend` for Apple Reminders (`pyobjc-framework-eventkit`, in the lock file). Spoken due times ("tomorrow at 9", "Friday", "in 2 hours", "November 20 at 3") are read in local time by `parse_due`; a bare "at 3" is the afternoon, and a time already past today is tomorrow. A timed reminder gets an alarm, so Reminders alerts on the phones. Completing matches on the title's words; several matches ask which (a clarify question).
- Only the lists in `reminders.lists` are reached (`RemindersConfig`, with `default_list`); a list named in a request but not in config gets "I can only use these lists: …".
- The `reminders` tool in both `tools.yaml` files, with `forget_after_s: 0` (Reminders holds the state). Bill's config names only `Victoria Test`; the example names Shopping and Household.
- Access: until macOS allows it, the tool is an automatic stub with the setup steps. The service asks for access when the tool loads (once, on its own thread), and when it's allowed the app reloads the tools so the stub goes away without a restart.
- `assistant reminders grant` (asks for the program it runs in, i.e. Terminal) and `assistant reminders lists`.
- Docs: architecture v2.14 (§8 "Built-in reminders", §11), new `docs/reminders.md`.
- Tests: `tests/test_reminders.py` (41), with Reminders replaced by a fake. `uv run pytest -q`: 440 passed, 3 skipped. `assistant config check` passes on Bill's config.

Change to decision 2, confirmed live: macOS keeps Reminders access per program. The service runs under launchd as its own Python, so access granted from a Terminal (`assistant reminders grant`) covers Terminal, not the service. After Bill allowed the service, `assistant reminders lists` from a shell still said `not_determined`. The service therefore asks for itself; `grant` stays for the command-line checks. Docs say so.

Live, 2026-11-11:

- 22:03 UTC: the restarted service logged `reminders_access_asked`; the tool was an automatic stub.
- 22:06:04 UTC: Bill allowed it at the Mac. `reminders_access` was logged and the tools were rebuilt (`tools_loaded`, no `tool_auto_stub`), with no restart. After later restarts it loads ready and doesn't ask again.
- Typed chat, 22:08 UTC: "What's on the Victoria Test list?" → "There are no items on the Victoria Test list."; "What are my reminders today?" → "You have no reminders due today." (each a `reminders` call). But every add and complete failed: the service sees 10 lists and none is called `Victoria Test`, even matching without case or extra spaces.
- Fixes from this: the log keeps only its listed fields, so the reminders events now use `status`, `kind` and `reason`. A missing list is said ("There's no Victoria Test list in Reminders.") and logged with how many lists are visible (no names). List names match ignoring case and extra spaces.
- Also seen: "Add bread to the shopping list." (not a configured list) got "Which list should I add bread to?" from the model rather than the tool's "I can only use these lists" line.

- Cause: `Victoria Test` didn't exist in Reminders (Bill didn't see it on his phone either). Fix: the service creates a configured list that's missing, once, when the tool loads (`ensure_lists`), in the local "On My Mac" account when there is one, else the default account. 22:19 UTC: `reminders_list_created`, `status: in iCloud` (this Mac has no local account, so the list syncs to the phone).
- Typed chat, 22:19-22:21 UTC, all on `Victoria Test` (the only list in Bill's config):
  - "Remind me to call the plumber tomorrow at 9." → added, due tomorrow 9 AM (seen again by "What are my reminders this week?" → "call the plumber tomorrow at 9 AM").
  - "Add milk …", "Add oat milk …", then "What's on the Victoria Test list?" → all three.
  - "I bought the milk." was first sent as an add (a duplicate "Milk"). Fix: the `action` description says complete is for "the user says they did it (I bought the milk, …)". After that: "I bought the milk." → `complete` `milk`; "I called the plumber." → `complete`; "Cross off the oat milk." → `complete`; a second "I bought the milk." cleared the duplicate; the list ended empty.
  - Same title in different case ("milk", "Milk") is one match, not a "which one?" question.
- Tests: 43 in `tests/test_reminders.py`; `uv run pytest -q`: 442 passed, 3 skipped. `assistant config check` passes (it shows the tool as a stub only because the check doesn't start Reminders).

All lists, typed chat on the restarted service (`lists: all`):

- "What lists do you have?" → `lists`: all 11 lists in Bill's account, Victoria Test included.
- "What are my reminders today?" → `list` `when: today`, across every list: one reminder.
- "Remind me to test the voice check tomorrow at 10." → added to Victoria Test (the default), due tomorrow 10 AM; "What's on the Victoria Test list?" → that reminder.
- Tests: 45 in `tests/test_reminders.py`.

Numbered lists (Bill, 2026-11-11: "can you format the list output as a numeric list?"): the tool now answers in its own words (`reply: { template: true }`, no second model call), and a list of lists or reminders is numbered, one per line, which the chat shows as a list. Typed chat, 21:06 local:

- "What lists do you have?" → "You have 11 lists:" then 1-11.
- "What are my reminders this week?" → "2 reminders due this week on your lists:" then each with its list and due date (an overdue one included).
- "Add bread to the garage list." → "There's no garage list. The lists are …".
- Tests: 46 in `tests/test_reminders.py`. Not yet heard by voice (how the numbers sound).

List names and numbers (Bill, 2026-11-11: "if i get the speilling wrong or the cases wrong it fails" and "I also want to choose the number in the list"):

- A spoken list name matches whatever the case, word order or small misspellings (`list_score`: the words said found in the name, then how much of the name was said, then spelled-out similarity). "house" means House, not Carriage House Needs. Names that fit two lists about equally ("packing", "shared") ask "Which list: …?". An unknown name answers with the numbered lists.
- The tool remembers the last numbered list shown on each device for 10 minutes, so "show me 4" opens list 4 and "number 2 is done" completes reminder 2 (digits, "four", "fourth", "4th"). Another device's numbers don't count.
- `forget_after_s` for reminders went from 0 to 120: a bare "show me 4" needs the listing still in the conversation for the model to send it to `reminders`. The tool, not the model, holds the numbers, and every answer rereads Reminders.
- Typed chat, 21:14-21:16 local: "What lists do you have?" then "Show me 11." → Victoria Test; "show me number 4" → Groceries; fresh conversations: "Show me the grocerys list" → Groceries; "show me the famly list" → Family; "Show me the packing list" → "Which list: Vacation Packing or Bill Work Trip Packing?", "The vacation one" → Vacation Packing.
- Tests: 66 in `tests/test_reminders.py`; `uv run pytest -q`: 465 passed, 3 skipped.

Add reply (Bill, 2026-11-11: "respond with the reminder that youa dded and what list you added to"): always "I've added “X” to Y", with ", due …" when it has a due time. Typed chat, 21:22 local: "Remind me to water the plants tomorrow at 8 am." → "I've added “water the plants” to Victoria Test, due tomorrow at 8 AM."; "I watered the plants." → "Marked “water the plants” done." `uv run pytest -q`: 465 passed, 3 skipped.

Still to do: the voice check (Bill). Victoria Test holds "test the voice check" (due tomorrow at 10) for it.

## Code review, 2026-11-12

Bill asked for a code review, then for the findings to be fixed without him ("can you fix these without my input? ... I need to go to bed"). Ten findings; all fixed. `uv run pytest -q`: 556 passed, 3 skipped.

- A list's own name is matched first, so lists called "My List", "List" or "2026" can be opened (they were read as filler words or a list number).
- The numbered list a "show me 4" refers to is remembered per browser (the dashboard's `client_id`), else per device. Every dashboard chat is the device `web`, so before this one browser's numbers applied to another's.
- Reminders is asked off the event loop, once per request (access, the list names and the default list), not several times on the loop that carries audio.
- When Reminders access is granted, every config listener runs (`ConfigManager.notify`), so satellites rebuild their tools too, not only dashboard chats. Not seen live: access was already granted on this Mac.
- A question at the end of a spoken reply stays open longer by the time it takes to say the reply (`SPOKEN_WORDS_PER_S`, `ToolRuntime.run`). This applies to every tool's questions. Not heard by voice.

**A test touched Bill's real Groceries list** (03:15 UTC). Live check, one conversation: "What lists do you have?", "Show me 4." (list 4 is the real Groceries list), then "Add red onions, white onions and milk to groceries." With the Groceries listing still in the conversation (`forget_after_s: 120`), the model used the `reminders` tool, not `groceries`: it added one reminder, "red onions, white onions and milk", to the real **Groceries** list, and "We got the onions and the milk." then marked it done. It is there now as a completed reminder; the reminders tool can't delete it. Nothing else on that list changed.

Open question for Bill from this: with `lists: all`, "add X to groceries" can go to either tool when a list of that name exists. One way to settle it: the general `reminders` tool leaves alone any list a list tool owns. Not done; it changes what the reminders tool reaches.
