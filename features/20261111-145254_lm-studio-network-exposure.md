# LM Studio open to the LAN

- Status: open
- Phase: current
- Origin: Architecture review, 2026-11-11 (Stage 7 finding)
- Created: 2026-11-11 (UTC)

## Done when

- [ ] LM Studio's server is reachable only from this Mac and the Hermes container, not the whole LAN (it listens on `*:1234`, which the container needs to reach it at `192.168.64.1`), or Bill accepts it as it is.
- [ ] The LM Studio key that Hermes holds (Stage 7 exception) is decided: kept, or replaced with a key of its own.
- [ ] The outcome is recorded in `docs/hermes.md`.

## Blocking decisions

- How to restrict it: LM Studio's own key check, the macOS firewall, or binding to the container network's address.

## Evidence and notes

Not started.
