# Home control (Home Assistant)

`home_control` turns things on and off, sets brightness or temperature, and checks state, through Home Assistant's **Model Context Protocol Server** integration. `lock_door` locks a door after a spoken "yes". Only entities with the **`voice` label** can be controlled; everything else is refused ("The front porch outlet isn't set up for voice control.").

## Set up

1. **Install the integration** in Home Assistant: Settings > Devices & services > Add integration > "Model Context Protocol Server". Its endpoint is `http://<home-assistant>/api/mcp`.
2. **Point the assistant at it** in `config/tools.yaml`:

   ```yaml
   mcp_servers:
     homeassistant: { url: http://homeassistant.local/api/mcp, token: "${HA_TOKEN:-}" }
   ```

3. **Store a token**: Home Assistant > your profile > Security > Long-lived access tokens > Create token, then

   ```sh
   uv run assistant secrets set HA_TOKEN
   ```

4. **Choose what voice may control**: put the `voice` label on those entities (Settings > Labels, or an entity's settings). The entities must also be exposed to Assist (Settings > Voice assistants > Expose), since that's all Home Assistant's MCP server can act on.
5. Restart the assistant. `uv run assistant tools status` shows `home_control` and `lock_door` as `ready`; without a token they're stubs that send these steps to chat.

## What it does

- **On / off**: "turn on the bedroom light", "turn off the kitchen lights" (all labelled lights in the Kitchen area, or every labelled name the words match), "turn on bright kitchen" (a scene). A singular name that matches several asks "Which one: ...?".
- **Set**: "set the piano light to 40 percent" (lights), "set the hall thermostat to 70" (climate).
- **Check**: "is the bedroom light on?" reads the state directly: "The bedroom light is off."
- **Locks**: "lock the front door" asks "Lock the Front Door Lock?"; only a "yes" to that question locks it (a one-time code the model never sees, valid for 30 seconds).
- Replies are templates, spoken with no second model call. Devices that are offline are reported ("The Upper Porch Light isn't responding.") rather than tried.

## How targets are matched

The labelled entities, their names, aliases and areas are read from Home Assistant's registries (refreshed every minute). A spoken target matches, in order: an entity whose distinguishing words were all said ("bedroom light" in "the bedroom lights"); an area ("the kitchen": the labelled lights, switches, fans and covers there); then a name the words are part of ("front door" for "Front Door Lock"). The matched entity's exact name is passed to Home Assistant's intent, so it never acts on something unlabelled with a similar name.

Labels and aliases make names easier to say: add an alias in Home Assistant ("lamp" for "Hue white lamp 1") and the assistant picks it up within a minute.
