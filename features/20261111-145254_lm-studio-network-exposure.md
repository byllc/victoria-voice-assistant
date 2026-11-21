# LM Studio open to the LAN

- Status: ready for review
- Phase: current
- Origin: Architecture review, 2026-11-11 (Stage 7 finding)
- Created: 2026-11-11 (UTC)

## Done when

- [x] LM Studio's server is reachable only from this Mac and the Hermes container, not the whole LAN (it listens on `*:1234`, which the container needs to reach it at `192.168.64.1`), or Bill accepts it as it is.
- [x] The LM Studio key that Hermes holds (Stage 7 exception) is decided: kept, or replaced with a key of its own.
- [x] The outcome is recorded in `docs/hermes.md`.

## Blocking decisions

- Settled: not restricted. Bill accepts it as it is (2026-11-12: "leave LM Studio on the lan it wil lbe access from other tools and this is a home network").
- Settled: the shared key is kept on the dev machine (Bill, 2026-11-12: "the shared key is a dev key, those should be config and keychain bound and will be set in staging").

## Evidence and notes

2026-11-12: Bill chose to leave this for now ("Leave it for now"). Nothing was changed; the decision on how to restrict it is still open.

2026-11-12, later: Bill accepted LM Studio being reachable on the LAN. Recorded in `docs/hermes.md`, Security. The third criterion stays unticked until the key question is answered and recorded there too.

2026-11-12, later still: the key is decided and recorded in `docs/hermes.md`, Security. Nothing in the code or configuration changed: the key is already named as `${LMS_API_KEY}` in config and held in the Keychain. Setting the Mac mini's own keys belongs to the staging deployment feature (`20261111-145250`).
