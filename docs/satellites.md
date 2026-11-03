# Voice satellites

A satellite is a small voice device in a room: it listens for the wake word, sends what you say to the assistant, and plays the reply. Ours is the FutureProofHomes Satellite1, running our build of its firmware (`services/satellite1/`). Stage 12 is still in progress; the parts not yet tried by voice are marked below.

## How it works

ESPHome devices don't dial out, so the assistant connects *to* each satellite listed under `satellites:` in `config/tools.yaml`, using the device's API encryption key, and keeps the connection open. Only the assistant connects: Home Assistant doesn't, and never gets the key (decisions log, 2026-11-01).

1. The satellite hears the wake word itself ("Victoria" or "Hey Viki") and streams 16 kHz audio to the assistant.
2. The assistant runs the turn through the same pipeline as browser voice: speech detection and Smart Turn decide when you've finished, Whisper transcribes, the router model answers or calls a tool, and Kokoro speaks.
3. The reply is encoded as FLAC (the only format the Satellite1 plays) and served from the assistant's reply audio server on the LAN (plain HTTP, port 8767). The satellite fetches and plays it.
4. If the reply asks you something, the satellite listens again without the wake word.
5. Things the assistant says unprompted (a finished agent task, a notification) are sent to the satellite as announcements.

The code is `src/victoria_voice_assistant/satellite/esphome.py`.

## Setting up a satellite

### 1. Build and flash the firmware

Our firmware is FutureProofHomes' stable release with our API key, the wake words, and the clean microphone channel built in. [services/satellite1/README.md](../services/satellite1/README.md) has the details.

```sh
sh services/satellite1/build.sh                                 # compile only
sh services/satellite1/build.sh --flash /dev/cu.usbmodem2101     # over USB (a data cable to the Core board's USB-C port)
sh services/satellite1/build.sh --flash 192.168.1.251            # over Wi-Fi, once it runs our firmware
```

The first build generates `SATELLITE1_API_KEY` and stores it in the Keychain and `config/.env`. Every satellite built afterwards gets the same key, so one secret serves them all.

### 2. Wi-Fi (a new or reset device only)

Flashing keeps the device's Wi-Fi settings. A new or factory-reset satellite has none. The firmware offers Improv in two ways:

- **Over USB:** with the satellite plugged in, open <https://web.esphome.io> in Chrome or Edge, **Connect**, pick the port, then **Configure Wi-Fi**.
- **Over Bluetooth:** <https://www.improv-wifi.com> in Chrome, or Home Assistant's companion app. Press the action button (the centre button) when asked: the firmware requires it. The LED ring twinkles warm white while Bluetooth setup is running.

Find its address afterwards from your router, or use its mDNS name: `satellite1-` followed by the last six characters of its MAC address, then `.local` (for example `satellite1-34bf90.local`).

### 3. Tell the assistant about it

Add it to `config/tools.yaml`. The name is how the assistant refers to the device; `room` puts it in a room of the device registry (`rooms`, by default the room with the satellite's own name), whose player, Home Assistant area and Echo it then uses:

```yaml
satellites:
  office:
    host: satellite1-34bf90.local   # or its IP address
    key: "${SATELLITE1_API_KEY}"
#   port: 6053      # the ESPHome API port
#   gain: 24        # the microphone is raised by this before speech detection (its clean channel is quiet)
#   room: office    # its room in `rooms`; default: the room with its own name
#   arbitration_offset_db: 0   # added to its score when several hear the same words
#   pending_color: "#FFA000"   # the LED ring while notifications are unread; null leaves the ring alone
# satellite_audio_port: 8767   # one setting for all satellites: where they fetch replies
# satellite_arbitration_s: 0.5 # how long to wait for other satellites waking on the same words
```

Saving `tools.yaml` is enough; no restart (Stage 13). A satellite added under `satellites:` connects, one removed disconnects, one whose `host`, `port` or `key` changed reconnects, and other changes (its `gain`, `room` or `pending_color`, and tool changes) reach it while connected. Only `satellite_audio_port` needs a restart.

Give its room what the room has, under `rooms` (several satellites can share a room):

```yaml
rooms:
  office:
    player: Bill's Mac mini      # music played here ([music.md](music.md))
    area: Office                 # "turn off the lights" from this satellite means the Office's lights
    echo: media_player.office_echo   # "ask Alexa" goes to this Echo
```

### 4. Check it connected

The assistant connects once its speech models are loaded, about 10 seconds after a start. The satellite's red twinkle (no client connected) stops, and the log shows it:

```sh
grep satellite_connected ~/Library/Logs/"Victoria Voice Assistant"/assistant-error.log | tail -1
```

(The services write their JSON log lines to the `-error.log` file.)

### 5. Home Assistant

If the satellite was in Home Assistant before, Home Assistant's ESPHome entry for it can no longer connect (it doesn't have the new key), and its entities show as unavailable. Delete it: **Settings > Devices & services > ESPHome**, the device, **⋮ > Delete**. Whether and when to do this is Bill's call. While Home Assistant isn't connected, the device's sensors, LED ring and buttons aren't available to Home Assistant automations.

## Several satellites in earshot

When more than one satellite hears the same words, one answers (Stage 13). The first to wake opens a short window (`satellite_arbitration_s`, 0.5 s) for others waking too, and each one's audio is held meanwhile, not transcribed. Then a satellite with an open question or a ringing timer answers; otherwise the one whose speech stands highest above its own background noise, with near-ties (within 1.5 dB) going to the first to wake. The others go quietly back to idle. The window passes while you're still speaking, so the reply isn't slower. With one satellite configured, nothing is held.

