# Home Assistant satellite behaviour

- Status: ready for review
- Phase: current
- Origin: Stage 14
- Created: 2026-11-06 (UTC)
- Started: 2026-11-12, branch `feature-upgrade-feature-tool` (Bill: "ask all the qeustions you need right now and then get to work")

## Done when

- [ ] Home Assistant's own behaviour with the satellite is documented in `docs/satellites.md`, and Bill accepts it.

## Blocking decisions

None.

## Evidence and notes

Written 2026-11-12: `docs/satellites.md`, "5. Home Assistant", from a read of Home Assistant's states (2026.10.4; nothing was changed there).

- The office Satellite1's 31 own entities have been `unavailable` since the key changed on 2026-11-02 (Assist satellite, LED ring, buttons, switches, selects, sensors, its media player and remote timer).
- Still live: the network device tracker (`home`), and `media_player.bill_s_office_satelite` (`idle`), which is not from the ESPHome entry; which integration provides it wasn't checked.
- Not checked: whether Home Assistant shows a repair notice for the entry, and what its log says when it retries. Those need Home Assistant's own pages.
- The Waveshare test satellite has no entities in Home Assistant.
- The doc gives the choices: leave the entry, or delete it.

The criterion is Bill's acceptance, so it stays unticked.
