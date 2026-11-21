# Carried-over small checks

- Status: in progress
- Phase: current
- Origin: Stages 3, 11 and the document panel (carried over)
- Created: 2026-11-11 (UTC)
- Started: 2026-11-12, branch `feature-upgrade-feature-tool` (Bill: "ask all the qeustions you need right now and then get to work")

## Done when

- [x] Weather's home location is set in Bill's `config/tools.yaml` ("what's the weather" answers for home).
- [ ] The assistant service refuses a connection from another machine on the LAN, checked live (unit-tested only, Stage 3).
- [ ] The fallback model takes over when the primary model is really unloaded in LM Studio (simulated with a dead URL, Stage 3).
- [ ] Opening and closing the document panel leaves a voice session and music playing (not tried live).

## Blocking decisions

None.

## Evidence and notes

2026-11-12:

- **Weather home:** `weather.home` in `config/tools.yaml` reads `WEATHER_HOME_LAT` and `WEATHER_HOME_LON`, which are in the Keychain and `config/.env`, not in the YAML (Bill: the location is a secret). They were already set, and are within a few kilometres of the location Bill gave. Live, typed chat, 14:04 UTC: "What's the weather?" → "It is currently cloudy with a temperature of 44 degrees. …" (`tool_call` for `weather` in the log).
- **Refusing another machine:** not done from another machine. From this Mac, the service listens on `127.0.0.1:8765` only, and a connection to the Mac's own LAN address on port 8765 is refused. That shows the port isn't open on the network; a try from a second machine is still open.
- **Fallback model with the primary really unloaded:** can't be shown with this configuration. `fast` and `fast-small` are both `google/gemma-4-12b` on this Mac, so unloading the primary unloads the fallback too. It needs a different model for `router_fallback` (several are downloaded in LM Studio), which is Bill's choice, or the Mac mini's configuration.
- **Document panel with voice and music:** not done; it needs a voice session.
