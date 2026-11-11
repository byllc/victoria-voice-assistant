# Synced music across rooms

- Status: open
- Phase: staging
- Origin: Stage 15 (from Stage 13)
- Created: 2026-11-06 (UTC)

## Done when

- [ ] "Play … in the kitchen and the office" plays in sync.
- [ ] Adding and removing satellites by voice works while music plays.
- [ ] Ducking affects only the room that is talking.
- [ ] Check findings reviewed, and the music route agreed, before audio work starts.

## Blocking decisions

- Music on satellite speakers, existing speakers, or both (architecture open question 1).
- The music route (from the check findings).

## Evidence and notes

Room registry and music pause/resume per room are in place (`victoria_voice_assistant/rooms.py`). Only the office satellite has a music room player; no multi-room sync has been heard.

Bill, 2026-11-11: "sync'd music is a staging feature, lets move it forward". Moved to the staging phase.
