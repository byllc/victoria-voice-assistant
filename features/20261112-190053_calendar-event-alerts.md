# Spoken alerts before calendar events

- Status: open
- Phase: later
- Origin: Bill, 2026-11-12, when asked whether Victoria should speak up before events ("You have the dentist in 15 minutes"): "Later, separate feature (Recommended)". Split from the [calendar tool](20261112-184425_calendar-tool.md).
- Created: 2026-11-12 (UTC)

## Done when

- [ ] Before an event, Victoria says it ("You have the dentist in 15 minutes") through its notifications, so do-not-disturb and the room rules apply.
- [ ] Tests with a stand-in calendar and clock; live check on a test calendar, then by voice.

## Blocking decisions

1. **Which events.** Proposal: only events that have an alert set in the calendar, at the alert's time. The alternative is every timed event, a fixed number of minutes before.
2. **Which calendars.** Proposal: a list named in config (`calendar.announce`), empty at first, so nothing is announced until you name one.
3. **Where it's said.** Proposal: as other notifications are: the rooms that are in use, and the phone when subscribed.
4. **Phase.** It is in `later` until you say when; if it should be said on several satellites it is staging work.

## Evidence and notes

Not started.
