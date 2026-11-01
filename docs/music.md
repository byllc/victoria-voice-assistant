# Music (Music Assistant) and the Alexa relay

Music runs through **Music Assistant**, which streams Plex (and Spotify, when connected there) to its players: AirPlay, Cast, DLNA, Home Assistant media players, Sendspin and others. The assistant only controls it.

## Set up

1. **Music Assistant** must be running somewhere on the network; here it's the Home Assistant add-on at `http://homeassistant.local:8095`. Connect your music sources in Music Assistant (Settings > Providers): Plex is connected; **Spotify is not**, so "on Spotify" answers "Spotify isn't connected in Music Assistant." until it is.
2. **Token**: open Music Assistant directly (port 8095, not the Home Assistant sidebar), your user profile > create a long-lived token, then

   ```sh
   uv run assistant secrets set MUSIC_ASSISTANT_TOKEN
   ```

3. **`config/tools.yaml`**:

   ```yaml
   music:
     server: http://homeassistant.local:8095
     token: "${MUSIC_ASSISTANT_TOKEN:-}"
     source_order: [plex, spotify]   # searched in this order unless the request names a source
     rooms: {}                         # optional, see below
     duck_level: 20
   ```

   and the `music` tool with `kind: builtin`, `service: music`. Restart the assistant; `uv run assistant tools status` shows `music` as `ready` (a stub with these steps until the token is set).

## Where it plays

Music plays where you say (Bill, 2026-11-01): "play Footprints in the exercise room", "play some Miles Davis on the Mac mini". Any Music Assistant player can be named by its name. Without a place, the first request from a device asks "Where should I play it: ...?"; after that the device keeps using that player until you name another. A satellite (Stage 12) plays in its own room.

`rooms` adds friendlier names and ties a satellite to its room:

```yaml
rooms:
  exercise room: { player: Exercise room speaker, aliases: [gym] }
  office: { player: "Bill’s Mac mini", satellite: office-sat }
```

## The web app as a speaker and player

Above the tabs, the web app has a music player: artwork, title, artist and album, the track time and a progress bar, previous / play-pause / next, and volume. Pressing play (or the speaker button, **🔈 Off** / **🔊 Here**) turns that browser into a Music Assistant speaker ("Victoria (Safari on iPhone)") over Sendspin, Music Assistant's streaming protocol (`@sendspin/sendspin-js`). Music asked for from that browser with no place named then plays right there ("Playing Footprints by Miles Davis from Plex here."). Voice and chat commands (pause, skip, volume) and ducking act on it too, and it keeps playing while you switch tabs.

- The web interface relays the browser's `/sendspin` connection to Music Assistant's Sendspin listener (port 8927 on the `music.server` host; `music.sendspin_url` overrides it), behind the household login, so it works from the HTTPS page. Music Assistant's `/sendspin` on its API port isn't used: it wants Music Assistant's own login first ("First message must be auth").
- Music Assistant shows the browser as a "universal player" with its own id; the browser's Sendspin id is that player's output id, which is how "here" finds it.
- Artwork, which Music Assistant serves over plain HTTP, comes through the web interface (`/api/music/artwork`, images from the Music Assistant host only).
- Browsers only allow audio after a tap, so after reloading press play (or the speaker button) again.

## What you can say

- **Play**: an artist, album, track or playlist; "from Plex" / "on Spotify" picks the source. The reply says what was found and where: "Playing Footprints by Miles Davis from Plex on the Exercise room speaker." If the exact album isn't in the library, the closest match plays (often the artist), and the reply names it.
- **Control** (the player that's playing, or the one this device last used): pause, resume, skip, go back, stop, "volume 30", "turn it down", "what's playing?".
- **Ducking**: while you talk to the assistant by voice, music on that device's player drops to `duck_level` and comes back after the reply.
- An unreachable server: "I can't reach the music server right now."

## The Alexa relay

"Ask Alexa to …" sends the words to an Echo, as if spoken to it, through Home Assistant's community **Alexa Media Player** integration (`media_player.play_media`, `media_content_type: custom`). The Echo answers or plays on its own; the assistant only says "Asking Alexa." The agent is in `agents.yaml`:

```yaml
- name: alexa
  aliases: [alexis]
  kind: relay
  via: ha_service                   # the Home Assistant connection (mcp_servers.homeassistant)
  service: media_player.play_media
  content_type: custom
  targets: { kitchen: media_player.kitchen_echo }   # room -> Echo entity
  reply: "Asking Alexa."
```

The Echo is the one whose room the request names ("… in the kitchen", which is then left off the command), the device's own room, or the only one listed; otherwise it asks which. Until the integration is installed (through HACS, with an Amazon login) and `targets` lists real entities, "ask Alexa" answers "Alexa isn't set up yet." with these steps in chat. Alexa Media Player uses Amazon's unofficial API, so treat it as a convenience.