If the wrong one keeps answering in some spot, give the satellite that should a head start (`arbitration_offset_db: 3` under it) or the other a handicap (a negative value). The log shows each decision: `satellite_arbitration`, with every candidate's score.

## Updating every satellite's firmware

```sh
uv run assistant satellites status              # each satellite's firmware version and build time
uv run assistant satellites update              # build once, then update every satellite over Wi-Fi
uv run assistant satellites update --only office
```

`update` builds the firmware once (`services/satellite1/build.sh`), uploads it to each satellite in `tools.yaml` in turn (`build.sh --upload HOST`), and then reads back each one's version. A satellite that's offline or fails is reported and the rest go ahead. The assistant reconnects to each one as it restarts.

## Moving to the Mac mini (deployment)

The Mac mini runs the assistant in the end; the satellites don't change.

1. **Same network.** The Mac mini must be on the satellites' LAN: it connects to them, and they fetch replies from it on port 8767. `.local` names need mDNS across the network. Otherwise use fixed IP addresses (a DHCP reservation in the router).
2. **The same key.** The satellites have the key built in, so the Mac mini needs the same value. From this Mac, without printing it:

   ```sh
   uv run assistant secrets get SATELLITE1_API_KEY | ssh <mac-mini> 'cd <repository on the Mac mini> && uv run assistant secrets set SATELLITE1_API_KEY'
   ```

   The alternative is a new key: run `build.sh` on the Mac mini (it makes one if none is stored) and reflash every satellite over Wi-Fi.
3. **Configuration.** Copy the `satellites:` section into the Mac mini's `config/tools.yaml`.
4. **One assistant at a time.** A satellite sends its voice to one client. Before starting the Mac mini's assistant, remove `satellites:` from this Mac's configuration, or stop its assistant, so the two don't compete.
5. **Firewall.** If the macOS firewall is on, allow incoming connections for the assistant's Python. Otherwise satellites can't fetch replies, and they stay silent. It's off on the M2.
6. Restart the assistant and check the log (step 4 above).

## Using it

- **Wake words:** "Victoria" or "Hey Viki", then your request in the same breath or after the chime. Pressing the action button once also starts listening.
- **What you can say:** anything you can say in the browser: timers, weather, the time, search, agents, home control, music.
- **Questions back:** when Victoria asks something ("How long should the timer be?"), the satellite listens again: answer without the wake word.
- **Timers** ring on the satellite that set them, and its LED ring shows the time left. To stop one, say "stop" without the wake word (the firmware listens for it only while a timer rings), or press the action button. *Not yet tried by voice.*
- **Volume:** the up and down buttons. The LED ring shows the level.
- **Mute:** the left button mutes the microphones in hardware. A red light by the microphones shows it's muted.
- **Notifications** are read out as announcements when they arrive. While any are unread for the satellite's room, its LED ring glows amber (`pending_color`; `null` turns it off). "Any notifications?" reads them, and the ring goes off.

### The LED ring

| Ring | Meaning |
|---|---|
| Off | Idle, waiting for the wake word |
| Steady amber | Idle, with unread notifications |
| Slow clockwise spin | Woken, waiting for you to speak |
| Fast clockwise spin | Listening |
| Two blinking lights | Thinking |
| Fast anticlockwise spin | Replying |
| Part of the ring lit | A timer is running: the lit part is the time left |
| Whole ring blinking | A timer is ringing |
| Red twinkle | No client connected: the assistant isn't running, or can't reach it |
| Blue twinkle | Starting up, Wi-Fi connected |
| Warm white twinkle | Bluetooth Wi-Fi setup is running |
| Fast red pulse | An error in the turn |
| Red light by the microphones | Muted |

### Power

The Satellite1 needs a USB-C Power Delivery supply of 9 V or more for full volume. FutureProofHomes recommends 30 W. On 5 V (a computer's USB port, for example) its amplifier stays in low-power mode and replies are quiet.

### Resetting the device

Holding the action button for 10 seconds warns (a sound and the LED ring); keep holding to 22 seconds to factory-reset. A reset device keeps our firmware and key but loses its Wi-Fi settings (set them up again, step 2).

## Troubleshooting

| Symptom | Check |
|---|---|
| Red in the dashboard's health strip, or "The office satellite is offline." in Alerts | The assistant has had no connection to it for about a minute: check its power and Wi-Fi (`uv run assistant satellites status` tries it directly). One notice per time it goes offline. |
| Red twinkle | Is the assistant running (`curl http://127.0.0.1:8765/health`)? Is the device under `satellites:` with the right host? The log shows `satellite_unreachable` with a reason: a wrong key, or a host that can't be resolved or reached. |
| It wakes but says nothing | Look for `satellite_reply` (with a `duration_s`) or `satellite_reply_failed`. A reply it can't fetch plays as silence: is the Mac reachable from the satellite on port 8767 (firewall, network)? |
| It wakes, then gives up | `satellite_no_speech`: no speech was detected within 8 seconds of the wake word. Speak sooner, or check `gain` if it keeps happening. |
| Whatever you say is misheard | The microphone level: our firmware uses the clean channel, raised by `gain` (24). Lower it if the audio clips, raise it if speech is missed. |
| Quiet replies | Power: see above. |
| Home Assistant shows it unavailable | Expected: see Home Assistant above. |

Log events, all with `"device": "<name>"`: `satellite_connected`, `satellite_disconnected`, `satellite_unreachable`, `satellite_turn` (with the wake word), `satellite_no_speech`, `satellite_no_reply`, `satellite_turn_aborted`, `satellite_reply`, `satellite_announce`, `satellite_reply_failed`, `satellite_send_failed`.
