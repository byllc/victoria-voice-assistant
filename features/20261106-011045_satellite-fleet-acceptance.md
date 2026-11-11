# Satellite fleet acceptance

- Status: open
- Phase: staging
- Origin: Stage 13 (open for acceptance)
- Created: 2026-11-06 (UTC)

## Done when

- [ ] A satellite added to or removed from `tools.yaml` connects or disconnects without a restart. An offline satellite shows in the health strip and the dashboard.
- [ ] One command updates the firmware of every satellite and reports each one's version.
- [ ] An announcement to several rooms is synthesized once and plays in each.
- [ ] The dashboard shows every satellite's room, online state, voice state, latest request, and timers, updated live.
- [ ] "Turn off the lights" from a satellite acts on that room's Home Assistant area.
- [ ] The architecture changes proposed in the decisions log (2026-11-02) are decided.

## Blocking decisions

- The 2026-11-02 architecture proposals.

## Evidence and notes

Evidence is recorded in the Stage 13 section of `docs/stages.md`. Live checks so far used one or two satellites; the firmware update has not been run across several boards.

Bill, 2026-11-11: "any featrues that involve more than one satellite need to be in staging". Moved to the staging phase.
