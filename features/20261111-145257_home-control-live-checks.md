# Home control live checks

- Status: in progress
- Phase: staging
- Origin: Stage 9 (carried over)
- Created: 2026-11-11 (UTC)

## Done when

- [ ] Scenes, brightness, climate and locks checked live against Home Assistant, each when Bill allows it or it is available.
- [ ] A lock asks for confirmation before it acts.

## Blocking decisions

- Settled (Bill, 2026-11-12: "you can control all switchese and lights, read any sensors and read any temperature information"): switches and lights may be driven; sensors and temperatures may be read. Not covered, so not driven: locks, setting a thermostat, scenes.

## Evidence and notes

Not started. Lights on and off were checked in Stage 9.

Live, 2026-11-12 15:16-15:18 UTC, typed chat on the service, each request in a fresh conversation, with Home Assistant's state read after each. Only lights and switches were driven; both test devices were off before and are off now.

**What worked**

- Reading a state: "Is the piano light on?" → "The piano light is off."; "Is the front porch light on?" → "The Front Porch Light is on."; "Is the bedroom light on?" → "The bedroom light is off." Each matches Home Assistant.
- Routing and matching: every on, off and set request reached `home_control` with the right action and target, in about 0.1-0.2 s per call.

**What did not: the devices don't follow**

- "Turn on the piano light." → "Turned on the piano light."; "Set the piano light to 40 percent." → "Set the piano light to 40 percent."; "Turn on the china cabinet upper." → "Turned on the china cabinet upper." After each, Home Assistant still showed the device `off`, including 10 s later.
- Home Assistant's own reply to the intent is success: `{"response_type": "action_done", "data": {"success": [{"name": "piano light", …}], "failed": []}}`. So the assistant sent the right command and Home Assistant accepted it.
- No real light or switch in Home Assistant has changed state since 2026-11-05 (the piano light since 01:36 UTC that day, the kitchen and china cabinet switches since 03:18, the bedrooms since 03:45). Only network-gear LEDs have changed since. The Fibaro leak sensor has not reported since 2026-11-01. That points at the integration or radio those devices are on being down in Home Assistant, not at one device. Nothing was changed in Home Assistant to look further.
- A finding for the assistant: it says "Turned on …" on Home Assistant's word alone. When the device doesn't follow, the reply is wrong. Proposal, not built: after an on, off or set, read the state back for up to about two seconds and say "… didn't respond" when it hasn't changed. The risk is a slow device being reported as not responding.

**Not checked**

- Brightness and on/off on a device that responds: none does at the moment.
- Scenes, climate and locks: not covered by the permission (and there are no climate entities; both locks are `unavailable`).
- Temperatures: "What's the temperature inside the house?" → "I couldn't find house." `home_control` reads the state of labelled lights, switches and the like, not sensors. Home Assistant has 13 temperature sensors, 9 reporting: eight are the NAS's drive and system temperatures and one is the leak sensor (74.9 °F, last reported 2026-11-01). There is no room temperature sensor to read. Reading sensors by voice would be a new piece of the tool.
- The lock confirmation criterion.

**Reading the state back, built 2026-11-12** (Bill: "Yes, build that")

- After an on, off or set, `home_control` reads the device's state for up to 2 s (`FOLLOW_WAIT_S`, every 0.25 s) and reports a device that hasn't changed: "The piano light didn't respond." With several devices: "Turned on the kitchen island light. The Kitchen cabinets didn't respond." It logs `ha_not_followed`.
- A device already in the state asked for counts as done. A state that can't be read counts as done too, so only a device seen not to change is reported. Scenes aren't checked. For a light's level, the brightness must be within 5 points; for a thermostat, the set temperature within half a degree.
- A reply for a device that works now comes when its state changes, usually well under the 2 s; for one that doesn't, after the 2 s.
- Tests: `test_a_device_that_does_not_follow_is_reported`, `test_one_silent_device_among_several_is_named` (`tests/test_home.py`); the stand-in Home Assistant now changes state when commanded. `uv run pytest -q`: 571 passed, 3 skipped.
- Live, typed chat, 15:21 UTC: "Turn on the piano light." → "The piano light didn't respond."; "Set the piano light to 40 percent." → "The piano light didn't respond."; "Turn off the piano light." → "Turned off the piano light." (it was already off); "Is the piano light on?" → "The piano light is off."
- Not seen: a device that does respond, so the success path with the read-back is covered by tests only until the devices are back in Home Assistant.

2026-11-12: moved to staging. Bill: "I'm going to be redoing all of my home assistant devices soon, so we can move any more device management stuff to staging".
