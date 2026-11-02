#!/bin/sh
# Build (and, with --flash PORT, flash) our Satellite1 firmware: FutureProofHomes' stable source plus our key.
#   sh services/satellite1/build.sh                       compile only
#   sh services/satellite1/build.sh --flash /dev/cu.usbmodem2101
#   sh services/satellite1/build.sh --flash 192.168.1.251  (over Wi-Fi, once it runs this firmware)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
project=$here/../..
ref=v0.2.1  # FutureProofHomes' stable release; its requirements.txt pins the ESPHome version
if [ ! -d "$here/Satellite1-ESPHome" ]; then
    git clone -q https://github.com/FutureProofHomes/Satellite1-ESPHome.git "$here/Satellite1-ESPHome"
fi
git -C "$here/Satellite1-ESPHome" fetch -q --tags origin
git -C "$here/Satellite1-ESPHome" checkout -q "$ref"
esphome_pin=$(grep '^esphome==' "$here/Satellite1-ESPHome/requirements.txt")

# The key: from the Keychain (or config/.env), never printed; generated and stored once if missing.
key=$(uv run --project "$project" --quiet assistant secrets get SATELLITE1_API_KEY 2>/dev/null || true)
if [ -z "$key" ]; then
    key=$(python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())')
    printf '%s' "$key" | uv run --project "$project" --quiet assistant secrets set SATELLITE1_API_KEY >/dev/null
    echo "Generated a new SATELLITE1_API_KEY and stored it (Keychain and config/.env)."
fi
umask 077
printf 'api_key: "%s"\n' "$key" > "$here/secrets.yaml"

cd "$here"
if [ "${1:-}" = "--flash" ]; then
    uv tool run --from "$esphome_pin" esphome run satellite1.yaml --device "$2" --no-logs
else
    uv tool run --from "$esphome_pin" esphome compile satellite1.yaml
fi
