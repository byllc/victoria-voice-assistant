# Calendar

The `calendar` tool works on the calendars of the Mac running the assistant, through EventKit. It reaches every account added to macOS (iCloud, Google, Exchange), so a Google calendar is reached by adding the Google account in System Settings > Internet Accounts with Calendars turned on. Architecture §8 has the design.

Checked by typed chat on 2026-11-12: reading on the real calendars; adding, moving, changing and deleting on a test calendar. Not yet checked by voice.

Say "appointment", "event" or "calendar" in a change or a cancel ("cancel my dentist appointment"): without one, "cancel the …" can go to the timer tool.

## What it does

Reading:

- "What's on my calendar today?", "tomorrow", "Friday", "this week", "next week", "this weekend". All-day events come first.
- "What's next?" and "When is the dentist?"
- "Tell me about the dentist appointment": the place, the notes, who's invited.
- "Am I free Friday afternoon?", "When am I free tomorrow?" It looks between 8 AM and 10 PM (`free_from`, `free_until`).
- "Which calendars do I have?"; "what's on the work calendar today?"

Writing:

- "Add lunch with Sam Friday at noon." An hour long when no length is said; "for 90 minutes" or "until 3" sets it. No time means all day. It can also take a place, notes, an alert ("with an alert 15 minutes before") and a repeat (daily, weekdays, weekly, monthly, yearly). Adding doesn't ask first; the reply says what was added and where.
- "Move the dentist to 3", "move it to Monday", "make it two hours", "rename it", "change the place". It asks first ("Dentist, Friday at 2 PM to 3 PM: move it to Friday at 3 PM to 4 PM?") and changes it on a yes.
- "Cancel my haircut." It asks first and deletes on a yes.
- A repeating event asks "just this one, or all of them from now on?" before a change or a delete.
- When several events match, it lists them with numbers and asks which. After any numbered list, "move number 2 to 4" works for 10 minutes, on the device the list was shown on.

A yes counts for 2 minutes and only on the device that was asked. A read-only calendar (a subscribed holidays calendar) is read but never changed.

It doesn't invite people, answer invitations or add a video link: EventKit can't. Those need the Google backend, which isn't built (feature `20261112-184425`).

## Setup

1. Settings, in `config/tools.yaml`. Every calendar is reached unless it's named under `skip`. Anyone near a satellite can hear events read out and can add or cancel one, so skip what isn't the household's.

   ```yaml
   calendar:
     skip: [Birthdays]            # never read or written
     default_calendar: Family     # where new events go; the Mac's default calendar when not set
     default_minutes: 60
     free_from: 8
     free_until: 22
   ```

2. Allow access. macOS asks once per program, at the Mac. The service asks for itself when the `calendar` tool loads: choose full access (reading needs it; "add only" isn't enough). If no prompt shows, allow it in System Settings > Privacy & Security > Calendars. Once it's allowed, the tool stops being a stub without a restart.

   For the command-line checks, Terminal needs access too:

   ```bash
   uv run assistant calendar grant
   ```

3. Check:

   ```bash
   uv run assistant calendar calendars
   ```

   lists each calendar with its account, marks the ones the tool reaches, and says where new events go.

   ```bash
   uv run assistant calendar new "Victoria Test"
   ```

   creates a calendar on this Mac, for trying writes away from the real ones.
