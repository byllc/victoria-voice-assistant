#!/bin/sh
# Run the test Hermes container in the foreground (so launchd or a terminal can supervise it).
# Keys come from config/.env: HERMES_API_KEY (the gateway's key) and LMS_API_KEY (LM Studio's).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
config_dir=${ASSISTANT_CONFIG_DIR:-$here/../../config}
work_dir=${HERMES_WORK_DIR:-$HOME/Services/hermes/work}

value() { sed -n "s/^$1=//p" "$config_dir/.env" | tail -n 1; }
API_SERVER_KEY=$(value HERMES_API_KEY)
LM_API_KEY=$(value LMS_API_KEY)
if [ -z "$API_SERVER_KEY" ] || [ -z "$LM_API_KEY" ]; then
    echo "Set HERMES_API_KEY and LMS_API_KEY in $config_dir/.env" >&2
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
