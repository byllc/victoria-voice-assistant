#!/bin/sh
# Build (and, with --flash PORT|HOST, flash) the Waveshare ESP32-S3-LCD-1.85 test satellite.
#   sh services/waveshare/build.sh                              compile only
#   sh services/waveshare/build.sh --flash /dev/cu.usbmodemXXXX  over USB
#   sh services/waveshare/build.sh --flash victoria-waveshare.local  over Wi-Fi, once it runs this firmware
# Same ESPHome version as our Satellite1 build, and the same key (SATELLITE1_API_KEY), never printed.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
project=$here/../..
esphome_pin=esphome==2026.7.3

key=$(uv run --project "$project" --quiet assistant secrets get SATELLITE1_API_KEY 2>/dev/null || true)
if [ -z "$key" ]; then
    echo "SATELLITE1_API_KEY is not set: build the Satellite1 firmware once (services/satellite1/build.sh)." >&2
    exit 1
fi
umask 077
printf 'api_key: "%s"\n' "$key" > "$here/secrets.yaml"

cd "$here"
if [ "${1:-}" = "--flash" ]; then
    uv tool run --from "$esphome_pin" esphome run waveshare-lcd-185.yaml --device "$2" --no-logs
else
    uv tool run --from "$esphome_pin" esphome compile waveshare-lcd-185.yaml
fi
