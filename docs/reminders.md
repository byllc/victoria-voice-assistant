# Reminders

The `reminders` tool works on Apple Reminders on the Mac running the assistant. Reminders syncs through iCloud, so what it adds shows up on the phones, and timed reminders alert there. Architecture §8 has the design.

## What it does

- "Remind me to call the plumber tomorrow at 9." adds to the default list, with an alert at 9.
- "Add milk to the shopping list."
- "What's on the shopping list?", "What are my reminders today?", "Anything overdue?"
- "I bought the milk." marks it done. With several matches it asks which.

- "What lists do you have?" answers with a numbered list; then "show me 4" opens list 4. After a list of reminders, "number 2 is done" marks the second one done. Numbers count for 10 minutes, on the device they were shown on.
- List names don't need the right case or spelling: "show me grocerys" finds Groceries. When a name fits two lists, it asks which.

It never deletes or edits a reminder. Do that on the phone.

## Setup

1. Name the lists it may use in `config/tools.yaml`. Every other list stays private. Anyone near a satellite can hear these read out, so name only shared lists.

   ```yaml
   reminders:
     lists: [Shopping, Household]
     default_list: Household   # where "remind me to ..." goes; the first list when not set
   ```

   A list that isn't in Reminders yet is created when the tool loads: in "On My Mac" if this Mac has that account, otherwise in the default account (iCloud, so it shows on the phones). To see the lists:

   ```bash
   uv run assistant reminders lists
   ```

2. Allow access. macOS asks once per program, at the Mac. The service runs under launchd as its own program, so it asks for itself when the `reminders` tool loads: answer that prompt at the Mac. If no prompt shows, or it was refused, allow it in System Settings > Privacy & Security > Reminders. Once it's allowed, the tool stops being a stub without a restart.

   For the command-line checks above, Terminal needs access too:

   ```bash
   uv run assistant reminders grant
   ```

Until access is allowed, the tool is an automatic stub that says it isn't set up, and the log shows `tool_auto_stub` with these steps.

## On the dev Mac

The dev Mac is signed in to Bill's account. Its config has `lists: all` (Bill, 2026-11-11), so the assistant reaches all his lists, and `default_list: Victoria Test`, so new reminders from testing go to that list. The service created it in iCloud on 2026-11-11.

## The groceries tool

A tool tied to one list, read whole. Unchecked means we need it; checked means we have it.

- "Do we need carrots?", "What do we need from the store?": the whole list goes to the model, which answers.
- "Add ice cream to groceries", "We're out of pizza": added, or unchecked if it was checked off before.
- "We got the milk and the eggs": checked off.
- "Take carrots off the grocery list": removed (this tool only; the reminders tool never deletes).

Its list is set on the tool in `config/tools.yaml`; the dev Mac uses a test list:

```yaml
  - name: groceries
    kind: builtin
    service: reminders.list
    list: "Victoria Groceries"     # change to Groceries for the real list
```

More list tools (hardware, Costco) are more entries like it, each with its own `list:`.
