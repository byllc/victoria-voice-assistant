# Satellite1 firmware

Our build of FutureProofHomes' Satellite1 firmware: their stable release (v0.2.1) unchanged, plus one
addition, a fixed ESPHome API encryption key that we hold (`SATELLITE1_API_KEY`, in the Keychain and
`config/.env`). Stock firmware lets Home Assistant set the key; with ours, both Home Assistant and the
assistant can connect.

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

In Home Assistant, the device needs the same key (Settings > Devices & services > ESPHome > the Satellite1 >
Reconfigure). To copy it without printing it:

```sh
uv run assistant secrets get SATELLITE1_API_KEY | pbcopy
```
