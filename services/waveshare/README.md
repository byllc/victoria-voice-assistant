# Waveshare test satellite

A second voice satellite for the Stage 13 checks while only one Satellite1 exists: the Waveshare
ESP32-S3-LCD-1.85 with its speaker box, on ESPHome (`waveshare-lcd-185.yaml`). It has the same API key
(`SATELLITE1_API_KEY`) and wake words ("Victoria", "Hey Viki") as our Satellite1 build. It is not a
Satellite1: one microphone, no XMOS echo cancellation, and no LED ring. The screen stays off for now, and
the BOOT button starts or stops a turn and silences a ringing timer.

```sh
sh services/waveshare/build.sh                                   # compile only
sh services/waveshare/build.sh --flash /dev/cu.usbmodemXXXX       # over USB
sh services/waveshare/build.sh --flash victoria-waveshare.local   # over Wi-Fi
```

Wi-Fi: a new board offers the open network `victoria-waveshare`; join it from a phone and choose the home
network on the page that opens (or use Improv: web.esphome.io over USB, improv-wifi.com over Bluetooth).

In `tools.yaml`, give it the office satellite's `gain` (24): its raw speech level is about the same.

```yaml
satellites:
  waveshare:
    host: victoria-waveshare.local
    key: "${SATELLITE1_API_KEY}"
    gain: 24.0
    pending_color: null
```
