#!/bin/sh
# Run the test Hermes container in the foreground (so launchd or a terminal can supervise it).
# Keys come from the macOS Keychain (docs/secrets.md): HERMES_API_KEY (the gateway's key) and LMS_API_KEY (LM Studio's).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
project=$here/../..
work_dir=${HERMES_WORK_DIR:-$HOME/Services/hermes/work}

# Read into this process only, and passed to the container by name (never on a command line).
value() { uv run --project "$project" --quiet assistant secrets get "$1" 2>/dev/null; }
API_SERVER_KEY=$(value HERMES_API_KEY)
LM_API_KEY=$(value LMS_API_KEY)
if [ -z "$API_SERVER_KEY" ] || [ -z "$LM_API_KEY" ]; then
    echo "Store HERMES_API_KEY and LMS_API_KEY: uv run assistant secrets set HERMES_API_KEY (docs/secrets.md)" >&2
    exit 1
fi
export API_SERVER_KEY LM_API_KEY

mkdir -p "$work_dir"
container rm --force hermes >/dev/null 2>&1 || true
# Only the work folder is shared with the Mac; the API is published on the Mac's loopback only.
exec container run --name hermes --rm \
    --memory 2G \
    --publish 127.0.0.1:8642:8642 \
    --volume "$work_dir:/work" \
    --env API_SERVER_KEY --env LM_API_KEY \
    victoria-hermes:0.22.5
