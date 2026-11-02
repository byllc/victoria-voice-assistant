# Satellite1 firmware

Our build of FutureProofHomes' Satellite1 firmware: their stable release (v0.2.1) with three changes. A fixed
ESPHome API encryption key that we hold (`SATELLITE1_API_KEY`, in the Keychain and `config/.env`) and Bill's
wake words in `wake_words/`: "Victoria" (his own model, trained with TaterTotterson's
microWakeWord-Trainer-AppleSilicon) and "Hey Viki" (from TaterTotterson/microWakeWords, his tuned copy).
The stock "Okay Nabu" and "Hey Jarvis" are switched off at startup. And the voice comes from microphone
channel 1, the clean one (channel 0 clips); it is quiet, so the assistant raises it (`gain` under
`satellites:` in `tools.yaml`).

Only the assistant connects to the satellite; Home Assistant does not, and never gets the key (decisions log,
Stage 12). Stock firmware lets Home Assistant set the key; ours has the key built in.

Build and flash (the first build downloads ESPHome and the ESP32 toolchain; several minutes):

```sh
sh services/satellite1/build.sh                                 # compile only
sh services/satellite1/build.sh --flash /dev/cu.usbmodem2101     # over USB (the Core board's USB-C port, a data cable)
sh services/satellite1/build.sh --flash 192.168.1.251            # over Wi-Fi, once it runs this firmware
```

`build.sh` clones the source into `Satellite1-ESPHome/` (git-ignored), checks out the pinned release, runs the
ESPHome version that release requires, generates and stores the key the first time, and copies
`satellite1.yaml` next to their config (their paths are relative to that folder). Flashing keeps the device's
Wi-Fi settings.


## Connecting the assistant

Add the device under `satellites:` in `config/tools.yaml` (the name is its room's satellite name; see
`config/examples/tools.yaml`), then restart the assistant:

```yaml
satellites:
  office:
    host: satellite1-xxxxxx.local   # or its IP address
    key: "${SATELLITE1_API_KEY}"
```

The assistant connects once its speech models are loaded and reconnects if the connection drops; the log shows
`satellite_connected`. A satellite flashing red has no client connected. It fetches each spoken reply as FLAC
from the assistant's audio server (plain HTTP on the LAN, port 8767 by default); if the macOS firewall is on, it
must allow incoming connections to the project's Python (`.venv/bin/python`).
