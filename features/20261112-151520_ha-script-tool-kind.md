# `ha_script` tool kind: run a Home Assistant script by voice

- Status: open
- Phase: staging
- Origin: Architecture §8 tool kinds (`ha_service` / `ha_script`: "Optional direct Home Assistant calls for the most frequent smart-home actions, where latency matters"). Found unbuilt on 2026-11-12, when Bill asked which specified features are missing; Bill: "Yes, write it" (the feature file).
- Created: 2026-11-12 (UTC)

Today a tool with `kind: ha_script` is accepted by the config and becomes an automatic stub ("Home Assistant scripts"; `LATER_KINDS` in `tools/registry.py`). `ha_service`, its sibling, is built: one direct service call on entities with the voice label.

## Done when

- [ ] A tool with `kind: ha_script` runs the Home Assistant script it names, over the connection in `mcp_servers.homeassistant`, and says its reply. No second model call when the reply is a template.
- [ ] The tool's `params` are passed to the script as its variables.
- [ ] A script that doesn't exist, or a Home Assistant that can't be reached, gives a spoken error, and `assistant tools status` shows the tool as a stub with the reason when the script can't be found at startup.
- [ ] `confirm: true` asks before running, as it does for `ha_service`.
- [ ] `LATER_KINDS` no longer lists `ha_script`; the README, the example `tools.yaml` and `docs/home-assistant.md` describe it.
- [ ] Tests against the stand-in for Home Assistant; live check by chat with one harmless script, then by voice.

## Blocking decisions

1. **Whether to build it at all.** The architecture calls it optional. `ha_service` should already be able to run a script (`service: script.turn_on` on a labelled script entity, with `domains: [script]`; not tried), and the `home_control` tool reaches scenes. Proposal: build it only if there is a script you want by voice that those don't cover well; otherwise mark this `skipped` and take `ha_script` out of the accepted kinds.
2. **How a tool names its script.** Proposal: `service: script.<name>` on the tool, one script per tool, so the model chooses by the tool's own name and description ("movie night", "goodnight"). The alternative is one tool that takes the script's name as a parameter, which leaves the model to guess names.
3. **Which scripts are allowed.** `ha_service` and `home_control` act only on entities carrying the voice label. Proposal: the same for scripts: the script entity must have the label, so a script can't be run by voice unless you marked it in Home Assistant.
4. **Waiting for the script.** Proposal: start the script and reply at once ("Movie night is on."), not wait for it to finish. A long script would otherwise hold the turn. The alternative is to wait up to the tool's `timeout_s` and report a failure.
5. **Which script to test with.** A script in your Home Assistant that is safe to run while testing, or one made for the test (for example, one that only writes a log entry).

## Notes

- No satellites are needed. It waits for the Home Assistant devices to be redone, so it is in staging.
- Not started. No code has been written for it.

## Evidence and notes

Not started.

2026-11-12: moved to staging. Bill: "I'm going to be redoing all of my home assistant devices soon, so we can move any more device management stuff to staging".
